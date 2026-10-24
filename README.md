# Hooke

Agentic workflow automator: a systemd daemon that watches files and schedules periodic tasks, running each automation through the [pi agent](https://github.com/badlogic/pi-mono) with its available MCP servers.

You describe automations in a `hooke.toml` inside any project; a per-user daemon triggers them — on a schedule or when files change — and each run is a real pi agent session with your configured MCP servers, credentials, and project context.

## Install

Requires Python 3.14+ and [pi](https://github.com/badlogic/pi-mono) on PATH.

```sh
# 1. Install the CLI/daemon onto PATH
uv tool install /path/to/hooke        # or: uv tool install hooke (once published)

# 2. Install + start the systemd daemon (templated per user, starts at boot)
hooke install
#   or, for systems with systemd-homed encrypted homes:
hooke install --user                  # user unit + loginctl enable-linger
```

`hooke install` writes `/etc/systemd/system/hooke@<user>.service`, creates
`/run/hooke` via tmpfiles.d, symlinks `~/.local/bin/hooke` (what the unit
executes), and runs `systemctl enable --now hooke@<user>.service`. It will
prompt for your sudo password.

Check it's alive:

```sh
systemctl status hooke@$USER
hooke status              # "daemon 0.1.0 (pid ..., user ...)" means the socket works
```

## Create automations

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
| `mcp_servers` | `[]` | MCP servers this automation may use; must exist in `~/.pi/agent/mcp.json` or deploy fails. |
| `schedule` | — | `{ every = "6h" }` — durations like `90s`, `5m`, `6h`, `1d`. |
| `watch` | — | `{ paths = [...], ignore = [...], debounce = "2s" }` — globs relative to the project root. |
| `resume_on_restart` | `false` | Re-run automatically after a daemon restart interrupted it. |
| `continuity` | `false` | Persist the pi session and reuse it for later runs of this automation. |
| `model` / `thinking` | pi defaults | Per-run overrides (`thinking`: off…max). |
| `timeout` | `30m` | Kill the pi process tree after this long. |
| `concurrency` | `1` | Skip-if-running: overlapping triggers are dropped, never queued. |

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

## Caveats

- **Project trust** is user-approved in pi; the daemon runs headless, so `hooke deploy` warns when the project root is not in `~/.pi/agent/trust.json` — open pi once in that directory and approve, or add the path to `trust.json`.

## Troubleshooting

```sh
journalctl --user -u hooke -f              # --user install
sudo journalctl -u hooke@$USER -f          # system install
```

- `hooke status` says "cannot reach hooke daemon" → the unit isn't running; check `systemctl status hooke@$USER` and the journal above.
- A run shows `failed` → read its event stream: `hooke logs --run <id>`.
- A run shows `timed_out` → raise `timeout` in `hooke.toml` and redeploy.
- Config errors at deploy quote the exact `hooke.toml` problem; fix and redeploy.

## Development

```sh
uv sync
uv run pytest tests -q
uv run ruff check src tests && uv run pyrefly check src tests
uv run hooke daemon        # run the daemon in the foreground
```
