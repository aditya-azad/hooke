"""Pi agent implementation (https://github.com/badlogic/pi-mono), JSON mode."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from .base import AgentEvent, AgentRunner, RunRequest

#: pi's global MCP config (the only MCP source pi reads).
MCP_JSON = Path.home() / ".pi" / "agent" / "mcp.json"


class PiAgent(AgentRunner):
    name = "pi"
    mcp_config_hint = "~/.pi/agent/mcp.json"

    # Tool calls whose ``path`` argument counts as a file write for watcher
    # self-trigger suppression.
    _WRITE_TOOLS: ClassVar[frozenset[str]] = frozenset(
        {"write", "edit", "multiedit"}
    )

    def build_argv(self, request: RunRequest) -> list[str]:
        argv = ["pi", "--mode", "json", "-p", request.prompt]
        if request.model:
            argv += ["--model", request.model]
        if request.thinking:
            argv += ["--thinking", request.thinking]
        if request.continuity:
            argv += ["--session-dir", str(request.session_dir)]
            if request.session_ref:
                argv += ["--session", request.session_ref]
        else:
            argv += ["--no-session"]
        # pi names MCP tools mcp__<server>__<tool>.
        for server in request.excluded_mcp_servers:
            argv += ["--exclude-tools", f"mcp__{server}__*"]
        return argv

    def parse_event(self, line: str) -> AgentEvent:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return AgentEvent()
        if not isinstance(ev, dict):
            return AgentEvent()
        session_id = ev.get("id") if ev.get("type") == "session" else None
        msg = ev.get("message") or {}
        assistant = (
            ev.get("type") in ("message_start", "message_end")
            and msg.get("role") == "assistant"
        )
        writes: list[str] = []
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "toolCall"
                    and item.get("name") in self._WRITE_TOOLS
                ):
                    p = (item.get("arguments") or {}).get("path")
                    if p:
                        writes.append(str(Path(p).resolve()))
        return AgentEvent(
            session_id=session_id,
            assistant_message=assistant,
            write_paths=tuple(writes),
        )

    def configured_mcp_servers(self) -> tuple[set[str], bool]:
        # pi's config is the source of truth for its own runs; a missing
        # file means zero servers, which deploy validation treats as a
        # hard failure (unchanged historical behavior).
        try:
            data = json.loads(MCP_JSON.read_text())
        except (OSError, json.JSONDecodeError):
            return set(), True
        return set((data.get("mcpServers") or {})), True
