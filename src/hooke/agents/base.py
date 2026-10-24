"""Core datatypes and the AgentRunner interface.

Registration is implicit: subclassing :class:`AgentRunner` with a non-empty
``name`` registers the subclass automatically (via ``__init_subclass__``).
The registry lookup helpers are classmethods on ``AgentRunner`` itself, so
there is no separate registry module or global function namespace.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from ..config import ConfigError


@dataclass(frozen=True)
class RunRequest:
    """Everything one headless agent run needs. Built by the executor."""

    prompt: str
    cwd: str
    model: str | None = None
    thinking: str | None = None
    # Least privilege: servers configured for the user but NOT in the
    # automation's allowlist, to be excluded from this run.
    excluded_mcp_servers: tuple[str, ...] = ()
    # Session continuity: reuse/extend a persisted session across runs.
    continuity: bool = False
    session_dir: Path | None = None
    session_ref: str | None = None


@dataclass(frozen=True)
class AgentEvent:
    """Normalized signals extracted from one line of agent stdout.

    All fields default to "nothing interesting on this line".
    """

    # A persisted session id, as soon as the agent announces one. Persisted
    # to runs.jsonl immediately so boot recovery can reopen the session.
    session_id: str | None = None
    # True when the line evidences assistant output (used to classify a
    # nonzero exit as "failed" vs. retryable "crashed").
    assistant_message: bool = False
    # Absolute paths the run wrote via write-like tools, for watcher
    # self-trigger suppression.
    write_paths: tuple[str, ...] = ()


class AgentRunner(ABC):
    """Common interface for automation backends.

    Subclass this with a unique ``name`` and the subclass is registered
    automatically — that is the whole act of plugging a new agent into
    hooke. Implementations must be constructible without arguments.
    """

    #: Registry key used in hooke.toml (``agent = "<name>"``).
    name: str = ""

    _registry: ClassVar[dict[str, AgentRunner]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.name:  # abstract intermediates stay unregistered
            cls.register()

    @classmethod
    def register(cls) -> None:
        """Register this agent class under its ``name``. Called
        automatically on subclassing; also usable for explicit
        re-registration."""
        if not cls.name:
            raise ValueError(f"{cls.__name__}.name must be a non-empty string")
        existing = AgentRunner._registry.get(cls.name)
        if existing is not None and type(existing) is not cls:
            raise ValueError(
                f"agent name {cls.name!r} already registered by "
                f"{type(existing).__name__}"
            )
        AgentRunner._registry[cls.name] = cls()

    @classmethod
    def get_agent(cls, name: str) -> AgentRunner:
        """Look up a registered agent by config name."""
        agent = AgentRunner._registry.get(name)
        if agent is None:
            raise ConfigError(
                f"unknown agent {name!r} (available: {', '.join(AgentRunner.known_agents())})"
            )
        return agent

    @classmethod
    def known_agents(cls) -> list[str]:
        """All registered agent names, sorted."""
        return sorted(AgentRunner._registry)

    @abstractmethod
    def build_argv(self, request: RunRequest) -> list[str]:
        """Argv for one headless run of ``request``."""

    def parse_event(self, line: str) -> AgentEvent:
        """Interpret one stdout line. The base implementation ignores
        everything; agents with structured output override this."""
        return AgentEvent()
