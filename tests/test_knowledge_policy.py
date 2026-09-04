"""The knowledge policy: presets, overrides, the meet of workspace and
library, where each lives, and the surfaces that read and change it."""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest
from typer.testing import CliRunner

from grayson.cli import app
from grayson.config import WorkspaceConfig
from grayson.config_edit import ConfigError, set_knowledge_actions, set_values
from grayson.identity import set_user_id
from grayson.knowledge.policy import (
    ACTIONS,
    KnowledgePolicy,
    PolicyError,
    meet,
)
from grayson.library import (
    effective_policy,
    library_policy,
    link_library,
    read_library_settings,
    set_library_policy,
    write_library_settings,
)
from grayson.mcp.server import build_server

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def invoke_err(*args) -> dict:
    result = runner.invoke(app, list(args))
    assert result.exit_code != 0, result.output
    return json.loads(result.stderr or result.output)


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


@pytest.fixture
def at_a_terminal(monkeypatch):
    import grayson.cli as cli

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)


@pytest.fixture
def team_lib(workspace, tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    assert _git("init", "--bare", str(origin)).returncode == 0
    clone = tmp_path / "lib-clone"
    link_library(workspace, str(origin), clone, auto_push=True)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    workspace.reload_config()
    return clone


# -- the model --------------------------------------------------------------------


def test_presets_and_overrides():
    p = KnowledgePolicy.from_preset("curate", {"restore": "agent"})
    assert p.actor("restore") == "agent" and p.actor("resolve_contested") == "user"
    assert p.actor("retire") == "agent" and p.denied_actions() == ["resolve_contested"]
    assert KnowledgePolicy.from_preset("propose").denied_actions() == list(ACTIONS)
    assert KnowledgePolicy.from_preset("autonomous").denied_actions() == []
    with pytest.raises(PolicyError, match="unknown knowledge policy preset"):
        KnowledgePolicy.from_preset("nope")
    with pytest.raises(PolicyError, match="unknown knowledge action"):
        KnowledgePolicy.from_preset("curate", {"fly": "agent"})
    with pytest.raises(PolicyError, match="actor must be"):
        KnowledgePolicy.from_preset("curate", {"retire": "robot"})
    with pytest.raises(PolicyError, match="trust must be"):
        KnowledgePolicy.from_preset("curate", trust="blind")


def test_trust_admits_statuses():
    assert KnowledgePolicy.from_preset("curate", trust="data_inferred").admits("data_inferred")
    assert not KnowledgePolicy.from_preset("curate", trust="data_inferred").admits("proposed")
    assert KnowledgePolicy.from_preset("curate", trust="proposed").admits("proposed")
    assert not KnowledgePolicy.from_preset("curate", trust="user_confirmed").admits("data_inferred")


def test_meet_narrows_and_never_widens():
    eff = meet(KnowledgePolicy.from_preset("autonomous"), KnowledgePolicy.from_preset("propose"))
    assert all(actor == "user" for actor in eff.actions.values())
    assert eff.narrowed_by["retire"] == "library" and "library" in eff.refusal("retire")
    eff = meet(KnowledgePolicy.from_preset("propose"), KnowledgePolicy.from_preset("autonomous"))
    assert eff.narrowed_by["retire"] == "workspace" and "grayson.toml" in eff.refusal("retire")
    eff = meet(KnowledgePolicy.from_preset("curate"), KnowledgePolicy.from_preset("curate"))
    assert eff.allows_agent("retire") and not eff.allows_agent("restore")
    # the stricter trust wins
    eff = meet(
        KnowledgePolicy.from_preset("curate", trust="proposed"),
        KnowledgePolicy.from_preset("curate", trust="user_confirmed"),
    )
    assert eff.trust == "user_confirmed"
    # the library's horizon stands only when it set one
    eff = meet(
        KnowledgePolicy.from_preset("curate", proposed_horizon_days=10),
        KnowledgePolicy.from_preset("curate"),
    )
    assert eff.proposed_horizon_days == 10
    eff = meet(
        KnowledgePolicy.from_preset("curate", proposed_horizon_days=10),
        KnowledgePolicy.from_preset("curate", proposed_horizon_days=30),
    )
    assert eff.proposed_horizon_days == 30
    solo = meet(KnowledgePolicy.from_preset("autonomous"), None)
    assert solo.allows_agent("restore") and solo.narrowed_by == {}


def test_library_settings_parse():
    assert KnowledgePolicy.from_library_settings({"admins": ["a"]}) is None
    p = KnowledgePolicy.from_library_settings(
        {"knowledge_policy": "autonomous", "knowledge_agent_denied": ["restore"]}
    )
    assert p.actor("restore") == "user" and p.actor("retire") == "agent"
    with pytest.raises(PolicyError):
        KnowledgePolicy.from_library_settings({"knowledge_agent_denied": ["fly"]})
    with pytest.raises(PolicyError):
        KnowledgePolicy.from_library_settings({"knowledge_agent_denied": "restore"})


# -- where it lives ---------------------------------------------------------------


def test_workspace_config_parses_the_knowledge_section(workspace):
    assert workspace.config.knowledge.preset == "curate"
    set_values(workspace.root, {"knowledge.policy": "autonomous", "knowledge.briefing_cap": 5})
    set_knowledge_actions(workspace.root, {"retire": "user"})
    cfg = workspace.reload_config()
    assert cfg.knowledge.preset == "autonomous" and cfg.knowledge.briefing_cap == 5
    assert cfg.knowledge.actor("retire") == "user" and cfg.knowledge.actor("restore") == "agent"
    set_knowledge_actions(workspace.root, {"retire": None})
    assert workspace.reload_config().knowledge.actor("retire") == "agent"
    with pytest.raises(ConfigError, match="preset"):
        set_values(workspace.root, {"knowledge.policy": "nope"})
    with pytest.raises(ConfigError, match="trust"):
        set_values(workspace.root, {"knowledge.trust": "blind"})
    with pytest.raises(ConfigError, match="integer"):
        set_values(workspace.root, {"knowledge.briefing_cap": "many"})
    with pytest.raises(ConfigError, match="unknown knowledge action"):
        set_knowledge_actions(workspace.root, {"fly": "agent"})


def test_bad_policy_in_config_is_a_named_error(workspace):
    path = workspace.root / "grayson.toml"
    text = path.read_text(encoding="utf-8")
    assert 'policy = "curate"' in text  # the template carries the section
    path.write_text(text.replace('policy = "curate"', 'policy = "nope"'), encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[knowledge\]"):
        WorkspaceConfig.load(path)


def test_effective_policy_solo_then_team_default(workspace, team_lib):
    # the fixture linked a library: a library that has not chosen is `propose`
    eff = effective_policy(workspace)
    assert eff.library_default and eff.library.preset == "propose"
    assert all(actor == "user" for actor in eff.actions.values())
    assert eff.narrowed_by["retire"] == "library"
    assert "default when unset" in eff.refusal("retire")
    write_library_settings(team_lib, {"knowledge_policy": "curate"})
    eff = effective_policy(workspace)
    assert (
        not eff.library_default and eff.allows_agent("retire") and not eff.allows_agent("restore")
    )


def test_set_library_policy_is_an_admins_commit(workspace, team_lib):
    set_user_id("kcg")
    out = set_library_policy(workspace, preset="curate", deny=["retire"])
    assert out["changed"] and out["policy"]["actions"]["retire"] == "user"
    assert read_library_settings(team_lib)["knowledge_policy"] == "curate"
    assert read_library_settings(team_lib)["knowledge_agent_denied"] == ["retire"]
    log = _git("log", "-1", "--format=%B", cwd=team_lib).stdout
    assert "grayson library policy" in log and "Grayson-User: kcg" in log
    assert set_library_policy(workspace, allow=["retire"])["policy"]["actions"]["retire"] == "agent"
    assert set_library_policy(workspace)["changed"] is False
    with pytest.raises(ValueError, match="unknown preset"):
        set_library_policy(workspace, preset="nope")
    write_library_settings(team_lib, {"admins": ["someone-else"]})
    with pytest.raises(PermissionError, match="only a library admin"):
        set_library_policy(workspace, preset="autonomous")
    policy, report = library_policy(team_lib)
    assert policy.preset == "curate" and report == {"set": True}


def test_set_library_policy_refuses_in_solo_mode(workspace):
    set_user_id("kcg")
    with pytest.raises(PermissionError, match="grayson.toml"):
        set_library_policy(workspace, preset="curate")


# -- surfaces ----------------------------------------------------------------------


def test_cli_policy_show_and_set_solo(workspace, at_a_terminal):
    shown = invoke("library", "policy", "show")
    assert shown["preset"] == "curate" and shown["library_file"] is None
    out = invoke("library", "policy", "set", "--preset", "autonomous", "--deny", "restore")
    assert out["scope"] == "workspace" and out["actions"]["restore"] == "user"
    assert out["actions"]["retire"] == "agent"
    assert invoke("knowledge", "policy")["preset"] == "autonomous"
    invoke("library", "policy", "set", "--allow", "restore")
    assert invoke("knowledge", "policy")["actions"]["restore"] == "agent"


def test_cli_policy_set_needs_a_terminal(workspace):
    err = invoke_err("library", "policy", "set", "--preset", "autonomous")
    assert "interactive terminal" in json.dumps(err)


def test_cli_policy_set_team(workspace, team_lib, at_a_terminal):
    set_user_id("kcg")
    out = invoke("library", "policy", "set", "--preset", "curate", "--trust", "proposed")
    assert out["scope"] == "library" and out["policy"]["preset"] == "curate"
    shown = invoke("library", "policy", "show")
    assert shown["library"]["preset"] == "curate" and shown["trust"] == "data_inferred"
    # the workspace's default trust (data_inferred) is stricter than the library's
    assert shown["library_file"].endswith("library.toml")


def test_doctor_and_status_report_the_policy(workspace, team_lib):
    from grayson.library import library_doctor, library_status

    write_library_settings(team_lib, {"knowledge_policy": "autonomous"})
    assert library_status(workspace)["knowledge_policy"]["preset"] == "autonomous"
    report = library_doctor(workspace)
    assert report["policy"]["library"]["preset"] == "autonomous"
    assert report["settings"]["knowledge_policy"]["preset"] == "autonomous"
    write_library_settings(team_lib, {"knowledge_policy": "nope"})
    report = library_doctor(workspace)
    assert report["ok"] is False and any(
        "knowledge policy" in e for e in report["settings"]["errors"]
    )


def _call(server, name: str, args: dict):
    result = asyncio.run(server.call_tool(name, args))
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    content = getattr(result, "content", None) or []
    if content and getattr(content[0], "text", None):
        return json.loads(content[0].text)
    return structured


def test_mcp_knowledge_policy_and_refusal(workspace, fake_snow_env):
    from grayson.knowledge import KnowledgeStore

    server = build_server(workspace)
    policy = _call(server, "knowledge_policy", {})
    assert policy["preset"] == "curate" and policy["actions"]["retire"] == "agent"
    KnowledgeStore(workspace.knowledge_dir).add_fact("DB.S.T", "x", fact_id="x")
    refused = _call(server, "knowledge_restore", {"table": "DB.S.T", "fact_id": "x"})
    assert refused["type"] == "ActionRefused" and "grayson.toml" in refused["error"]
    assert refused["policy"]["actions"]["restore"] == "user"
    retired = _call(
        server, "knowledge_retire", {"table": "DB.S.T", "fact_id": "x", "evidence": ["q_1"]}
    )
    assert retired["fact"]["standing"] == "retired" and retired["fact"]["retired_by"] == "agent"
    no_evidence = _call(
        server, "knowledge_retire", {"table": "DB.S.T", "fact_id": "x", "evidence": []}
    )
    assert "evidence" in no_evidence["error"]
