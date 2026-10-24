"""Executor: bounded worker pool that runs each automation through its
configured agent (see :mod:`hooke.agents`; default: pi).

Every run gets a run record persisted to runs.jsonl **at enqueue time,
before the child starts** — that is what makes the boot recovery pass
trustworthy. The agent runs headlessly in JSON mode and its event stream is
streamed into ``run-logs/<run_id>.jsonl``.

Retries apply only to hard crashes (pi exits nonzero and produced no
assistant output at all). "The agent finished but did the wrong thing" is
not retryable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import uuid

from . import paths
from .agents import AgentRunner, RunRequest
from .stores import RunStore

RETRY_BACKOFF_SECONDS = (30.0, 60.0, 120.0)


class Job:
    __slots__ = (
        "automation",
        "cfg",
        "project",
        "resumed_from",
        "root",
        "session_ref",
        "trigger",
        "trigger_kind",
    )

    def __init__(
        self,
        project: str,
        automation: str,
        cfg: dict,
        root: str,
        trigger_kind: str,
        trigger: dict | None,
        resumed_from: str | None = None,
        session_ref: str | None = None,
    ) -> None:
        self.project = project
        self.automation = automation
        self.cfg = cfg
        self.root = root
        self.trigger_kind = trigger_kind
        self.trigger = trigger
        self.resumed_from = resumed_from
        self.session_ref = session_ref

    @property
    def key(self) -> tuple[str, str]:
        return (self.project, self.automation)


def build_prompt(job: Job, resumed_from: str | None) -> str:
    parts = [job.cfg["prompt"].strip(), "", "---", "hooke automation context:"]
    trigger = f"trigger: {job.trigger_kind}"
    if job.trigger_kind == "watch" and job.trigger:
        files = job.trigger.get("files") or {}
        trigger += " (changed paths: "
        bits = []
        for kind in ("added", "modified", "removed"):
            items = files.get(kind) or []
            if items:
                bits.append(f"{kind}: {', '.join(items)}")
        trigger += "; ".join(bits) + ")" if bits else ")"
    parts.append(f"- {trigger}")
    parts.append(f"- project: {job.project}")
    parts.append(f"- automation: {job.automation}")
    if resumed_from:
        parts.append(
            f"- a previous run ({resumed_from}) was interrupted before it could "
            "finish; inspect any partial work from earlier in this session and "
            "continue from there instead of starting over"
        )
    return "\n".join(parts)


def build_run_request(
    job: Job, prompt: str, session_ref: str | None
) -> RunRequest:
    """Translate the automation config into the agent-neutral run request."""
    continuity = bool(job.cfg.get("continuity"))
    return RunRequest(
        prompt=prompt,
        cwd=job.root,
        model=job.cfg.get("model"),
        thinking=job.cfg.get("thinking"),
        # Least privilege: exclude every configured MCP server that is not
        # in this automation's allowlist.
        excluded_mcp_servers=tuple(job.cfg.get("excluded_mcp_servers") or []),
        continuity=continuity,
        session_dir=paths.sessions_dir(job.project, job.automation)
        if continuity
        else None,
        session_ref=session_ref,
    )


class Executor:
    def __init__(self, run_store: RunStore, max_concurrent: int) -> None:
        self.run_store = run_store
        self.queue: asyncio.Queue[Job | None] = asyncio.Queue()
        self.max_concurrent = max(1, max_concurrent)
        self.active_counts: dict[tuple[str, str], int] = {}
        # Paths written by the currently-running (or just-finished) run of an
        # automation, for watcher self-trigger suppression.
        self.recent_writes: dict[tuple[str, str], dict[str, float]] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.session_refs: dict[tuple[str, str], str] = {}
        self.workers: list[asyncio.Task] = []
        self.shutting_down = False

    def start_workers(self) -> None:
        for i in range(self.max_concurrent):
            self.workers.append(asyncio.create_task(self._worker(i), name=f"hooke-worker-{i}"))

    async def enqueue(self, job: Job) -> str | None:
        """Queue a run. Persists the pending record first. Returns run_id,
        or None when dropped (skip-if-running)."""
        if self.shutting_down:
            return None
        key = job.key
        limit = job.cfg.get("concurrency", 1)
        if self.active_counts.get(key, 0) >= limit:
            return None  # skip-if-running: file-triggered work never overlaps
        run_id = uuid.uuid4().hex[:12]
        self.run_store.append(
            {
                "run_id": run_id,
                "project": job.project,
                "automation": job.automation,
                "trigger_kind": job.trigger_kind,
                "trigger": job.trigger,
                "resumed_from": job.resumed_from,
                "started_at": None,
                "ended_at": None,
                "pid": None,
                "exit_code": None,
                "status": "pending",
                "session_ref": None,
                "log_path": None,
                "attempts": 0,
                "error": None,
            }
        )
        self.active_counts[key] = self.active_counts.get(key, 0) + 1
        self.queue.put_nowait((job, run_id))  # type: ignore[arg-type]
        return run_id

    async def _worker(self, i: int) -> None:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            job, run_id = item
            try:
                await self._run_with_retries(job, run_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - a worker must never die
                self.run_store.append(
                    {
                        "run_id": run_id,
                        "status": "failed",
                        "ended_at": time.time(),
                        "error": f"executor error: {e}",
                    }
                )
            finally:
                key = job.key
                self.active_counts[key] = max(0, self.active_counts.get(key, 1) - 1)
                self.queue.task_done()

    async def _run_with_retries(self, job: Job, run_id: str) -> None:
        session_ref = (
            job.session_ref or self.session_refs.get(job.key)
            if job.cfg.get("continuity")
            else None
        )
        resumed_from = job.resumed_from
        attempts = 0
        max_attempts = 1 + len(RETRY_BACKOFF_SECONDS)
        while True:
            attempts += 1
            status, exit_code, error, writes, session = await self._spawn_agent(
                job, run_id, session_ref, resumed_from, attempts
            )
            for path in writes:
                self.recent_writes.setdefault(job.key, {})[path] = time.time() + 10.0
            if session:
                self.session_refs[job.key] = session
            # Hard crash = nonzero exit with no assistant output at all.
            # "Agent finished but did the wrong thing" is not retryable.
            if status != "crashed" or attempts >= max_attempts:
                self.run_store.append(
                    {
                        "run_id": run_id,
                        "status": status,
                        "ended_at": time.time(),
                        "pid": None,
                        "exit_code": exit_code,
                        "attempts": attempts,
                        "error": error,
                        "session_ref": session,
                    }
                )
                return
            # hard crash → retry with backoff
            delay = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
            self.run_store.append(
                {
                    "run_id": run_id,
                    "status": "retry_scheduled",
                    "attempts": attempts,
                    "error": error,
                    "retry_after": time.time() + delay,
                }
            )
            await asyncio.sleep(delay)
            resumed_from = None  # resume marker applies to the first attempt only

    async def _spawn_agent(
        self,
        job: Job,
        run_id: str,
        session_ref: str | None,
        resumed_from: str | None,
        attempt: int,
    ) -> tuple[str, int | None, str | None, list[str], str | None]:
        """Spawn one agent process for the automation's configured agent.
        Returns (status, exit_code, error, write_paths, session_id)."""
        agent: AgentRunner = AgentRunner.get_agent(job.cfg.get("agent") or "pi")
        prompt = build_prompt(job, resumed_from)
        argv = agent.build_argv(build_run_request(job, prompt, session_ref))
        log_path = paths.run_logs_dir() / f"{run_id}.jsonl"
        started = time.time()
        self.run_store.append(
            {
                "run_id": run_id,
                "status": "running",
                "started_at": started,
                "attempt": attempt,
                "pid": None,
                "log_path": str(log_path),
            }
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=job.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # own process group → killable as a tree
                limit=1 << 24,  # pi JSON events (system prompts) exceed the 64KB default
            )
        except FileNotFoundError:
            return "failed", None, f"{agent.name} binary not found on PATH", [], None
        self.run_store.append({"run_id": run_id, "pid": proc.pid, "status": "running"})
        self.processes[run_id] = proc

        session_id: str | None = None
        assistant_output = False
        writes: list[str] = []
        log_f = log_path.open("a")

        async def pump_stdout() -> None:
            nonlocal session_id, assistant_output
            # Persist the session id as soon as it is known: if the daemon
            # dies mid-run, boot recovery can reopen the same session.
            session_persisted = False
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip("\n")
                if not line:
                    continue
                log_f.write(line + "\n")
                log_f.flush()
                ev = agent.parse_event(line)
                if ev.session_id:
                    session_id = ev.session_id
                    if not session_persisted:
                        session_persisted = True
                        self.run_store.append({"run_id": run_id, "session_ref": session_id})
                if ev.assistant_message:
                    assistant_output = True
                writes.extend(ev.write_paths)

        status: str = "running"
        error: str | None = None
        stdout_task = asyncio.create_task(pump_stdout())
        try:
            await asyncio.wait_for(proc.wait(), timeout=job.cfg.get("timeout", 1800.0))
        except TimeoutError:
            status = "timed_out"
            error = f"timeout after {job.cfg.get('timeout', 1800.0):.0f}s"
            await self._kill_tree(proc)
        except asyncio.CancelledError:
            status = "interrupted"
            await self._kill_tree(proc)
            raise
        finally:
            self.processes.pop(run_id, None)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(stdout_task, timeout=5)
            log_f.close()

        exit_code = proc.returncode
        if status == "running":  # wait() returned normally
            if exit_code == 0:
                status = "finished"
            elif assistant_output:
                status = "failed"
                error = f"{agent.name} exited nonzero ({exit_code}) after producing output"
            else:
                status = "crashed"  # hard crash → retryable
                error = f"{agent.name} exited nonzero ({exit_code}) with no assistant output"
        return status, exit_code, error, writes, session_id

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        for _ in range(20):
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.1)
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()

    async def shutdown(self) -> None:
        """SIGTERM path: kill children, mark their runs interrupted."""
        self.shutting_down = True
        for run_id, proc in list(self.processes.items()):
            await self._kill_tree(proc)
            self.run_store.append(
                {
                    "run_id": run_id,
                    "status": "interrupted",
                    "ended_at": time.time(),
                    "error": "daemon shutting down",
                }
            )
        # Drain queued-but-unstarted jobs: their pending records become
        # interrupted so the boot recovery pass sees consistent truth.
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if item is not None:
                run_id = item[1]
                self.run_store.append(
                    {
                        "run_id": run_id,
                        "status": "interrupted",
                        "ended_at": time.time(),
                        "error": "daemon shutting down (never started)",
                    }
                )
        for w in self.workers:
            w.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
