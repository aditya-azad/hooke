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
| `prompt` | required | The agent's task. Trigger context (changed files, resume note) is appended automatically. |
| `agent` | `"pi"` | Which registered agent backend runs this automation (see `hooke.agents`; `pi`, `claude`, `codex`). |
| `mcp_servers` | `[]` | MCP servers this automation may use; validated against the agent's own MCP config at deploy (`~/.pi/agent/mcp.json`, `~/.claude.json`, or `~/.codex/config.toml`). |
| `schedule` | — | `{ every = "6h" }` — durations like `90s`, `5m`, `6h`, `1d`. |
| `watch` | — | `{ paths = [...], ignore = [...], debounce = "2s" }` — globs relative to the project root. |
| `resume_on_restart` | `false` | Re-run automatically after a daemon restart interrupted it. |
| `continuity` | `false` | Persist the agent session and reuse it for later runs of this automation. |
| `model` / `thinking` | agent defaults | Per-run overrides passed to the agent (`thinking`: off…max). |
| `timeout` | `30m` | Kill the agent process tree after this long. |
| `concurrency` | `1` | Skip-if-running: overlapping triggers are dropped, never queued. |

Ready-made configurations live in [`examples/`](examples/README.md) — copy one in as `hooke.toml` and deploy.

## Deploy and manage

```sh
hooke deploy              # in the project dir (or: hooke deploy ~/code/my-webapp)
hooke status              # what's armed, next due times, recent runs
hooke logs -n 20          # run history
hooke logs --run <id>     # one run's full record + its pi event stream
hooke disable my-webapp   # kept in the registry, inert
hooke enable my-webapp
hooke remove my-webapp    # gone from the registry
```

`deploy` is the only way the daemon's registry changes. It validates fully first — TOML, schema version, glob sanity, MCP server existence, project trust — so mistakes fail loudly now instead of silently at 3am. An identical redeploy is a no-op ("up to date, nothing to do"); a changed one swaps atomically without resetting unchanged automations' timers.

## Troubleshooting

```sh
journalctl --user -u hooke -f              # --user install
sudo journalctl -u hooke@$USER -f          # system install
```

- `hooke status` says "cannot reach hooke daemon" → the unit isn't running; check `systemctl status hooke@$USER` and the journal above.
- A run shows `failed` → read its event stream: `hooke logs --run <id>`.
- A run shows `timed_out` → raise `timeout` in `hooke.toml` and redeploy.
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
