"""hooke.toml parsing, normalization, and validation.

The normalized form is what gets hashed, shipped over the deploy protocol,
and stored in the daemon's registry. Durations are compiled to seconds,
globs are validated by compiling them, so the daemon never re-parses TOML.
"""

from __future__ import annotations

import glob as globmod
import hashlib
import json
import re
import tomllib
from pathlib import Path

SCHEMA_VERSION = 1
PROTOCOL_VERSION = 1
HOOKE_VERSION = "0.1.0"

DEFAULT_DEBOUNCE = 2.0
DEFAULT_TIMEOUT = 30 * 60.0
DEFAULT_CONCURRENCY = 1
DEFAULT_MAX_CONCURRENT_RUNS = 2

THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_AUTOMATION_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])$")
_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class ConfigError(Exception):
    """Validation failure with a user-facing message."""


def parse_duration(value: object, what: str) -> float:
    """Parse '90s' / '5m' / '6h' / '1d' into seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        secs = float(value)
    elif isinstance(value, str):
        m = _DURATION_RE.match(value.strip())
        if not m:
            raise ConfigError(f"{what}: expected '<n>s'|'<n>m'|'<n>h'|'<n>d', got {value!r}")
        secs = float(m.group(1)) * _UNIT_SECONDS[m.group(2)]
    else:
        raise ConfigError(f"{what}: expected duration string like '30m', got {value!r}")
    if secs <= 0:
        raise ConfigError(f"{what}: must be positive, got {value!r}")
    return secs


def _validate_glob(pattern: object, what: str) -> str:
    if not isinstance(pattern, str) or not pattern.strip():
        raise ConfigError(f"{what}: must be a non-empty glob string, got {pattern!r}")
    try:
        globmod.translate(pattern)
    except ValueError as e:
        raise ConfigError(f"{what}: invalid glob {pattern!r}: {e}") from e
    return pattern


def _norm_watch(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("watch: must be a table")
    paths = raw.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ConfigError("watch.paths: must be a non-empty list of globs")
    paths = [_validate_glob(p, f"watch.paths[{i}]") for i, p in enumerate(paths)]
    ignore_raw = raw.get("ignore", [])
    if not isinstance(ignore_raw, list):
        raise ConfigError("watch.ignore: must be a list of globs")
    ignore = [_validate_glob(p, f"watch.ignore[{i}]") for i, p in enumerate(ignore_raw)]
    debounce_raw = raw.get("debounce", DEFAULT_DEBOUNCE)
    debounce = parse_duration(debounce_raw, "watch.debounce")
    return {"paths": paths, "ignore": ignore, "debounce": debounce}


def _norm_schedule(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("schedule: must be a table")
    if "every" not in raw:
        raise ConfigError("schedule.every: required (e.g. every = \"6h\")")
    every = parse_duration(raw["every"], "schedule.every")
    if every < 1.0:
        raise ConfigError("schedule.every: must be at least 1s")
    return {"every": every}


def normalize_automation(name: str, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError(f"automation.{name}: must be a table")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ConfigError(f"automation.{name}.prompt: required, must be a non-empty string")

    mcp = raw.get("mcp_servers", [])
    if not isinstance(mcp, list) or not all(isinstance(s, str) and s for s in mcp):
        raise ConfigError(f"automation.{name}.mcp_servers: must be a list of server names")
    if len(set(mcp)) != len(mcp):
        raise ConfigError(f"automation.{name}.mcp_servers: duplicate entries")

    schedule = _norm_schedule(raw.get("schedule"))
    watch = _norm_watch(raw.get("watch"))
    if schedule is None and watch is None:
        raise ConfigError(
            f"automation.{name}: needs a trigger — add 'schedule' or 'watch'"
        )

    resume = raw.get("resume_on_restart", False)
    if not isinstance(resume, bool):
        raise ConfigError(f"automation.{name}.resume_on_restart: must be a boolean")
    continuity = raw.get("continuity", False)
    if not isinstance(continuity, bool):
        raise ConfigError(f"automation.{name}.continuity: must be a boolean")

    model = raw.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigError(f"automation.{name}.model: must be a non-empty string")

    thinking = raw.get("thinking")
    if thinking is not None and (
        not isinstance(thinking, str) or thinking not in THINKING_LEVELS
    ):
            raise ConfigError(
                f"automation.{name}.thinking: must be one of {', '.join(THINKING_LEVELS)}"
            )

    timeout = parse_duration(raw.get("timeout", DEFAULT_TIMEOUT), f"automation.{name}.timeout")
    concurrency = raw.get("concurrency", DEFAULT_CONCURRENCY)
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency < 1:
        raise ConfigError(f"automation.{name}.concurrency: must be an integer >= 1")

    return {
        "name": name,
        "prompt": prompt,
        "mcp_servers": list(mcp),
        "schedule": schedule,
        "watch": watch,
        "resume_on_restart": resume,
        "continuity": continuity,
        "model": model,
        "thinking": thinking,
        "timeout": timeout,
        "concurrency": concurrency,
    }


def normalize_project(raw: object, automations_raw: object, root: Path) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("[project]: must be a table")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ConfigError(
            "[project].name: required, lowercase id matching [a-z0-9][a-z0-9_.-]*"
        )
    schema = raw.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise ConfigError(
            f"[project].schema: unsupported version {schema!r} "
            f"(this hooke speaks schema {SCHEMA_VERSION})"
        )
    if not isinstance(automations_raw, dict) or not automations_raw:
        raise ConfigError("[automation]: at least one automation is required")
    automations: dict[str, dict] = {}
    for aname, araw in automations_raw.items():
        if not _AUTOMATION_KEY_RE.match(aname):
            raise ConfigError(f"automation name {aname!r}: must match [a-z0-9][a-z0-9_-]*")
        automations[aname] = normalize_automation(aname, araw)
    return {"name": name, "root": str(root), "automations": automations}


def validate_normalized(norm: dict) -> dict:
    """Daemon-side re-validation of an already-normalized config dict."""
    try:
        if set(norm) != {"name", "root", "automations"}:
            raise ConfigError("config: unexpected top-level keys")
        if not isinstance(norm["name"], str) or not _NAME_RE.match(norm["name"]):
            raise ConfigError("config.name: invalid")
        root = Path(norm["root"])
        if not root.is_absolute():
            raise ConfigError("config.root: must be an absolute path")
        autos = norm["automations"]
        if not isinstance(autos, dict) or not autos:
            raise ConfigError("config.automations: must be a non-empty table")
        clean: dict[str, dict] = {}
        for aname, araw in autos.items():
            if not _AUTOMATION_KEY_RE.match(aname):
                raise ConfigError(f"automation name {aname!r}: invalid")
            # Re-run the full normalizer: it is total (raises on any bad input)
            # and compiles every glob, which is the part worth re-checking.
            rebuilt = dict(araw)
            rebuilt["schedule"] = _norm_schedule(_rehydrate_duration(araw, "schedule"))
            rebuilt["watch"] = _norm_watch(_rehydrate_watch(araw))
            rebuilt.pop("schedule_seconds", None)
            rebuilt.pop("watch_seconds", None)
            clean[aname] = normalize_automation(aname, rebuilt)
        return {"name": norm["name"], "root": str(root), "automations": clean}
    except ConfigError:
        raise
    except (KeyError, TypeError) as e:
        raise ConfigError(f"config: malformed ({e})") from e


def _rehydrate_duration(araw: dict, key: str) -> dict | None:
    """Rebuild a {'every': seconds} table from the wire format for re-validation."""
    val = araw.get(key)
    if val is None:
        return None
    if not isinstance(val, dict):
        raise ConfigError(f"{key}: must be a table")
    return {"every": val.get("every")}


def _rehydrate_watch(araw: dict) -> dict | None:
    val = araw.get("watch")
    if val is None:
        return None
    if not isinstance(val, dict):
        raise ConfigError("watch: must be a table")
    return {
        "paths": val.get("paths"),
        "ignore": val.get("ignore", []),
        "debounce": val.get("debounce"),
    }


def load_and_validate(toml_path: Path) -> tuple[dict, str]:
    """Parse + normalize + validate a hooke.toml. Returns (normalized, hash)."""
    try:
        with open(toml_path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{toml_path}: invalid TOML: {e}") from e
    except FileNotFoundError as e:
        raise ConfigError(f"{toml_path}: not found (run 'hooke init' first?)") from e
    norm = normalize_project(raw.get("project"), raw.get("automation"), toml_path.parent.resolve())
    # Compile every watch glob up front — glob errors die here, in the CLI.
    for auto in norm["automations"].values():
        if auto["watch"]:
            for p in auto["watch"]["paths"] + auto["watch"]["ignore"]:
                globmod.translate(p)
    return norm, config_hash(norm)


def canonical_json(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def config_hash(norm: dict) -> str:
    return hashlib.sha256(canonical_json(norm).encode()).hexdigest()


INIT_TEMPLATE = '''\
# hooke.toml — agentic automations for this project.
# Docs: run each automation through pi with the MCP servers listed below.

[project]
name = "{name}"
schema = 1

# Periodic automation: runs every 6 hours.
#[automation.sync-flashcards]
#prompt = "Check notes/ for new cards and add them to Anki"
#mcp_servers = ["anki"]
#schedule = {{ every = "6h" }}
#resume_on_restart = false
#continuity = true

# File-triggered automation: runs shortly after matching files change.
#[automation.index-notes]
#prompt = "Re-index files that changed"
#watch = {{ paths = ["notes/**"], ignore = ["**/.git/**"], debounce = "2s" }}
#resume_on_restart = true
'''
