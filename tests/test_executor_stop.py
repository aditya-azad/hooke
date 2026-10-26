"""Tests for executor.stop(): queued runs are cancelled at dequeue,
running runs are killed and recorded as stopped."""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from hooke import paths
from hooke.executor import Executor, Job
from hooke.stores import RunStore


def make_job(tmp_path: pathlib.Path, project: str = "p", automation: str = "a") -> Job:
    return Job(project, automation, {}, str(tmp_path), "schedule", None)


def run_status(run_store: RunStore, run_id: str) -> dict:
    merged: dict = {}
    for rec in run_store.load_latest().values():
        if rec["run_id"] == run_id:
            merged.update(rec)
    return merged


def test_stop_cancels_queued_run(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_stop_cancels_queued_run(tmp_path, monkeypatch))


async def _stop_cancels_queued_run(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "runs_path", lambda: tmp_path / "runs.jsonl")
    run_store = RunStore()
    ex = Executor(run_store, max_concurrent=1)
    calls: list[str] = []

    async def fake_spawn(self, job, run_id, session_ref, resumed_from, attempt):
        calls.append(run_id)
        return "finished", 0, None, [], None

    monkeypatch.setattr(Executor, "_spawn_agent", fake_spawn)
    run_id = await ex.enqueue(make_job(tmp_path))
    assert run_id
    ex.start_workers()
    try:
        result = await ex.stop(run_id=run_id)
        assert result["pending"] == [run_id]
        await ex.queue.join()
        assert calls == []  # never started
        rec = run_status(run_store, run_id)
        assert rec["status"] == "stopped"
        assert "queued" in (rec["error"] or "")
        assert ex.active_counts.get(("p", "a")) == 0
    finally:
        for w in ex.workers:
            w.cancel()


def test_stop_kills_running_run(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_stop_kills_running_run(tmp_path, monkeypatch))


async def _stop_kills_running_run(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paths, "runs_path", lambda: tmp_path / "runs.jsonl")
    run_store = RunStore()
    ex = Executor(run_store, max_concurrent=1)
    runner = tmp_path / "sleeper.py"
    runner.write_text("import time; time.sleep(60)\n")

    async def fake_spawn(self, job, run_id, session_ref, resumed_from, attempt):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(runner), start_new_session=True
        )
        self.processes[run_id] = proc
        try:
            await proc.wait()
        finally:
            self.processes.pop(run_id, None)  # mirror the real _spawn_agent
        return "failed", proc.returncode, "killed", [], None

    monkeypatch.setattr(Executor, "_spawn_agent", fake_spawn)
    run_id = await ex.enqueue(make_job(tmp_path))
    assert run_id
    ex.start_workers()
    try:
        for _ in range(100):
            if run_id in ex.processes:
                break
            await asyncio.sleep(0.05)
        assert run_id in ex.processes  # run is live
        result = await ex.stop(run_id=run_id)
        assert result["running"] == [run_id]
        await ex.queue.join()
        assert ex.processes == {}  # worker reaped it
        rec = run_status(run_store, run_id)
        assert rec["status"] == "stopped"
        assert rec["exit_code"] is not None  # killed, not natural exit
        assert ex.active_counts.get(("p", "a")) == 0
    finally:
        for proc in list(ex.processes.values()):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        for w in ex.workers:
            w.cancel()
