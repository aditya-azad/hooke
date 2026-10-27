"""Unit templates bake the deploying shell's PATH in as Environment=PATH."""

from __future__ import annotations

import pytest

from hooke import systemd
from hooke.config import HOOKE_VERSION


def test_templates_format_with_captured_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/opt/tools/bin:/usr/bin")
    path = systemd._unit_path()
    for template in (systemd.UNIT_TEMPLATE, systemd.USER_UNIT_TEMPLATE):
        text = template.format(version=HOOKE_VERSION, path=path)
        assert "Environment=PATH=/opt/tools/bin:/usr/bin" in text


def test_percent_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    # '%' would otherwise be parsed as a systemd specifier (%h etc.)
    monkeypatch.setenv("PATH", "/opt/100%tools/bin:/usr/bin")
    path = systemd._unit_path()
    assert "100%%tools" in path
    # The unit stores '%%'; systemd expands it to '%' at runtime.
    text = systemd.USER_UNIT_TEMPLATE.format(version=HOOKE_VERSION, path=path)
    assert "Environment=PATH=/opt/100%%tools/bin:/usr/bin" in text


def test_unset_path_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PATH", raising=False)
    assert systemd._unit_path() == "/usr/local/bin:/usr/bin"
