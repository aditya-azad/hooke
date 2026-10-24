# Contributing to Hooke

Thank you for considering a contribution! This document walks you through building, testing, and submitting changes — written so you can follow it on a fresh machine with no prior exposure to the codebase.

## Prerequisites

- **Python 3.14+**
- **[uv](https://docs.astral.sh/uv/)** — Python package/project manager
- **[pi](https://github.com/badlogic/pi-mono)** on PATH — only needed to actually *run* automations; tests don't invoke it

## Set up your environment

```sh
git clone https://github.com/aditya-azad/hooke.git
cd hooke
uv sync          # creates .venv and installs hooke + dev dependencies
```

## Run the checks

```sh
uv run pytest tests -q                        # test suite
uv run ruff check src tests                   # lint
uv run pyrefly check src tests                # type check
```

CI runs exactly these three commands — if they pass locally, your PR should pass CI. Please run them before opening a PR.

## Try your changes against a real daemon

```sh
uv run hooke daemon        # foreground daemon (safe: use a scratch project dir)
```

In another terminal, point the CLI at a scratch project and exercise the flow end to end:

```sh
cd /tmp/scratch-project
uv --project /path/to/hooke run hooke init
# edit hooke.toml: give the automation a harmless prompt, e.g. "list files in ./docs"
uv --project /path/to/hooke run hooke deploy
uv --project /path/to/hooke run hooke status
```

## Project layout

```
src/hooke/
  cli.py          stateless CLI: init, deploy, status, logs, disable/enable/remove
  daemon.py       the asyncio daemon: socket server, recovery, wiring
  scheduler.py    periodic triggers (persisted last-fired timestamps)
  watcher.py      file watchers (debounce, change manifests)
  executor.py     run queue, worker pool, timeouts
  agents/         agent backends (pi, claude, codex)
  stores.py       registry / run history / schedule state persistence
  protocol.py     Unix-socket JSONL framing
  systemd.py      `hooke install` unit generation
tests/            pytest suite
```

## Submitting changes

1. Fork the repo and create a branch from `main`.
2. Make your change. Add tests for anything behavior-changing.
3. Run the three checks above; keep them green.
4. Open a pull request with a short description of *what* changed and *why*.

For bugs: open an [issue](https://github.com/aditya-azad/hooke/issues) with the `hooke.toml` fragment, the daemon version, and the relevant run's output from `hooke logs --run <id>` (redact anything private).
