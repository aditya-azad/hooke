"""Agent abstraction: the common interface every automation backend implements.

hooke triggers; the agent does. The executor never talks to pi directly —
it asks :meth:`AgentRunner.get_agent` for the automation's agent and works
only through this interface, so other agents (claude, codex, ...) can be
added later without touching scheduling, watching, or run-record handling.

The contract an agent must satisfy:

- ``build_argv`` returns a subprocess argv that runs ONE automation run
  headlessly and streams machine-readable events (one JSON object per
  line) on stdout.
- ``parse_event`` interprets one stdout line and reports the normalized
  signals the executor depends on: session id (for continuity), whether
  any assistant output was produced (crash vs. failure classification),
  and which files the run wrote (watcher self-trigger suppression).

Per-automation config feeds a ``RunRequest``; agents map the generic knobs
(``model``, ``thinking``, MCP allowlist, session continuity) onto their own
CLI flags and protocols.

There is no registry module: subclassing ``AgentRunner`` with a ``name``
registers the implementation automatically. Concrete implementations live
in sibling modules (``agents/pi.py``); importing this package imports them.
"""

from __future__ import annotations

from .base import AgentEvent, AgentRunner, RunRequest

__all__ = [
    "AgentEvent",
    "AgentRunner",
    "RunRequest",
]

# Importing the implementation modules registers them via __init_subclass__.
from . import pi  # noqa: F401
