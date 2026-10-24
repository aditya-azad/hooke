"""Tests for the pure layers: config parse/validate/hash, durations,
schedule-state math, run-record merging, prompt/argv building."""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from hooke import config
from hooke.agents import AgentRunner, RunRequest
from hooke.agents.pi import PiAgent
from hooke.config import ConfigError
from hooke.executor import Job, build_prompt, build_run_request
from hooke.scheduler import boot_next_due
from hooke.stores import RunStore


def write_toml(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    p = tmp_path / "hooke.toml"
    p.write_text(body)
    return p


VALID = """
[project]
name = "demo"
schema = 1

[automation.tick]
prompt = "do the thing"
schedule = { every = "6h" }

[automation.watchy]
prompt = "react to files"
watch = { paths = ["notes/**"], ignore = ["**/.git/**"], debounce = "2s" }
resume_on_restart = true
"""

# ---------------------------------------------------------------- durations


@pytest.mark.parametrize(
    ("raw", "secs"),
    [("90s", 90.0), ("5m", 300.0), ("6h", 21600.0), ("1d", 86400.0), (30, 30.0)],
)
def test_parse_duration(raw, secs):
    assert config.parse_duration(raw, "x") == secs


@pytest.mark.parametrize("bad", ["", "6x", "-5m", "h", None, []])
def test_parse_duration_rejects(bad):
    with pytest.raises(config.ConfigError):
        config.parse_duration(bad, "x")


# ---------------------------------------------------------------- validation


def test_valid_config(tmp_path):
    norm, digest = config.load_and_validate(write_toml(tmp_path, VALID))
    assert norm["name"] == "demo"
    assert norm["root"] == str(tmp_path)
    assert norm["automations"]["tick"]["schedule"] == {"every": 21600.0}
    assert norm["automations"]["tick"]["watch"] is None
    assert norm["automations"]["watchy"]["watch"]["debounce"] == 2.0
    assert norm["automations"]["watchy"]["resume_on_restart"] is True
    assert len(digest) == 64


def test_schema_version_rejected(tmp_path):
    with pytest.raises(config.ConfigError, match="schema"):
        config.load_and_validate(write_toml(tmp_path, VALID.replace("schema = 1", "schema = 2")))


def test_no_trigger_rejected(tmp_path):
    body = VALID.replace('schedule = { every = "6h" }', "")
    with pytest.raises(config.ConfigError, match="trigger"):
        config.load_and_validate(write_toml(tmp_path, body))


def test_empty_glob_rejected(tmp_path):
    body = VALID.replace('paths = ["notes/**"]', 'paths = [""]')
    with pytest.raises(config.ConfigError, match="glob"):
        config.load_and_validate(write_toml(tmp_path, body))


def test_glob_weird_but_translatable_is_accepted(tmp_path):
    # fnmatch-style translation tolerates unbalanced brackets (literal '[');
    # glob sanity means "compiles", not "pretty".
    body = VALID.replace('paths = ["notes/**"]', 'paths = ["notes/["]')
    norm, _ = config.load_and_validate(write_toml(tmp_path, body))
    assert norm["automations"]["watchy"]["watch"]["paths"] == ["notes/["]


def test_empty_prompt_rejected(tmp_path):
    body = VALID.replace('prompt = "do the thing"', 'prompt = ""')
    with pytest.raises(config.ConfigError, match="prompt"):
        config.load_and_validate(write_toml(tmp_path, body))


def test_bad_thinking_rejected(tmp_path):
    body = VALID.replace("resume_on_restart = true", "resume_on_restart = true\nthinking = \"ultra\"")
    with pytest.raises(config.ConfigError, match="thinking"):
        config.load_and_validate(write_toml(tmp_path, body))


def test_daemon_revalidation_accepts_roundtrip(tmp_path):
    norm, _ = config.load_and_validate(write_toml(tmp_path, VALID))
    revalidated = config.validate_normalized(norm)
    assert revalidated["automations"]["tick"] == norm["automations"]["tick"]


def test_daemon_revalidation_rejects_garbage():
    with pytest.raises(config.ConfigError):
        config.validate_normalized(
            {"name": "x", "root": "/tmp", "automations": {"a": {"prompt": "p"}}}
        )  # no trigger
    with pytest.raises(config.ConfigError):
        config.validate_normalized({"name": "X!", "root": "/tmp", "automations": {}})


def test_hash_stability(tmp_path):
    _, h1 = config.load_and_validate(write_toml(tmp_path, VALID))
    _, h2 = config.load_and_validate(write_toml(tmp_path, VALID))
    assert h1 == h2


# ---------------------------------------------------------------- scheduler


def test_boot_next_due_no_backlog():
    now = 1_000_000.0
    interval = 60.0
    # Missed many windows: fire once, soon, never a backlog.
    last, due = boot_next_due(now - 10 * interval, interval, now)
    assert last == now - 10 * interval
    assert now <= due <= now + 30.0
    # Fresh automation fires once immediately after jitter.
    last, due = boot_next_due(None, interval, now)
    assert last is None and now <= due <= now + 30.0
    # Future due preserved.
    last, due = boot_next_due(now + 10, interval, now)
    assert due == now + 10 + interval


# ---------------------------------------------------------------- run store


def test_run_store_merges_partial_records(tmp_path, monkeypatch):
    monkeypatch.setattr("hooke.paths.runs_path", lambda: tmp_path / "runs.jsonl")
    store = RunStore()
    store.append({"run_id": "r1", "project": "p", "status": "pending", "started_at": None})
    store.append({"run_id": "r1", "status": "running", "started_at": 42.0, "pid": 7})
    store.append({"run_id": "r1", "status": "finished", "ended_at": 50.0})
    store.close()
    merged = RunStore().load_latest()["r1"]
    assert merged["project"] == "p"  # from the first record
    assert merged["status"] == "finished"  # latest wins
    assert merged["started_at"] == 42.0  # merged, not clobbered
    assert merged["pid"] == 7


# ---------------------------------------------------------------- agents


def _job(**overrides) -> Job:
    cfg = {
        "prompt": "do the thing",
        "agent": "pi",
        "mcp_servers": [],
        "schedule": {"every": 60.0},
        "watch": None,
        "resume_on_restart": False,
        "continuity": False,
        "model": None,
        "thinking": None,
        "timeout": 60.0,
        "concurrency": 1,
    }
    cfg.update(overrides)
    return Job("proj", "auto", cfg, "/tmp", "schedule", None)


def test_build_prompt_schedule():
    text = build_prompt(_job(), None)
    assert "do the thing" in text
    assert "trigger: schedule" in text


def test_build_prompt_watch_manifest():
    job = _job()
    job2 = Job("proj", "auto", job.cfg, "/tmp", "watch", {"files": {"added": ["a.md"], "modified": [], "removed": ["b.md"]}})
    text = build_prompt(job2, None)
    assert "added: a.md" in text
    assert "removed: b.md" in text


def test_build_prompt_resume_note():
    text = build_prompt(_job(), "abc123")
    assert "abc123" in text
    assert "interrupted" in text


def test_agent_defaults_to_pi(tmp_path):
    norm, _ = config.load_and_validate(write_toml(tmp_path, VALID))
    assert norm["automations"]["tick"]["agent"] == "pi"


def test_agent_custom_and_invalid(tmp_path):
    body = VALID.replace('prompt = "do the thing"', 'prompt = "do the thing"\nagent = "claude-code"')
    norm, _ = config.load_and_validate(write_toml(tmp_path, body))
    assert norm["automations"]["tick"]["agent"] == "claude-code"

    body = VALID.replace('prompt = "do the thing"', 'prompt = "do the thing"\nagent = "Bad Agent"')
    with pytest.raises(ConfigError):
        config.load_and_validate(write_toml(tmp_path, body))


def test_agent_roundtrips_through_validation(tmp_path):
    body = VALID.replace('prompt = "do the thing"', 'prompt = "do the thing"\nagent = "pi"')
    norm, digest = config.load_and_validate(write_toml(tmp_path, body))
    revalidated = config.validate_normalized(norm)
    assert revalidated["automations"]["tick"]["agent"] == "pi"
    assert config.config_hash(revalidated) == digest


def _request(job: Job, prompt: str, session_ref: str | None = None) -> RunRequest:
    return build_run_request(job, prompt, session_ref)


def test_build_argv_basics():
    argv = PiAgent().build_argv(_request(_job(), "prompt here"))
    assert argv[:4] == ["pi", "--mode", "json", "-p"]
    assert "--no-session" in argv
    assert "--exclude-tools" not in argv


def test_build_argv_excluded_mcp():
    job = _job()
    job.cfg["excluded_mcp_servers"] = ["papers"]
    argv = PiAgent().build_argv(_request(job, "prompt here"))
    i = argv.index("--exclude-tools")
    assert argv[i + 1] == "mcp__papers__*"


def test_build_argv_continuity_session():
    job = _job(continuity=True)
    argv = PiAgent().build_argv(_request(job, "prompt", "sess-1"))
    i = argv.index("--session-dir")
    assert argv[i + 1].endswith("sessions/proj/auto")
    assert argv[argv.index("--session") + 1] == "sess-1"


def test_build_argv_model_thinking():
    argv = PiAgent().build_argv(
        _request(_job(model="haiku", thinking="low"), "p")
    )
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--thinking") + 1] == "low"


def test_pi_parse_event():
    agent = PiAgent()
    ev = agent.parse_event(
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "name": "edit", "arguments": {"path": "/x/y.md"}}
                    ],
                },
            }
        )
    )
    assert ev.assistant_message
    assert ev.write_paths == ("/x/y.md",)
    assert ev.session_id is None

    ev = agent.parse_event(json.dumps({"type": "session", "id": "s-1"}))
    assert ev.session_id == "s-1"
    assert not ev.assistant_message

    assert agent.parse_event("not json").session_id is None
    assert agent.parse_event("[1,2]").assistant_message is False


def test_get_agent_registry():
    assert "pi" in AgentRunner.known_agents()
    assert isinstance(AgentRunner.get_agent("pi"), PiAgent)
    with pytest.raises(ConfigError):
        AgentRunner.get_agent("gemini")


def test_subclass_auto_registers():
    class FakeAgent(AgentRunner):
        name = "fake-test-agent"

        def build_argv(self, request):
            return ["fake"]

    assert "fake-test-agent" in AgentRunner.known_agents()
    assert isinstance(AgentRunner.get_agent("fake-test-agent"), FakeAgent)

    # Unnamed (abstract-ish) intermediates stay unregistered.
    class Unnamed(AgentRunner):
        pass

    assert type(AgentRunner.get_agent("fake-test-agent")) is not Unnamed

    # Name collisions are caught at import time.
    with pytest.raises(ValueError):

        class Clash(AgentRunner):
            name = "fake-test-agent"

    del AgentRunner._registry["fake-test-agent"]  # don't leak into other tests


# ---------------------------------------------------------------- protocol


# ---------------------------------------------------------------- protocol


def test_canonical_json_and_hash():
    assert json.loads(config.canonical_json({"b": 1, "a": 2})) == {"a": 2, "b": 1}
    assert config.config_hash({"a": 1}) == config.config_hash({"a": 1})
    assert config.config_hash({"a": 1}) != config.config_hash({"a": 2})


# ---------------------------------------------------------------- agents

from hooke.agents.claude import ClaudeAgent
from hooke.agents.codex import CodexAgent


def test_known_agents_includes_new_backends():
    assert set(AgentRunner.known_agents()) >= {"pi", "claude", "codex"}


def test_claude_build_argv_basics():
    argv = ClaudeAgent().build_argv(_request(_job(agent="claude"), "prompt here"))
    assert argv[0] == "claude"
    i = argv.index("--print")
    assert argv[i + 1] == "prompt here"
    assert "--output-format" in argv and "stream-json" in argv
    assert "--no-session-persistence" in argv
    assert "--resume" not in argv
    assert "--disallowedTools" not in argv


def test_claude_build_argv_continuity_session():
    job = _job(agent="claude", continuity=True)
    argv = ClaudeAgent().build_argv(_request(job, "prompt", "sess-1"))
    assert argv[argv.index("--resume") + 1] == "sess-1"
    assert "--no-session-persistence" not in argv


def test_claude_build_argv_model_thinking_excluded_mcp():
    job = _job(agent="claude", model="sonnet", thinking="low")
    job.cfg["excluded_mcp_servers"] = ["papers"]
    argv = ClaudeAgent().build_argv(_request(job, "p"))
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--effort") + 1] == "low"
    assert argv[argv.index("--disallowedTools") + 1] == "mcp__papers"


def test_claude_parse_event():
    agent = ClaudeAgent()
    ev = agent.parse_event(
        json.dumps({"type": "system", "subtype": "init", "session_id": "s-1"})
    )
    assert ev.session_id == "s-1"
    assert not ev.assistant_message

    ev = agent.parse_event(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Write", "input": {"file_path": "/x/y.md"}},
                        {"type": "text", "text": "hi"},
                    ],
                },
            }
        )
    )
    assert ev.assistant_message
    assert ev.write_paths == ("/x/y.md",)
    assert ev.session_id is None

    assert agent.parse_event("not json").session_id is None


def test_codex_build_argv_basics():
    argv = CodexAgent().build_argv(_request(_job(agent="codex"), "prompt here"))
    assert argv[:2] == ["codex", "exec"]
    assert "resume" not in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--json" in argv and "--skip-git-repo-check" in argv
    assert argv[-1] == "prompt here"


def test_codex_build_argv_resume():
    job = _job(agent="codex", continuity=True)
    argv = CodexAgent().build_argv(_request(job, "prompt", "thread-1"))
    assert argv[:4] == ["codex", "exec", "resume", "thread-1"]
    assert any('sandbox_mode="workspace-write"' == a for a in argv)


def test_codex_build_argv_model_thinking_excluded_mcp():
    job = _job(agent="codex", model="gpt-5.2", thinking="high")
    job.cfg["excluded_mcp_servers"] = ["papers", "anki"]
    argv = CodexAgent().build_argv(_request(job, "p"))
    assert argv[argv.index("-m") + 1] == "gpt-5.2"
    assert argv[argv.index("-c") + 1] == 'model_reasoning_effort="high"'
    pairs = [(argv[i], argv[i + 1]) for i, a in enumerate(argv) if a == "-c"]
    assert 'mcp_servers.papers.enabled=false' in argv
    assert 'mcp_servers.anki.enabled=false' in argv


def test_codex_parse_event():
    agent = CodexAgent()
    ev = agent.parse_event(
        json.dumps({"type": "thread.started", "thread_id": "t-1"})
    )
    assert ev.session_id == "t-1"

    ev = agent.parse_event(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}})
    )
    assert ev.assistant_message
    assert ev.session_id is None

    ev = agent.parse_event(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [{"path": "/x/y.md", "kind": "write"}],
                },
            }
        )
    )
    assert ev.write_paths == ("/x/y.md",)
    assert not ev.assistant_message

    assert agent.parse_event("not json").session_id is None


def test_configured_mcp_servers_agent_aware(tmp_path, monkeypatch):
    claude_cfg = tmp_path / "claude.json"
    claude_cfg.write_text(json.dumps({"mcpServers": {"anki": {}, "papers": {}}}))
    monkeypatch.setattr("hooke.agents.claude.CLAUDE_JSON", claude_cfg)
    servers, known = ClaudeAgent().configured_mcp_servers()
    assert known and servers == {"anki", "papers"}

    codex_cfg = tmp_path / "config.toml"
    codex_cfg.write_text('[mcp_servers.anki]\ncommand = "x"\n')
    monkeypatch.setattr("hooke.agents.codex.CODEX_CONFIG", codex_cfg)
    servers, known = CodexAgent().configured_mcp_servers()
    assert known and servers == {"anki"}

    # Missing configs degrade to (empty, unknown) — warn, don't fail deploy.
    monkeypatch.setattr("hooke.agents.claude.CLAUDE_JSON", tmp_path / "nope")
    assert ClaudeAgent().configured_mcp_servers() == (set(), False)

    # pi keeps its historical strictness: a missing file is still "known".
    assert PiAgent().configured_mcp_servers()[1] is True


def test_codex_parse_event_turn_failed():
    ev = CodexAgent().parse_event(
        json.dumps({"type": "turn.failed", "error": {"message": "boom"}})
    )
    assert ev.error == "boom"

    ev = CodexAgent().parse_event(json.dumps({"type": "turn.completed", "usage": {}}))
    assert ev.error is None
