"""`hooke run` daemon op: manual triggers queue jobs like schedule/watch."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from hooke import daemon as daemon_mod


def _auto(name: str) -> dict:
    return {"name": name, "agent": "script", "script": "scripts/x.py", "schedule": {"every": "1d"}}


def _daemon(projects: dict, run_ids: dict[str, str | None]):
    d = object.__new__(daemon_mod.Daemon)
    d.registry = SimpleNamespace(get_project=lambda name: projects.get(name))

    async def enqueue(job) -> str | None:
        return run_ids.get(job.automation)

    d.executor = SimpleNamespace(enqueue=enqueue)
    return d


def _projects() -> dict:
    return {
        "p": {
            "name": "p",
            "root": "/tmp/p",
            "disabled": False,
            "automations": {"a1": _auto("a1"), "a2": _auto("a2")},
        },
        "off": {
            "name": "off",
            "root": "/tmp/off",
            "disabled": True,
            "automations": {"a1": _auto("a1")},
        },
    }


def test_run_all_automations() -> None:
    d = _daemon(_projects(), {"a1": "r1", "a2": "r2"})
    reply = asyncio.run(d._op_run({"op": "run", "project": "p"}))
    assert reply == {"ok": True, "queued": ["a1", "a2"], "skipped": []}


def test_run_single_automation() -> None:
    d = _daemon(_projects(), {"a2": "r2"})
    reply = asyncio.run(d._op_run({"op": "run", "project": "p", "automation": "a2"}))
    assert reply == {"ok": True, "queued": ["a2"], "skipped": []}


def test_run_unknown_project() -> None:
    d = _daemon(_projects(), {})
    reply = asyncio.run(d._op_run({"op": "run", "project": "nope"}))
    assert not reply["ok"]


def test_run_unknown_automation() -> None:
    d = _daemon(_projects(), {})
    reply = asyncio.run(d._op_run({"op": "run", "project": "p", "automation": "nope"}))
    assert not reply["ok"]


def test_run_disabled_project() -> None:
    d = _daemon(_projects(), {"a1": "r1"})
    reply = asyncio.run(d._op_run({"op": "run", "project": "off"}))
    assert not reply["ok"] and "disabled" in reply["error"]


def test_run_skip_if_running() -> None:
    # enqueue returns None when the concurrency limit already holds.
    d = _daemon(_projects(), {"a1": None, "a2": "r2"})
    reply = asyncio.run(d._op_run({"op": "run", "project": "p"}))
    assert reply == {"ok": True, "queued": ["a2"], "skipped": ["a1"]}


def test_run_missing_project_arg() -> None:
    d = _daemon(_projects(), {})
    reply = asyncio.run(d._op_run({"op": "run"}))
    assert not reply["ok"]


def test_run_manual_trigger_kind() -> None:
    captured = {}

    async def enqueue(job) -> str | None:
        captured["job"] = job
        return "rid"

    d = _daemon(_projects(), {})
    d.executor = SimpleNamespace(enqueue=enqueue)
    reply = asyncio.run(d._op_run({"op": "run", "project": "p", "automation": "a1"}))
    assert reply["ok"]
    job = captured["job"]
    assert (job.project, job.automation, job.trigger_kind, job.trigger) == ("p", "a1", "manual", None)
    assert job.cfg["excluded_mcp_servers"] == []  # script backend: nothing to exclude


@pytest.mark.parametrize("bad", [{"op": "run"}, {"op": "run", "project": ""}])
def test_run_rejects_bad_project(bad: dict) -> None:
    d = _daemon(_projects(), {})
    reply = asyncio.run(d._op_run(bad))
    assert not reply["ok"]
