from __future__ import annotations

import os
import pathlib
import subprocess
import sys

from .config import HOOKE_VERSION

UNIT_TEMPLATE = """\
# Installed by hooke {version} — templated per user.
[Unit]
Description=Hooke automation daemon (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=%i
ExecStart=%h/.local/bin/hooke daemon
Restart=on-failure
RestartSec=5
PrivateTmp=yes
Environment=HOME=%h
Environment=PATH={path}

[Install]
WantedBy=multi-user.target
"""

USER_UNIT_TEMPLATE = """\
# Installed by hooke {version}
[Unit]
Description=Hooke automation daemon
After=network-online.target

[Service]
Type=exec
ExecStart=%h/.local/bin/hooke daemon
Restart=on-failure
RestartSec=5
PrivateTmp=yes
Environment=PATH={path}

[Install]
WantedBy=default.target
"""

TMPFILES_ENTRY = "d /run/hooke 0755 root root -\n"

# systemd's default service PATH (seen in the wild; only used when the
# deploying shell somehow has no PATH set).
_FALLBACK_PATH = "/usr/local/bin:/usr/bin"


def _unit_path() -> str:
    """The PATH to bake into a generated unit.

    Escapes ``%`` as ``%%`` — the unit is a systemd template, and an
    unescaped ``%`` would be parsed as a specifier (``%h`` etc.), not text.
    """
    path = os.environ.get("PATH") or _FALLBACK_PATH
    return path.replace("%", "%%")


def _sh(cmd: list[str], *, may_fail: bool = False) -> bool:
    print(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, check=False)
    if proc.returncode != 0:
        if may_fail:
            return False
        print(f"command failed (exit {proc.returncode})", file=sys.stderr)
        sys.exit(1)
    return True


def _sudo_write(path: str, content: str) -> None:
    proc = subprocess.run(["sudo", "tee", path], input=content.encode(), check=False)
    if proc.returncode != 0:
        print(f"failed to write {path} (sudo required)", file=sys.stderr)
        sys.exit(1)


def _ensure_local_bin() -> pathlib.Path:
    """The unit runs %h/.local/bin/hooke — make sure that resolves to this
    executable even though systemd's PATH is minimal."""
    exe = pathlib.Path(sys.argv[0]).resolve()
    bin_dir = pathlib.Path.home() / ".local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "hooke"
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(exe)
    return target


def install(use_user_unit: bool) -> None:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    link = _ensure_local_bin()
    print(f"hooke binary linked at {link}")

    if use_user_unit:
        unit_dir = pathlib.Path.home() / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True, exist_ok=True)
        unit = unit_dir / "hooke.service"
        unit.write_text(
            USER_UNIT_TEMPLATE.format(version=HOOKE_VERSION, path=_unit_path())
        )
        print(f"wrote {unit}")
        print(
            f"baked deploying shell PATH into unit: {os.environ.get('PATH') or _FALLBACK_PATH}"
        )
        _sh(["systemctl", "--user", "daemon-reload"])
        _sh(["systemctl", "--user", "enable", "--now", "hooke.service"])
        # User units stop at logout unless the user lingers.
        _sh(["loginctl", "enable-linger", user], may_fail=True)
        print(
            "done. caveat: with systemd-homed encrypted homes, this is the only mode that works."
        )
        return

    _sudo_write(
        "/etc/systemd/system/hooke@.service",
        UNIT_TEMPLATE.format(version=HOOKE_VERSION, path=_unit_path()),
    )
    _sudo_write("/etc/tmpfiles.d/hooke.conf", TMPFILES_ENTRY)
    _sh(
        ["sudo", "systemd-tmpfiles", "--create", "/etc/tmpfiles.d/hooke.conf"],
        may_fail=True,
    )
    _sh(["sudo", "systemctl", "daemon-reload"])
    _sh(["sudo", "systemctl", "enable", "--now", f"hooke@{user}.service"])
    print(f"done. daemon: systemctl status hooke@{user}.service")
    print(
        "caveat: if your home directory is encrypted with systemd-homed, "
        "User= services cannot start before first login — use `hooke install --user` "
        "plus `loginctl enable-linger` instead."
    )
