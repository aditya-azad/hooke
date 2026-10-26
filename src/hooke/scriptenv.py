"""Project script environment: the venv script automations run in.

A script automation runs a Python file with the project's dedicated venv
(``<root>/.hooke/venv``), created on demand with hooke's own interpreter.
Each automation's ``dependencies`` list is installed into that venv before
every run — pip is a no-op when everything is already satisfied, and a
marker file records the installed dependency set so unchanged dependencies
skip even the pip call. The venv is shared by all of a project's script
automations (installs are additive).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

VENV_DIRNAME = ".hooke"
DEPS_MARKER = "hooke-deps.txt"

_ERROR_TAIL_LINES = 10


class ScriptEnvError(Exception):
    """Venv creation or dependency install failed."""


def venv_dir(root: Path) -> Path:
    return Path(root) / VENV_DIRNAME / "venv"


def marker_path(root: Path) -> Path:
    """Path of the metadata marker recording which deps are installed."""
    return venv_dir(root) / DEPS_MARKER


def venv_python(root: Path) -> Path:
    return venv_dir(root) / "bin" / "python"


def deps_signature(deps: list[str] | tuple[str, ...]) -> str:
    return hashlib.sha256(("\n".join(sorted(deps)) + "\n").encode()).hexdigest()


def _run(cmd: list[str], what: str) -> None:
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-_ERROR_TAIL_LINES:]
        raise ScriptEnvError(
            f"{what} failed (exit {proc.returncode})"
            + (":\n" + "\n".join(tail) if tail else "")
        )


def ensure_venv(root: Path, dependencies: list[str] | tuple[str, ...] = ()) -> None:
    """Create the project venv if needed and sync it with ``dependencies``.

    Blocking; the executor calls it via ``asyncio.to_thread`` and the CLI
    at deploy time.
    """
    root = Path(root)
    py = venv_python(root)
    if not py.is_file():
        vdir = venv_dir(root)
        if vdir.exists():
            shutil.rmtree(vdir)  # half-created venv: rebuild from scratch
        vdir.parent.mkdir(parents=True, exist_ok=True)
        _run([sys.executable, "-m", "venv", str(vdir)], f"venv creation for {vdir}")
        if not py.is_file():
            raise ScriptEnvError(f"venv created but {py} is missing")
    wanted = deps_signature(dependencies)
    marker = venv_dir(root) / DEPS_MARKER
    if marker.is_file() and marker.read_text().strip() == wanted:
        return  # same dependency set already installed
    if dependencies:
        cmd = [
            str(py),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            *dependencies,
        ]
        _run(cmd, f"dependency install into {venv_dir(root)}")
    marker.write_text(wanted + "\n")
