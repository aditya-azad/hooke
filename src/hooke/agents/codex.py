"""Codex CLI agent implementation (https://developers.openai.com/codex).

Runs ``codex exec`` with ``--json`` so one JSONL event per line lands on
stdout. The daemon spawns this with ``cwd`` set to the project root.

Privilege posture: the ``workspace-write`` sandbox (writes inside the
project root and temp dirs, no network). Network access stays off unless
the user enables it in ``~/.codex/config.toml``:

    [sandbox_workspace_write]
    network_access = true

Escalating commands are auto-denied rather than prompting — the daemon is
headless, so nothing may block on approval. ``--skip-git-repo-check`` lets
automations run outside git repositories.

Session continuity: ``codex exec resume <thread_id>`` reopens the session
captured from the ``thread.started`` event of the first run. Non-continuity
runs still record a rollout session in ``~/.codex/sessions`` (codex has no
persistence opt-out for exec); hooke simply ignores it.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from .base import AgentEvent, AgentRunner, RunRequest

#: Codex's global config, holding the ``mcp_servers`` table.
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"


class CodexAgent(AgentRunner):
    name = "codex"
    mcp_config_hint = "~/.codex/config.toml"

    def build_argv(self, request: RunRequest) -> list[str]:
        if request.continuity and request.session_ref:
            # `exec resume` has no --sandbox flag; the config key is the
            # same setting.
            argv = [
                "codex",
                "exec",
                "resume",
                request.session_ref,
                "-c",
                'sandbox_mode="workspace-write"',
            ]
        else:
            argv = ["codex", "exec", "--sandbox", "workspace-write"]
        argv += ["--json", "--skip-git-repo-check"]
        if request.model:
            argv += ["-m", request.model]
        if request.thinking:
            argv += ["-c", f'model_reasoning_effort="{request.thinking}"']
        # Codex has no per-run exclusion flag; disabling each excluded
        # server via a config override is equivalent. (The daemon only
        # excludes servers that exist in the user's config, and dotted
        # `-c` overrides deep-merge into existing entries.)
        for server in request.excluded_mcp_servers:
            argv += ["-c", f"mcp_servers.{server}.enabled=false"]
        argv.append(request.prompt)
        return argv

    def parse_event(self, line: str) -> AgentEvent:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return AgentEvent()
        if not isinstance(ev, dict):
            return AgentEvent()
        session_id = ev.get("thread_id") if ev.get("type") == "thread.started" else None
        assistant = False
        error = None
        if ev.get("type") == "turn.failed":
            err = ev.get("error") or {}
            error = str(err.get("message") or "turn failed") if isinstance(err, dict) else str(err)
        writes: list[str] = []
        item = ev.get("item")
        if isinstance(item, dict):
            if item.get("type") == "agent_message":
                assistant = True
            elif item.get("type") == "file_change":
                for change in item.get("changes") or []:
                    if isinstance(change, dict) and change.get("path"):
                        writes.append(str(Path(change["path"]).resolve()))
        return AgentEvent(
            session_id=session_id,
            assistant_message=assistant,
            write_paths=tuple(writes),
            error=error,
        )

    def configured_mcp_servers(self) -> tuple[set[str], bool]:
        try:
            data = tomllib.loads(CODEX_CONFIG.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            return set(), False
        servers = data.get("mcp_servers") or {}
        return set(servers), True
