"""Direct Python-script backend: run a project script in the project venv.

A script automation replaces the agent entirely — no prompt, no model, no
MCP, no session. The executor runs ``<root>/.hooke/venv/bin/python
<script>`` with the trigger context in ``HOOKE_*`` environment variables
(see :meth:`ScriptRunner.child_env`).

Output protocol: stdout/stderr lines are logged to the run log. A script
that writes project files should print one line per written path, prefixed
``@hooke-write:``, so the watcher can suppress those paths from triggering
the same automation again.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .. import scriptenv
from .base import AgentEvent, AgentRunner, RunRequest

#: Prefix marking one written path (watcher self-trigger suppression).
WRITE_PREFIX = "@hooke-write: "


class ScriptRunner(AgentRunner):
    name = "script"
    mcp_config_hint = ""  # no MCP config; scripts cannot allowlist servers

    def build_argv(self, request: RunRequest) -> list[str]:
        root = Path(request.cwd)
        return [str(scriptenv.venv_python(root)), str(root / (request.script or ""))]

    def parse_event(self, line: str) -> AgentEvent:
        if line.startswith(WRITE_PREFIX):
            path = line[len(WRITE_PREFIX) :].strip()
            if path:
                return AgentEvent(write_paths=(str(Path(path).resolve()),))
        if line.strip():
            # Any output counts as "it did something": a nonzero exit with
            # output is a plain failure, not a retryable hard crash.
            return AgentEvent(assistant_message=True)
        return AgentEvent()

    async def prepare(self, request: RunRequest) -> str | None:
        if not request.script:
            return "automation has no 'script' path"
        root = Path(request.cwd)
        if not (root / request.script).is_file():
            return f"script {request.script!r} not found in {root}"
        try:
            await asyncio.to_thread(scriptenv.ensure_venv, root, list(request.dependencies))
        except scriptenv.ScriptEnvError as e:
            return str(e)
        return None

    def child_env(self, request: RunRequest) -> dict[str, str]:
        trigger = (
            json.dumps(request.trigger or {}, sort_keys=True) if request.trigger else ""
        )
        return {
            "HOOKE_PROJECT": request.project or "",
            "HOOKE_AUTOMATION": request.automation or "",
            "HOOKE_TRIGGER_KIND": request.trigger_kind or "",
            "HOOKE_TRIGGER": trigger,
            "HOOKE_ROOT": request.cwd,
            "HOOKE_RUN_ID": request.run_id or "",
            "HOOKE_RESUMED_FROM": request.resumed_from or "",
        }

    def configured_mcp_servers(self) -> tuple[set[str], bool]:
        # Scripts have no MCP config; report "known, empty" so deploy
        # validation never warns about this backend.
        return set(), True
