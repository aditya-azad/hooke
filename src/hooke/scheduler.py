"""Scheduler: one asyncio task per scheduled automation.

Sleeps until next_due, records the fire, enqueues a run, recomputes.
schedule-state.json is updated on every fire *before* the run is enqueued,
so a crash at any instant leaves recoverable truth on disk. Missed windows
never stack up: next fire = last fire + interval, never a backlog.
"""

from __future__ import annotations

import asyncio
import random
import time

from .executor import Executor, Job
from .stores import ScheduleState

BOOT_JITTER_MAX = 30.0


async def scheduler_loop(
    key: str,
    job: Job,
    schedule_state: ScheduleState,
    executor: Executor,
) -> None:
    interval = job.cfg["schedule"]["every"]
    while True:
        state = schedule_state.get(key) or {}
        next_due = float(state.get("next_due") or 0.0)
        now = time.time()
        if next_due > now:
            await asyncio.sleep(min(next_due - now, 3600.0))
            continue
        # Fire: persist first, enqueue second.
        schedule_state.set(key, last_fired=now, next_due=now + interval)
        await executor.enqueue(job)


def boot_next_due(
    last_fired: float | None, interval: float, now: float | None = None
) -> tuple[float | None, float]:
    """Compute (last_fired, next_due) for boot/deploy.

    - No history: due immediately (a brand-new automation fires once after
      boot jitter).
    - History: next_due = last_fired + interval; if that is in the past,
      fire once at now + jitter (0-30s so ten automations don't stampede
      pi at boot). Never a backlog of missed windows.
    """
    now = now if now is not None else time.time()
    if last_fired is None:
        return None, now + random.uniform(0, BOOT_JITTER_MAX)
    next_due = last_fired + interval
    if next_due <= now:
        next_due = now + random.uniform(0, BOOT_JITTER_MAX)
    return last_fired, next_due
