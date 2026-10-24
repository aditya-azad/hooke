"""Filesystem layout for hooke state.

State dir: ``$XDG_STATE_HOME/hooke`` (default ``~/.local/state/hooke``).
Socket: ``/run/hooke/<user>.sock`` when that directory exists and is
writable (set up via tmpfiles.d by ``hooke install``), otherwise
``$XDG_RUNTIME_DIR/hooke.sock``.
"""

from __future__ import annotations

import os
import pathlib
import tempfile


def state_dir() -> pathlib.Path:
    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return pathlib.Path(base) / "hooke"


def registry_path() -> pathlib.Path:
    return state_dir() / "registry.json"


def schedule_state_path() -> pathlib.Path:
    return state_dir() / "schedule-state.json"


def runs_path() -> pathlib.Path:
    return state_dir() / "runs.jsonl"


def run_logs_dir() -> pathlib.Path:
    return state_dir() / "run-logs"


def sessions_root() -> pathlib.Path:
    return state_dir() / "sessions"


def sessions_dir(project: str, automation: str) -> pathlib.Path:
    return sessions_root() / project / automation


def socket_path() -> pathlib.Path:
    import getpass

    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - exotic daemon contexts
        user = os.environ.get("USER", "nobody")
    run_hooke = pathlib.Path("/run/hooke")
    if run_hooke.is_dir() and os.access(run_hooke, os.W_OK | os.X_OK):
        return run_hooke / f"{user}.sock"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return pathlib.Path(runtime) / "hooke.sock"
    return state_dir() / "hooke.sock"


def ensure_dirs() -> None:
    for d in (state_dir(), run_logs_dir(), sessions_root()):
        d.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: pathlib.Path, data: object) -> None:
    """Write JSON atomically (tmp file + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    payload = json.dumps(data, indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dirfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
