"""Persistent state: registry.json, schedule-state.json, runs.jsonl.

- registry.json and schedule-state.json are whole-file, atomically replaced
  (tmp + fsync + rename).
- runs.jsonl is append-only. Updates append a fresh record with the same
  ``run_id``; the latest record for an id wins. This makes the recovery
  pass trustworthy: a daemon death between "marked running" and "child
  finished" leaves an observable stale record behind.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import paths
from .paths import atomic_write_json

OPEN_RUN_STATUSES = ("pending", "running")


def _load_json(path: Path, default: object) -> object:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        # Corrupt state file: keep the daemon bootable, back the bad file up.
        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            os.replace(path, backup)
        except OSError:
            pass
        return default


class RegistryStore:
    def __init__(self) -> None:
        data = _load_json(paths.registry_path(), {"version": 1, "projects": {}})
        if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
            data = {"version": 1, "projects": {}}
        self.data: dict = data

    def projects(self) -> dict:
        return self.data["projects"]

    def get_project(self, name: str) -> dict | None:
        return self.projects().get(name)

    def upsert(self, project: dict) -> None:
        self.projects()[project["name"]] = project
        self.save()

    def remove(self, name: str) -> bool:
        if name not in self.projects():
            return False
        del self.projects()[name]
        self.save()
        return True

    def set_disabled(self, name: str, disabled: bool) -> dict | None:
        proj = self.get_project(name)
        if proj is None:
            return None
        proj["disabled"] = disabled
        self.save()
        return proj

    def save(self) -> None:
        self.data["version"] = 1
        self.data["saved_at"] = time.time()
        atomic_write_json(paths.registry_path(), self.data)


class ScheduleState:
    """``{ "<project>/<automation>": {"last_fired": float, "next_due": float} }``"""

    def __init__(self) -> None:
        data = _load_json(paths.schedule_state_path(), {})
        self.data: dict[str, dict] = data if isinstance(data, dict) else {}

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def set(self, key: str, last_fired: float | None, next_due: float) -> None:
        self.data[key] = {"last_fired": last_fired, "next_due": next_due}
        self.save()

    def pop(self, key: str) -> None:
        if key in self.data:
            del self.data[key]
            self.save()

    def save(self) -> None:
        atomic_write_json(paths.schedule_state_path(), self.data)


class RunStore:
    def __init__(self) -> None:
        self.path = paths.runs_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a")  # noqa: SIM115 - long-lived append handle, closed in close()

    def append(self, record: dict) -> dict:
        self._fh.write(json.dumps(record, sort_keys=True) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return record

    def load_latest(self) -> dict[str, dict]:
        """All runs, keyed by run_id. Records for one id are merged in
        append order — updates are partial patches, so the merged view is
        the run's full state."""
        merged: dict[str, dict] = {}
        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict) and "run_id" in rec:
                        merged.setdefault(rec["run_id"], {}).update(rec)
        except FileNotFoundError:
            return {}
        return merged

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass
