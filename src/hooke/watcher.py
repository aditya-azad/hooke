"""Watcher: file-triggered automations via watchfiles.

- Debounce window: coalesce event bursts (a `git pull` fires dozens of
  events) into one trigger.
- The run's prompt gets a manifest of what changed (added/modified/removed).
- Self-trigger suppression: while automation A's run is in flight, events
  on paths A itself wrote (tracked via pi tool events) are suppressed,
  plus content-hash dedupe of unchanged files.
- Events arriving during a run collapse into a single pending trigger.
"""

from __future__ import annotations

import asyncio
import glob as globmod
import hashlib
import re
import time
from pathlib import Path

from watchfiles import Change, awatch

from .executor import Executor, Job

MAX_DEBOUNCE_CAP = 60.0  # never defer a trigger forever under continuous writes
_READ_CHUNK = 1 << 20


def _glob_matcher(patterns: list[str]):
    regexes = [re.compile(globmod.translate(p)) for p in patterns]
    return regexes


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _file_hash(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(_READ_CHUNK):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


class WatcherSet:
    """Owns one asyncio task per watch automation; deploy swaps rebuild it."""

    def __init__(self, executor: Executor) -> None:
        self.executor = executor
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.stop_events: dict[tuple[str, str], asyncio.Event] = {}
        self.last_hashes: dict[tuple[str, str], dict[str, str]] = {}

    async def arm(self, key: tuple[str, str], job: Job) -> None:
        self.disarm(key)
        stop = asyncio.Event()
        self.stop_events[key] = stop
        self.tasks[key] = asyncio.create_task(
            self._watch_loop(key, job, stop), name=f"hooke-watch-{key[0]}/{key[1]}"
        )

    def disarm(self, key: tuple[str, str]) -> None:
        ev = self.stop_events.pop(key, None)
        task = self.tasks.pop(key, None)
        self.last_hashes.pop(key, None)
        if ev:
            ev.set()
        if task:
            task.cancel()

    def disarm_all(self) -> None:
        for key in list(self.tasks):
            self.disarm(key)

    async def _watch_loop(self, key: tuple[str, str], job: Job, stop: asyncio.Event) -> None:
        cfg = job.cfg["watch"]
        root = Path(job.root)
        include = _glob_matcher(cfg["paths"])
        ignore = _glob_matcher(cfg["ignore"])
        debounce = float(cfg["debounce"])
        last_hashes = self.last_hashes.setdefault(key, {})

        def relevant(change: Change, path_str: str) -> bool:
            rel = _rel(Path(path_str), root)
            if any(r.fullmatch(rel) for r in ignore):
                return False
            return any(r.fullmatch(rel) for r in include)

        batches: asyncio.Queue[list[tuple[Change, str]]] = asyncio.Queue()
        watch_task = asyncio.create_task(self._raw_watch(job, root, stop, batches))
        try:
            while True:
                batch = await batches.get()
                if batch is None:
                    return
                # Debounce: keep collecting until the stream goes quiet for
                # `debounce` seconds, capped so a continuous writer can't
                # starve the automation.
                deadline = time.monotonic() + min(max(debounce * 10, debounce), MAX_DEBOUNCE_CAP)
                while time.monotonic() < deadline:
                    try:
                        more = await asyncio.wait_for(batches.get(), timeout=debounce)
                        if more is None:
                            return
                        batch.extend(more)
                    except TimeoutError:
                        break
                manifest = self._coalesce(batch, root, relevant, last_hashes)
                if manifest is None:
                    continue
                # Self-trigger suppression: drop paths this automation's own
                # run recently wrote.
                writes = self.executor.recent_writes.get(key) or {}
                now = time.time()
                for kind in ("added", "modified", "removed"):
                    manifest[kind] = [p for p in manifest[kind] if p not in writes]
                for p in list(writes):
                    if writes[p] < now:
                        del writes[p]
                if not any(manifest.values()):
                    continue
                await self.executor.enqueue(
                    Job(
                        job.project,
                        job.automation,
                        job.cfg,
                        job.root,
                        "watch",
                        {"files": manifest},
                    )
                )
        finally:
            watch_task.cancel()

    async def _raw_watch(self, job: Job, root: Path, stop: asyncio.Event, out) -> None:
        try:
            async for changes in awatch(root, watch_filter=None, stop_event=stop):
                out.put_nowait([(c, str(p)) for c, p in changes])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a dead watcher must stop its loop, not kill the daemon
            out.put_nowait(None)  # signal the loop to exit

    def _coalesce(self, batch, root, relevant, last_hashes):
        """Fold a raw event batch into {added, modified, removed} of matching
        rel paths, deduping files whose content hash did not change."""
        paths_by_change: dict[str, set[str]] = {}
        for change, path_str in batch:
            if not relevant(change, path_str):
                continue
            rel = _rel(Path(path_str), root)
            kind = {
                Change.added: "added",
                Change.modified: "modified",
                Change.deleted: "removed",
            }.get(change)
            if kind:
                paths_by_change.setdefault(kind, set()).add(rel)
        manifest: dict[str, list[str]] = {"added": [], "modified": [], "removed": []}
        for kind, rels in paths_by_change.items():
            for rel in sorted(rels):
                if kind == "removed":
                    last_hashes.pop(rel, None)
                    manifest["removed"].append(rel)
                    continue
                digest = _file_hash(root / rel)
                if digest is None:
                    continue  # unreadable (race with deletion) — skip
                if last_hashes.get(rel) == digest:
                    continue  # content-hash dedupe
                last_hashes[rel] = digest
                bucket = "modified" if rel not in manifest["added"] else None
                if bucket:
                    manifest[bucket].append(rel)
        # A path can appear as added and modified within one burst; prefer added.
        if manifest["added"]:
            manifest["modified"] = [
                p for p in manifest["modified"] if p not in set(manifest["added"])
            ]
        if not any(manifest.values()):
            return None
        return manifest
