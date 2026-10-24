"""hooke CLI — stateless protocol client.

`init`, `deploy`, `status`, `logs`, `disable`, `enable`, `remove`,
`install`, and `daemon` (which just execs the daemon). The CLI never runs
automations itself; it is the lint layer plus a thin socket client.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from . import config, paths, protocol, systemd
from .agents import AgentRunner
from .config import ConfigError


def _fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


# ------------------------------------------------------------------- init


def cmd_init(args: argparse.Namespace) -> None:
    target = pathlib.Path(args.path or ".") / "hooke.toml"
    if target.exists():
        _fail(f"{target} already exists")
    name = target.parent.resolve().name.lower().replace(" ", "-")
    if not config._NAME_RE.match(name):
        name = "my-project"
    target.write_text(config.INIT_TEMPLATE.format(name=name))
    print(f"wrote {target}")
    print("edit the [automation.*] tables, then run `hooke deploy`.")


# ----------------------------------------------------------------- deploy


def _check_mcp_servers(norm: dict) -> None:
    """Explicit MCP allowlist enforced at deploy time: fail here, not in a
    cron run at 3am."""
    configured: set[str] = set()
    mcp_json = pathlib.Path.home() / ".pi" / "agent" / "mcp.json"
    try:
        data = json.loads(mcp_json.read_text())
        configured = set((data.get("mcpServers") or {}).keys())
    except (OSError, json.JSONDecodeError):
        pass
    for auto in norm["automations"].values():
        for server in auto["mcp_servers"]:
            if server not in configured:
                _fail(
                    f"automation '{auto['name']}' references MCP server '{server}' "
                    f"which is not configured in {mcp_json} — "
                    f"configure it with pi first, then deploy"
                )


def _check_agents(norm: dict) -> None:
    """Every automation's agent must be registered — fail at deploy, not
    in a cron run at 3am."""
    for auto in norm["automations"].values():
        try:
            AgentRunner.get_agent(auto["agent"])
        except ConfigError as e:
            _fail(f"automation '{auto['name']}': {e}")


def _check_trust(root: pathlib.Path) -> None:
    """Project trust is user-approved in pi; the daemon runs headless, so
    warn loudly at deploy instead of failing silently at 3am."""
    trust_json = pathlib.Path.home() / ".pi" / "agent" / "trust.json"
    try:
        trust = json.loads(trust_json.read_text())
    except (OSError, json.JSONDecodeError):
        trust = {}
    if not trust.get(str(root)):
        print(
            f"warning: {root} is not in pi's project trust list ({trust_json}). "
            "The daemon runs headless and cannot approve trust prompts — open pi "
            "once in that directory and approve, or add the path to trust.json.",
            file=sys.stderr,
        )


def cmd_deploy(args: argparse.Namespace) -> None:
    root = pathlib.Path(args.path or ".").resolve()
    toml = root / "hooke.toml"
    try:
        norm, digest = config.load_and_validate(toml)
    except ConfigError as e:
        _fail(str(e))
    _check_agents(norm)
    _check_mcp_servers(norm)
    _check_trust(root)

    socket = paths.socket_path()
    try:
        reply = protocol_sync(socket, {"op": "deploy", "project": norm["name"], "config": norm, "hash": digest})
    except protocol.ProtocolError as e:
        _fail(str(e))
    if not reply.get("ok"):
        _fail(reply.get("error", "deploy failed"))
    if reply.get("unchanged"):
        print(f"{norm['name']}: up to date, nothing to do.")
        return
    s = reply.get("summary") or {}
    print(
        f"deployed {norm['name']}: {s.get('automations', '?')} automations "
        f"({s.get('scheduled', '?')} scheduled, {s.get('watched', '?')} watched)"
    )
    for key, st in sorted((s.get("schedule_state") or {}).items()):
        due = st.get("next_due")
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(due)) if due else "?"
        print(f"  {key}: next due {when}")


def protocol_sync(socket, op: dict) -> dict:
    return _run(protocol.request(socket, op))


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# ------------------------------------------------------- status/logs/etc.


def cmd_status(_args: argparse.Namespace) -> None:
    reply = _try_query({"op": "status"})
    print(f"daemon {reply.get('daemon_version')} (pid {reply.get('pid')}, user {reply.get('user')})")
    autos = reply.get("automations") or []
    if not autos:
        print("no automations deployed")
    for a in autos:
        flag = " [disabled]" if a.get("disabled") else ""
        due = a.get("next_due")
        due_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(due)) if due and a["trigger"] == "schedule" else "-"
        last = a.get("last_fired")
        last_s = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)) if last else "never"
        print(f"  {a['project']}/{a['automation']}{flag}  {a['trigger']:<8}  last: {last_s}  next: {due_s}")
    print(f"active runs: {reply.get('active_runs', 0)}")
    runs = reply.get("recent_runs") or []
    if runs:
        print("recent runs:")
        for r in runs[:10]:
            started = r.get("started_at")
            s = time.strftime("%m-%d %H:%M:%S", time.localtime(started)) if started else "?"
            print(f"  {r.get('run_id')}  {r.get('project')}/{r.get('automation')}  "
                  f"{r.get('trigger_kind')}  {r.get('status')}  {s}")


def cmd_logs(args: argparse.Namespace) -> None:
    if args.run_id:
        reply = _try_query({"op": "logs", "run_id": args.run_id})
        rec = reply.get("run") or {}
        print(json.dumps(rec, indent=2))
        if reply.get("log"):
            print("--- pi event stream (tail) ---")
            print(reply["log"])
        return
    op: dict = {"op": "logs", "limit": args.n}
    if args.project:
        op["project"] = args.project
    reply = _try_query(op)
    runs = reply.get("runs") or []
    if not runs:
        print("no runs recorded")
        return
    print(f"{'run_id':<14}{'project/automation':<45}{'trigger':<10}{'status':<12}{'started'}")
    for r in runs:
        started = r.get("started_at")
        s = time.strftime("%m-%d %H:%M:%S", time.localtime(started)) if started else "?"
        ident = f"{r.get('project')}/{r.get('automation')}"
        print(f"{r.get('run_id', '?'):<14}{ident:<45}{r.get('trigger_kind', '?'):<10}"
              f"{r.get('status', '?'):<12}{s}")


def cmd_disable(args: argparse.Namespace) -> None:
    _project_op({"op": "disable", "project": args.project, "disabled": True},
                f"disabled {args.project} (kept in registry, inert)")


def cmd_enable(args: argparse.Namespace) -> None:
    _project_op({"op": "disable", "project": args.project, "disabled": False},
                f"enabled {args.project}")


def cmd_remove(args: argparse.Namespace) -> None:
    _project_op({"op": "remove", "project": args.project}, f"removed {args.project}")


def _project_op(op: dict, success_msg: str) -> None:
    try:
        reply = protocol_sync(paths.socket_path(), op)
    except protocol.ProtocolError as e:
        _fail(str(e))
    if not reply.get("ok"):
        _fail(reply.get("error", "operation failed"))
    print(success_msg)


def _try_query(op: dict) -> dict:
    try:
        reply = protocol_sync(paths.socket_path(), op)
    except protocol.ProtocolError as e:
        _fail(str(e))
    if not reply.get("ok"):
        _fail(reply.get("error", "query failed"))
    return reply


def cmd_daemon(_args: argparse.Namespace) -> None:
    from .daemon import main as daemon_main

    daemon_main()


def cmd_install(args: argparse.Namespace) -> None:
    systemd.install(use_user_unit=args.user)


# ------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hooke", description="agentic workflow automator")
    sub = p.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a starter hooke.toml in a project")
    init.add_argument("path", nargs="?", help="project directory (default: cwd)")
    init.set_defaults(fn=cmd_init)

    dep = sub.add_parser("deploy", help="validate hooke.toml and deploy it to the daemon")
    dep.add_argument("path", nargs="?", help="project directory (default: cwd)")
    dep.set_defaults(fn=cmd_deploy)

    sub.add_parser("status", help="daemon + automation status").set_defaults(fn=cmd_status)

    logs = sub.add_parser("logs", help="run history / a run's pi event stream")
    logs.add_argument("project", nargs="?", help="filter by project")
    logs.add_argument("-n", type=int, default=20, help="how many runs (default 20)")
    logs.add_argument("--run", dest="run_id", help="show one run's full record + log")
    logs.set_defaults(fn=cmd_logs)

    dis = sub.add_parser("disable", help="disable a project (kept in registry, inert)")
    dis.add_argument("project")
    dis.set_defaults(fn=cmd_disable)

    en = sub.add_parser("enable", help="re-enable a disabled project")
    en.add_argument("project")
    en.set_defaults(fn=cmd_enable)

    rm = sub.add_parser("remove", help="remove a project from the daemon registry")
    rm.add_argument("project")
    rm.set_defaults(fn=cmd_remove)

    ins = sub.add_parser("install", help="install and start the systemd unit")
    ins.add_argument("--user", action="store_true",
                     help="user unit + linger instead of the templated system unit")
    ins.set_defaults(fn=cmd_install)

    sub.add_parser("daemon", help="run the daemon (used by the systemd unit)").set_defaults(fn=cmd_daemon)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
