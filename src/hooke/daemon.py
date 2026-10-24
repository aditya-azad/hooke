"""hooked — the hooke daemon (run as `hooke daemon`, one per user).

Single asyncio process owning scheduling, watching, and execution. Boot
sequence: load registry → load run history → recovery pass → arm
schedulers and watchers → serve socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import getpass
import os
import signal
import time

from . import config, paths, protocol
from .agents import AgentRunner
from .executor import Executor, Job
from .scheduler import boot_next_due, scheduler_loop
from .stores import RegistryStore, RunStore, ScheduleState
from .watcher import WatcherSet


class Daemon:
    def __init__(self) -> None:
        paths.ensure_dirs()
        self.registry = RegistryStore()
        self.schedule_state = ScheduleState()
        self.run_store = RunStore()
        self.executor = Executor(
            self.run_store,
            max_concurrent=int(os.environ.get("HOOKE_MAX_CONCURRENT_RUNS", config.DEFAULT_MAX_CONCURRENT_RUNS)),
        )
        self.watchers = WatcherSet(self.executor)
        self.sched_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.armed_cfgs: dict[tuple[str, str], dict] = {}  # config snapshot per armed automation
        self.server: asyncio.Server | None = None
        self.shutdown_evt = asyncio.Event()

    # ---------------------------------------------------------------- boot

    async def run(self) -> None:
        self._acquire_instance_lock()
        self._boot_recovery()
        self.executor.start_workers()
        for proj in self.registry.projects().values():
            if proj.get("disabled"):
                continue
            for auto in proj["automations"].values():
                self._arm(proj, auto)
        await self._serve()
        print(
            f"hooke daemon {config.HOOKE_VERSION} up: "
            f"{len(self.registry.projects())} project(s), socket {paths.socket_path()}",
            flush=True,
        )
        await self.shutdown_evt.wait()
        await self._shutdown()

    def _acquire_instance_lock(self) -> None:
        """One daemon per user: flock on the state dir."""
        self._lock_fh = (paths.state_dir() / "daemon.lock").open("w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                "another hooke daemon is already running for this user "
                "(holding ~/.local/state/hooke/daemon.lock)"
            )

    def _boot_recovery(self) -> None:
        latest = self.run_store.load_latest()
        now = time.time()

        # Run recovery: anything still pending/running died with the process.
        interrupted: list[dict] = []
        for rec in latest.values():
            if rec.get("status") in ("pending", "running", "retry_scheduled"):
                self.run_store.append(
                    {
                        "run_id": rec["run_id"],
                        "status": "interrupted",
                        "ended_at": now,
                        "error": "daemon restart",
                    }
                )
                interrupted.append(rec)

        # Scheduler recovery: last_fired = max(started_at) over history;
        # history stays append-only evidence, schedule-state gets the truth.
        for proj in self.registry.projects().values():
            for auto in proj["automations"].values():
                if not auto.get("schedule"):
                    continue
                key = f"{proj['name']}/{auto['name']}"
                starts = [
                    r["started_at"]
                    for r in latest.values()
                    if r.get("project") == proj["name"]
                    and r.get("automation") == auto["name"]
                    and r.get("started_at")
                ]
                last = max(starts) if starts else None
                _, next_due = boot_next_due(last, auto["schedule"]["every"], now)
                self.schedule_state.set(key, last, next_due)

        # Run recovery: resume opted-in automations with their original
        # trigger payload; drop the rest.
        for rec in interrupted:
            proj = self.registry.projects().get(rec.get("project", ""))
            if not proj or proj.get("disabled"):
                continue
            auto = proj["automations"].get(rec.get("automation", ""))
            if not auto or not auto.get("resume_on_restart"):
                continue
            job = Job(
                proj["name"],
                auto["name"],
                self._make_cfg(auto),
                proj["root"],
                rec.get("trigger_kind") or "boot",
                rec.get("trigger"),
                resumed_from=rec["run_id"],
                session_ref=rec.get("session_ref"),
            )
            with contextlib.suppress(Exception):
                # fire_and_forget style; enqueue is cheap and synchronous up
                # to the queue put.
                asyncio.get_running_loop().create_task(self._enqueue_later(job))

    async def _enqueue_later(self, job: Job) -> None:
        # Small delay so resumed work doesn't stampede pi at boot alongside
        # the scheduler jitter.
        await asyncio.sleep(2.0)
        await self.executor.enqueue(job)

    # ------------------------------------------------------------- arming

    def _make_cfg(self, auto: dict) -> dict:
        # Least privilege: the agent connects every configured MCP server,
        # so the per-automation allowlist is enforced by excluding every
        # server the automation did not name. Discovery is agent-aware
        # (pi: ~/.pi/agent/mcp.json, claude: ~/.claude.json, codex:
        # ~/.codex/config.toml); when an agent's config cannot be read,
        # nothing is excluded and its own defaults apply.
        agent = AgentRunner.get_agent(auto.get("agent") or "pi")
        configured, _known = agent.configured_mcp_servers()
        return {
            **auto,
            "excluded_mcp_servers": sorted(
                configured - set(auto.get("mcp_servers") or [])
            ),
        }

    def _arm(self, proj: dict, auto: dict) -> None:
        key = (proj["name"], auto["name"])
        cfg = self._make_cfg(auto)
        if auto.get("schedule"):
            interval = auto["schedule"]["every"]
            state = self.schedule_state.get(f"{key[0]}/{key[1]}")
            if not state or state.get("next_due") is None:
                _, next_due = boot_next_due(None, interval)
                self.schedule_state.set(f"{key[0]}/{key[1]}", None, next_due)
            self.sched_tasks[key] = asyncio.create_task(
                scheduler_loop(
                    f"{key[0]}/{key[1]}",
                    Job(proj["name"], auto["name"], cfg, proj["root"], "schedule", None),
                    self.schedule_state,
                    self.executor,
                ),
                name=f"hooke-sched-{key[0]}/{key[1]}",
            )
        if auto.get("watch"):
            self.armed_cfgs[key] = auto
            # Watchers arm via a task so they don't block boot.
            asyncio.get_running_loop().create_task(
                self.watchers.arm(key, Job(proj["name"], auto["name"], cfg, proj["root"], "watch", None))
            )

    def _disarm(self, key: tuple[str, str]) -> None:
        # Deliberately does NOT touch schedule-state: a redeploy of an
        # otherwise-identical automation must not reset its last_fired.
        task = self.sched_tasks.pop(key, None)
        if task:
            task.cancel()
        self.watchers.disarm(key)
        self.armed_cfgs.pop(key, None)

    # ------------------------------------------------------------- socket

    async def _serve(self) -> None:
        sock = paths.socket_path()
        sock.parent.mkdir(parents=True, exist_ok=True)
        for stale in (sock,):
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()
        old_umask = os.umask(0o077)
        try:
            self.server = await asyncio.start_unix_server(self._handle_conn, str(sock))
        finally:
            os.umask(old_umask)
        with contextlib.suppress(OSError):
            os.chmod(sock, 0o700)

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await protocol.send_json(writer, protocol.hello_message())
            op = await asyncio.wait_for(protocol.recv_json(reader), timeout=10)
            reply = await self._dispatch(op)
            await protocol.send_json(writer, reply)
        except (protocol.ProtocolError, TimeoutError, ConnectionError) as e:
            with contextlib.suppress(Exception):
                await protocol.send_json(writer, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001 - one bad client must not kill the daemon
            with contextlib.suppress(Exception):
                await protocol.send_json(writer, {"ok": False, "error": f"daemon error: {e}"})
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, op: dict) -> dict:
        kind = op.get("op")
        if kind == "deploy":
            return await self._op_deploy(op)
        if kind == "remove":
            return self._op_remove(op)
        if kind == "disable":
            return self._op_disable(op)
        if kind == "status":
            return self._op_status()
        if kind == "logs":
            return self._op_logs(op)
        return {"ok": False, "error": f"unknown op {kind!r}"}

    # ---------------------------------------------------------------- ops

    async def _op_deploy(self, op: dict) -> dict:
        # Never trust the CLI: re-validate independently.
        try:
            norm = config.validate_normalized(op.get("config") or {})
        except config.ConfigError as e:
            return {"ok": False, "error": f"invalid config: {e}"}
        name = norm["name"]
        existing = self.registry.get_project(name)
        if existing and existing.get("hash") == op.get("hash") and not existing.get("disabled"):
            return {"ok": True, "unchanged": True, "summary": self._summary(name)}
        norm["hash"] = op.get("hash")
        norm["disabled"] = False
        norm["deployed_at"] = time.time()

        old = existing or {"automations": {}, "root": norm["root"]}
        # Resolve in-flight runs: they keep running against their captured
        # config snapshot; the new config governs all subsequent triggers.
        old_keys = {(name, a) for a in old["automations"]}
        new_keys = {(name, a) for a in norm["automations"]}
        for key in old_keys - new_keys:
            self._disarm(key)
        for key in new_keys:
            auto = norm["automations"][key[1]]
            prev = old["automations"].get(key[1])
            if key not in old_keys or prev != auto:
                self._disarm(key)
                self._arm(norm, auto)
        self.registry.upsert(norm)
        # Registry is persisted synchronously (inside upsert) before replying.
        return {"ok": True, "summary": self._summary(name)}

    def _op_remove(self, op: dict) -> dict:
        name = op.get("project")
        if not isinstance(name, str):
            return {"ok": False, "error": "project required"}
        for key in [k for k in self.armed_cfgs if k[0] == name]:
            self._disarm(key)
        for key in [k for k in self.sched_tasks if k[0] == name]:
            self._disarm(key)
            self.schedule_state.pop(f"{key[0]}/{key[1]}")
        if self.registry.remove(name):
            return {"ok": True}
        return {"ok": False, "error": f"unknown project {name!r}"}

    def _op_disable(self, op: dict) -> dict:
        name = op.get("project")
        disabled = bool(op.get("disabled", True))
        if not isinstance(name, str):
            return {"ok": False, "error": "project required"}
        proj = self.registry.set_disabled(name, disabled)
        if proj is None:
            return {"ok": False, "error": f"unknown project {name!r}"}
        for key in [k for k in list(self.sched_tasks) + list(self.armed_cfgs) if k[0] == name]:
            self._disarm(key)
            if disabled:
                self.schedule_state.pop(f"{key[0]}/{key[1]}")
        if not disabled:
            for auto in proj["automations"].values():
                self._arm(proj, auto)
        return {"ok": True, "disabled": disabled}

    def _summary(self, name: str) -> dict:
        proj = self.registry.get_project(name) or {}
        autos = proj.get("automations") or {}
        scheduled = sum(1 for a in autos.values() if a.get("schedule"))
        watched = sum(1 for a in autos.values() if a.get("watch"))
        return {
            "project": name,
            "automations": len(autos),
            "scheduled": scheduled,
            "watched": watched,
            "schedule_state": {k: v for k, v in self.schedule_state.data.items() if k.startswith(name + "/")},
        }

    def _op_status(self) -> dict:
        armed = []
        for proj in self.registry.projects().values():
            for auto in proj["automations"].values():
                key = f"{proj['name']}/{auto['name']}"
                armed.append(
                    {
                        "project": proj["name"],
                        "automation": auto["name"],
                        "disabled": bool(proj.get("disabled")),
                        "trigger": "schedule" if auto.get("schedule") else "watch",
                        "next_due": (self.schedule_state.get(key) or {}).get("next_due"),
                        "last_fired": (self.schedule_state.get(key) or {}).get("last_fired"),
                    }
                )
        runs = sorted(
            self.run_store.load_latest().values(),
            key=lambda r: r.get("started_at") or r.get("ended_at") or 0,
            reverse=True,
        )[:20]
        return {
            "ok": True,
            "daemon_version": config.HOOKE_VERSION,
            "pid": os.getpid(),
            "user": getpass.getuser(),
            "automations": armed,
            "recent_runs": runs,
            "active_runs": sum(self.executor.active_counts.values()),
        }

    def _op_logs(self, op: dict) -> dict:
        run_id = op.get("run_id")
        if run_id:
            rec = self.run_store.load_latest().get(run_id)
            if not rec:
                return {"ok": False, "error": f"unknown run {run_id!r}"}
            log_path = rec.get("log_path")
            content = None
            if log_path:
                with contextlib.suppress(OSError):
                    with open(log_path) as f:
                        lines = f.readlines()
                    content = "".join(lines[-2000:])
            return {"ok": True, "run": rec, "log": content}
        project = op.get("project")
        limit = int(op.get("limit", 20))
        runs = sorted(
            self.run_store.load_latest().values(),
            key=lambda r: r.get("started_at") or r.get("ended_at") or 0,
            reverse=True,
        )
        if project:
            runs = [r for r in runs if r.get("project") == project]
        return {"ok": True, "runs": runs[:limit]}

    # ----------------------------------------------------------- shutdown

    async def _shutdown(self) -> None:
        self.watchers.disarm_all()
        for task in self.sched_tasks.values():
            task.cancel()
        await asyncio.gather(*self.sched_tasks.values(), return_exceptions=True)
        await self.executor.shutdown()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            paths.socket_path().unlink(missing_ok=True)
        self.run_store.close()


def main() -> None:
    daemon = Daemon()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, daemon.shutdown_evt.set)
        await daemon.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
