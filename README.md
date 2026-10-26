# Hooke

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](pyproject.toml)
[![Code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Agentic workflow automator: a systemd daemon that watches files and schedules periodic tasks, running each automation through your agent with its available MCP servers.

<details>
<summary>Table of contents</summary>

- [Why](#why)
- [Install](#install)
- [Quick start](#quick-start)
- [Script automations (no LLM)](#script-automations-no-llm)
- [Deploy and manage](#deploy-and-manage)
- [Troubleshooting](#troubleshooting)
- [Support](#support)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)
- [Development](#development)

</details>

## Why

Every project accumulates chores that an agent could do if it just knew when to start: "run the tests whenever `src/` changes", "summarize new notes every morning", "check dependencies daily". Cron handles *when* but not *what* — it has no model, no tools, no context. Hooke gives you the cron half for free (systemd, already running) and hands each trigger to a real agent session with your project's context and your configured MCP servers.

## Install

Requires Python 3.14+ and [pi](https://github.com/earendil-works/pi) or other supported agents on PATH.

1. Install the CLI/daemon onto PATH

```sh
uv tool install /path/to/hooke        # or: uv tool install hooke (once published)
```

2. Install + start the systemd daemon (templated per user, starts at boot)

```sh
hooke install
hooke install --user                  # user unit + loginctl enable-linger
```

Check it's alive — you should see the version and a pid:

```sh
$ systemctl status hooke@$USER
````

```sh
$ hooke status
```

## Quick start

In any project directory:

```sh
cd ~/code/my-webapp
hooke init                # writes a commented hooke.toml
```

Edit `hooke.toml`:

```toml
[project]
name = "my-webapp"                 # unique id, collisions rejected at deploy
schema = 1

[automation.deps-review]
prompt = "Check package manifests for outdated or vulnerable dependencies and summarize findings in DEPENDENCIES.md"
agent = "pi"                       # which agent runs this automation (default: pi)
model = "claude-opus-4-5"          # optional per-automation model override
mcp_servers = ["papers"]           # explicit allowlist — least privilege
schedule = { every = "6h" }
resume_on_restart = false
continuity = true                  # reuse the persisted pi session between runs

[automation.test-on-change]
prompt = "Source files changed. Run the test suite and fix any failures you introduced."
watch = { paths = ["src/**", "tests/**"], ignore = ["**/.git/**", "**/node_modules/**"], debounce = "2s" }
resume_on_restart = true
```

Per-automation fields:

| Field | Default | Meaning |
|---|---|---|
| `prompt` | required (unless `script`) | The agent's task. Trigger context (changed files, resume note) is appended automatically. |
| `agent` | `"pi"` | Which registered backend runs this automation (see `hooke.agents`; `pi`, `claude`, `codex`). |
| `mcp_servers` | `[]` | MCP servers this automation may use; validated against the agent's own MCP config at deploy (`~/.pi/agent/mcp.json`, `~/.claude.json`, or `~/.codex/config.toml`). |
| `schedule` | — | `{ every = "6h" }` — durations like `90s`, `5m`, `6h`, `1d`. |
| `watch` | — | `{ paths = [...], ignore = [...], debounce = "2s" }` — globs relative to the project root. |
| `resume_on_restart` | `false` | Re-run automatically after a daemon restart interrupted it. |
| `continuity` | `false` | Persist the agent session and reuse it for later runs of this automation. |
| `model` / `thinking` | agent defaults | Per-run overrides passed to the agent (`thinking`: off…max). |
| `timeout` | `30m` | Kill the agent process tree after this long. |
| `concurrency` | `1` | Skip-if-running: overlapping triggers are dropped, never queued. |
| `script` | — | Run a project Python file directly instead of an agent — see [Script automations](#script-automations-no-llm). |
| `dependencies` | `[]` | Requirements installed into the project venv for script automations. |

Ready-made configurations live in [`examples/`](examples/README.md) — copy one in as `hooke.toml` and deploy.

## Script automations (no LLM)

Not every chore needs a model. When an automation sets `script` instead of `prompt`, hooke runs that Python file directly:

```toml
[automation.disk-report]
script = "scripts/disk_report.py"   # relative to the project root
dependencies = ["httpx", "rich"]   # optional
schedule = { every = "1d" }
```

- **Venv:** each script runs with `<project>/.hooke/venv/bin/python` — a dedicated venv in the same directory as `hooke.toml`, created on demand with the hooke interpreter and shared by all of the project's script automations. `deploy` builds the venv and installs `dependencies` eagerly so failures surface immediately; before each run the daemon re-syncs (a no-op when the dependency set is unchanged) and rebuilds the venv if it was deleted.
- **Trigger context:** the script gets `HOOKE_PROJECT`, `HOOKE_AUTOMATION`, `HOOKE_TRIGGER_KIND`, `HOOKE_TRIGGER` (the trigger payload as JSON, e.g. the changed-file manifest for watchers), `HOOKE_ROOT`, `HOOKE_RUN_ID`, and `HOOKE_RESUMED_FROM` in its environment.
- **Output:** stdout/stderr are captured into the run log (`hooke logs --run <id>`). A nonzero exit is a failed run; a script that exits zero without producing output is treated as a hard crash and retried.
- **Watcher friendliness:** if the automation is file-triggered and the script writes project files, print one line per written path — `@hooke-write: <absolute path>` — so those paths are suppressed from re-triggering the same automation. Watchers also always ignore `.hooke/**`.
- **Not allowed with `script`:** `prompt`, `agent`, `model`, `thinking`, `continuity`, and `mcp_servers` — scripts get no model, no tools, no session.

## Deploy and manage

```sh
hooke deploy              # in the project dir (or: hooke deploy ~/code/my-webapp)
hooke status              # what's armed, next due times, recent runs
hooke logs -n 20          # run history
hooke logs --run <id>     # one run's full record + its pi event stream
hooke stop --run <id>     # kill a run's agent process tree now
hooke disable my-webapp   # kept in the registry, inert
hooke enable my-webapp
hooke remove my-webapp    # gone from the registry
```

`deploy` is the only way the daemon's registry changes. It validates fully first — TOML, schema version, glob sanity, script existence, venv + dependency install, MCP server existence, project trust — so mistakes fail loudly now instead of silently at 3am. An identical redeploy is a no-op ("up to date, nothing to do"); a changed one swaps atomically without resetting unchanged automations' timers.

### Stopping runs

A run normally ends on its own, but you can stop active runs at any time:

```sh
hooke stop --run <id>            # one run
hooke stop my-webapp             # all active runs of a project
hooke stop my-webapp --automation test-on-change   # one automation's active runs
```

Running runs get their process tree killed immediately; queued runs are cancelled before they start; runs parked in retry backoff are cancelled too. Each targeted run is recorded as `stopped` in the run log, so `hooke logs` shows exactly what was interrupted. A stopped run never retries.

## Troubleshooting

```sh
journalctl --user -u hooke -f              # --user install
sudo journalctl -u hooke@$USER -f          # system install
```

- `hooke status` says "cannot reach hooke daemon" → the unit isn't running; check `systemctl status hooke@$USER` and the journal above.
- A run shows `failed` → read its event stream: `hooke logs --run <id>`.
- A run shows `timed_out` → raise `timeout` in `hooke.toml` and redeploy.
- A run is wedged (or you just want it gone now) → `hooke stop --run <id>`; the killed run is recorded as `stopped` and never retries.
- Config errors at deploy quote the exact `hooke.toml` problem; fix and redeploy.

## Support

- **Bugs and questions:** [open an issue](https://github.com/aditya-azad/hooke/issues) — include your `hooke.toml` fragment, the daemon version, and the run's output from `hooke logs --run <id>` (redact anything private).

## Roadmap

- [ ] Publish to PyPI (`uv tool install hooke`)
- [ ] Boot-time catch-up scan of watched globs (currently fires only on the next change)
- [ ] `hooke run` — trigger an automation once, on demand
- [ ] Per-automation notification hooks (e.g. notify on `failed`/`timed_out`)

## Contributing

PRs are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the test/lint/type commands CI runs, and how to submit changes.

## License

[MIT](LICENSE) — see the [LICENSE](LICENSE) file.

## Development

```sh
uv sync
uv run pytest tests -q
uv run ruff check src tests && uv run pyrefly check src tests
uv run hooke daemon        # run the daemon in the foreground
```
