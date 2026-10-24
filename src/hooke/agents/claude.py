"""Claude Code agent implementation (https://claude.com/claude-code).

Runs headless print mode with a ``stream-json`` event stream on stdout.
The daemon spawns this with ``cwd`` set to the project root, so Claude
operates on the automation's project by default.

Privilege posture matches the pi default (the agent may use its tools
without asking): ``--dangerously-skip-permissions``. The daemon is
headless — a permission prompt would hang the run until timeout.

Session continuity: Claude persists sessions under ``~/.claude/projects/``
keyed by working directory, so the generic ``session_dir`` knob is unused
here. A fresh continuity run captures the session id from the ``init``
event; later runs pass it back via ``--resume`` (same session id, no fork).
Non-continuity runs pass ``--no-session-persistence`` so scheduled work
does not pollute the user's ``claude --resume`` list.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import AgentEvent, AgentRunner, RunRequest

# Tools whose ``file_path`` argument counts as a file write for watcher
# self-trigger suppression.
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

#: Claude's global config, holding the top-level ``mcpServers`` table.
CLAUDE_JSON = Path.home() / ".claude.json"


class ClaudeAgent(AgentRunner):
    name = "claude"
    mcp_config_hint = "~/.claude.json"

    def build_argv(self, request: RunRequest) -> list[str]:
        argv = [
            "claude",
            "--print",
            request.prompt,
            "--output-format",
            "stream-json",
            "--verbose",  # required by --print --output-format stream-json
            "--dangerously-skip-permissions",
        ]
        if request.model:
            argv += ["--model", request.model]
        if request.thinking:
            argv += ["--effort", request.thinking]
        if request.continuity and request.session_ref:
            argv += ["--resume", request.session_ref]
        elif not request.continuity:
            argv += ["--no-session-persistence"]
        # Claude names MCP tools mcp__<server>__<tool>; the bare server
        # rule denies every tool that server exposes.
        for server in request.excluded_mcp_servers:
            argv += ["--disallowedTools", f"mcp__{server}"]
        return argv

    def parse_event(self, line: str) -> AgentEvent:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return AgentEvent()
        if not isinstance(ev, dict):
            return AgentEvent()
        session_id = (
            ev.get("session_id")
            if ev.get("type") == "system" and ev.get("subtype") == "init"
            else None
        )
        assistant = ev.get("type") == "assistant"
        # Claude exits 0 even when the run failed; the result event's
        # ``is_error`` flag is the reliable failure signal.
        error = None
        if ev.get("type") == "result" and ev.get("is_error"):
            error = str(ev.get("result") or ev.get("subtype") or "run failed")
        writes: list[str] = []
        if assistant:
            content = (ev.get("message") or {}).get("content")
            if isinstance(content, list):
                for item in content:
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "tool_use"
                        and item.get("name") in _WRITE_TOOLS
                    ):
                        p = (item.get("input") or {}).get("file_path")
                        if p:
                            writes.append(str(Path(p).resolve()))
        return AgentEvent(
            session_id=session_id,
            assistant_message=assistant,
            write_paths=tuple(writes),
            error=error,
        )

    def configured_mcp_servers(self) -> tuple[set[str], bool]:
        try:
            data = json.loads(CLAUDE_JSON.read_text())
        except (OSError, json.JSONDecodeError):
            return set(), False
        servers = data.get("mcpServers") or {}
        return set(servers), True
