"""Tests for script automations: config, venv management, and the
script backend's argv/env/event handling."""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from test_config import write_toml

from hooke import scriptenv
from hooke.agents import AgentRunner, RunRequest
from hooke.agents.script import ScriptRunner
from hooke.config import ConfigError, load_and_validate, validate_normalized
from hooke.executor import Job, build_run_request

SCRIPT_TOML = """
[project]
name = "demo"
schema = 1

[automation.snapshot]
script = "scripts/snapshot.py"
dependencies = ["wheel"]
schedule = { every = "1h" }
"""

# ---------------------------------------------------------------- config


def test_script_automation_parses(tmp_path):
    norm, _ = load_and_validate(write_toml(tmp_path, SCRIPT_TOML))
    auto = norm["automations"]["snapshot"]
    assert auto["script"] == "scripts/snapshot.py"
    assert auto["dependencies"] == ["wheel"]
    assert auto["agent"] == "script"
    assert auto["prompt"] is None
    assert auto["schedule"] == {"every": 3600.0}


def test_script_automation_roundtrips_through_validation(tmp_path):
    norm, digest = load_and_validate(write_toml(tmp_path, SCRIPT_TOML))
    revalidated = validate_normalized(json.loads(json.dumps(norm)))
    assert revalidated["automations"]["snapshot"] == norm["automations"]["snapshot"]
    assert config_hash_matches(norm, digest)


def config_hash_matches(norm: dict, digest: str) -> bool:
    from hooke.config import config_hash

    return config_hash(norm) == digest


def test_script_no_deps_ok(tmp_path):
    body = SCRIPT_TOML.replace('dependencies = ["wheel"]\n', "")
    norm, _ = load_and_validate(write_toml(tmp_path, body))
    assert norm["automations"]["snapshot"]["dependencies"] == []


def test_script_path_and_deps_whitespace_stripped(tmp_path):
    body = SCRIPT_TOML.replace(
        'script = "scripts/snapshot.py"', 'script = " scripts/snapshot.py "'
    ).replace('dependencies = ["wheel"]', 'dependencies = [" wheel "]')
    norm, _ = load_and_validate(write_toml(tmp_path, body))
    auto = norm["automations"]["snapshot"]
    assert auto["script"] == "scripts/snapshot.py"
    assert auto["dependencies"] == ["wheel"]


@pytest.mark.parametrize(
    "bad",
    [
        'script = "/abs/path.py"',
        'script = "../outside.py"',
        'script = "a/../../b.py"',
        'script = ""',
        "script = 42",
    ],
)
def test_script_paths_rejected(tmp_path, bad):
    body = SCRIPT_TOML.replace('script = "scripts/snapshot.py"', bad)
    with pytest.raises(ConfigError, match="script"):
        load_and_validate(write_toml(tmp_path, body))


def test_script_rejects_agent_keys(tmp_path):
    for key, value in [
        ("prompt", '"do it with a model"'),
        ("agent", '"pi"'),
        ("model", '"haiku"'),
        ("thinking", '"low"'),
        ("continuity", "true"),
        ("mcp_servers", '["anki"]'),
    ]:
        body = SCRIPT_TOML.replace(
            'schedule = { every = "1h" }',
            f'{key} = {value}\nschedule = {{ every = "1h" }}',
        )
        with pytest.raises(ConfigError, match=key):
            load_and_validate(write_toml(tmp_path, body))


def test_dependencies_without_script_rejected(tmp_path):
    body = SCRIPT_TOML.replace(
        'script = "scripts/snapshot.py"',
        'prompt = "use a model instead"',
    )
    with pytest.raises(ConfigError, match="dependencies"):
        load_and_validate(write_toml(tmp_path, body))


def test_dependencies_bad_types_rejected(tmp_path):
    for value in ["httpx", "[42]", '[""]', '["a", "a"]']:
        body = SCRIPT_TOML.replace('dependencies = ["wheel"]', f"dependencies = {value}")
        with pytest.raises(ConfigError, match="dependencies"):
            load_and_validate(write_toml(tmp_path, body))


# ---------------------------------------------------------------- venv


def test_ensure_venv_creates_and_marks(tmp_path):
    root = tmp_path
    scriptenv.ensure_venv(root, [])
    py = scriptenv.venv_python(root)
    assert py.is_file()
    marker = scriptenv.marker_path(root)
    assert marker.is_file()
    assert marker.read_text().strip() == scriptenv.deps_signature([])

    # Same dependency set: marker matches, no pip call needed (venv intact).
    scriptenv.ensure_venv(root, [])
    assert py.is_file()


def test_ensure_venv_rebuilds_broken(tmp_path):
    root = tmp_path
    scriptenv.ensure_venv(root, [])
    scriptenv.venv_python(root).unlink()  # simulate a mangled venv
    scriptenv.ensure_venv(root, [])
    assert scriptenv.venv_python(root).is_file()


def test_ensure_venv_bad_dependency_fails(tmp_path):
    with pytest.raises(scriptenv.ScriptEnvError, match="dependency install"):
        scriptenv.ensure_venv(tmp_path, ["definitely-not-a-real-package-xyz"])


def test_venv_python_path_shape(tmp_path):
    assert scriptenv.venv_python(tmp_path) == tmp_path / ".hooke" / "venv" / "bin" / "python"


# ---------------------------------------------------------------- backend


def _request(tmp_path, **overrides) -> RunRequest:
    fields = {
        "prompt": "",
        "cwd": str(tmp_path),
        "script": "scripts/snapshot.py",
        "dependencies": ("wheel",),
        "project": "demo",
        "automation": "snapshot",
        "run_id": "abc123",
        "trigger_kind": "watch",
        "trigger": {"files": {"modified": ["a.md"]}},
        "resumed_from": None,
    }
    fields.update(overrides)
    return RunRequest(**fields)


def test_build_argv_uses_project_venv_python(tmp_path):
    argv = ScriptRunner().build_argv(_request(tmp_path))
    assert argv == [
        str(tmp_path / ".hooke" / "venv" / "bin" / "python"),
        str(tmp_path / "scripts" / "snapshot.py"),
    ]


def test_child_env_carries_trigger_context(tmp_path):
    env = ScriptRunner().child_env(_request(tmp_path))
    assert env["HOOKE_PROJECT"] == "demo"
    assert env["HOOKE_AUTOMATION"] == "snapshot"
    assert env["HOOKE_TRIGGER_KIND"] == "watch"
    assert json.loads(env["HOOKE_TRIGGER"]) == {"files": {"modified": ["a.md"]}}
    assert env["HOOKE_ROOT"] == str(tmp_path)
    assert env["HOOKE_RUN_ID"] == "abc123"
    assert env["HOOKE_RESUMED_FROM"] == ""

    empty = ScriptRunner().child_env(_request(tmp_path, trigger=None))
    assert empty["HOOKE_TRIGGER"] == ""


def test_parse_event_write_lines_and_output():
    agent = ScriptRunner()
    ev = agent.parse_event("@hooke-write: /tmp/out.md")
    assert ev.write_paths == ("/tmp/out.md",)
    assert not ev.assistant_message

    ev = agent.parse_event("plain progress line")
    assert ev.assistant_message and not ev.write_paths

    assert agent.parse_event("").assistant_message is False
    assert agent.parse_event("").write_paths == ()


def test_parse_event_relative_write_path_resolves():
    ev = ScriptRunner().parse_event("@hooke-write: out.md")
    assert ev.write_paths == (str(pathlib.Path("out.md").resolve()),)


def test_script_backend_registered():
    assert "script" in AgentRunner.known_agents()
    assert isinstance(AgentRunner.get_agent("script"), ScriptRunner)


def test_prepare_fails_when_script_missing(tmp_path):
    err = asyncio.run(ScriptRunner().prepare(_request(tmp_path)))
    assert err and "scripts/snapshot.py" in err


def test_prepare_succeeds_after_venv_sync(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "snapshot.py").write_text("print('ok')\n")
    err = asyncio.run(ScriptRunner().prepare(_request(tmp_path, dependencies=())))
    assert err is None
    assert scriptenv.venv_python(tmp_path).is_file()


# ---------------------------------------------------------------- wiring


def _job(cfg: dict) -> Job:
    return Job("demo", "snapshot", cfg, "/tmp", "schedule", None)


def test_build_run_request_script_fields():
    cfg = {
        "prompt": None,
        "agent": "script",
        "script": "scripts/snapshot.py",
        "dependencies": ["httpx"],
        "mcp_servers": [],
        "schedule": {"every": 60.0},
        "watch": None,
        "resume_on_restart": False,
        "continuity": False,
        "model": None,
        "thinking": None,
        "timeout": 60.0,
        "concurrency": 1,
        "excluded_mcp_servers": [],
    }
    req = build_run_request(_job(cfg), None, None, run_id="r1")
    assert req.script == "scripts/snapshot.py"
    assert req.dependencies == ("httpx",)
    assert req.project == "demo"
    assert req.automation == "snapshot"
    assert req.run_id == "r1"
    assert req.prompt == ""
    assert req.trigger_kind == "schedule"
