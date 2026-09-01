"""Live session guard changes and per-workflow session defaults."""

from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from grayson.cli import app
from grayson.config import CONFIG_FILENAME, WorkspaceConfig
from grayson.config_edit import ConfigError, config_summary, set_workflow_defaults
from grayson.core.session import Session

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def invoke_err(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 1, result.output
    return json.loads(result.stderr or result.output)


@pytest.fixture
def at_a_terminal(monkeypatch):
    import grayson.cli as cli

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)


@pytest.fixture
def sid(workspace, fake_snow_env) -> str:
    out = invoke(
        "session", "start", "--workflow", "table-health", "--table", "DB.S.T1", "--skip-snapshot"
    )
    return out["session"]["id"]


# -- per-workflow defaults: config surface --------------------------------


def test_workflow_defaults_round_trip(workspace):
    set_workflow_defaults(workspace.root, "table-health", guard_profile="strict", strict_scope=True)
    cfg = WorkspaceConfig.load(workspace.root / CONFIG_FILENAME)
    wd = cfg.workflow_defaults["table-health"]
    assert wd.guard_profile == "strict" and wd.strict_scope is True
    assert config_summary(workspace.root)["workflow_defaults"] == {
        "table-health": {"guard_profile": "strict", "strict_scope": True}
    }
    # each call states the full row: profile omitted inherits again
    set_workflow_defaults(workspace.root, "table-health", strict_scope=False)
    wd = WorkspaceConfig.load(workspace.root / CONFIG_FILENAME).workflow_defaults["table-health"]
    assert wd.guard_profile is None and wd.strict_scope is False
    # omitting both clears the entry entirely
    set_workflow_defaults(workspace.root, "table-health")
    cfg = WorkspaceConfig.load(workspace.root / CONFIG_FILENAME)
    assert "table-health" not in cfg.workflow_defaults


def test_workflow_defaults_reject_unknown_profile(workspace):
    with pytest.raises(ConfigError, match="unknown guard profile"):
        set_workflow_defaults(workspace.root, "table-health", guard_profile="nope")


def test_cli_config_workflow_defaults(workspace):
    out = invoke(
        "config",
        "workflow-defaults",
        "table-health",
        "--guard-profile",
        "strict",
        "--strict-scope",
    )
    assert out["defaults"] == {"guard_profile": "strict", "strict_scope": True}
    err = invoke_err("config", "workflow-defaults", "no-such-workflow", "--strict-scope")
    assert "unknown workflow" in err["error"]


# -- per-workflow defaults: session start resolution ----------------------


def test_session_start_uses_workspace_workflow_defaults(workspace, fake_snow_env):
    invoke(
        "config",
        "workflow-defaults",
        "table-health",
        "--guard-profile",
        "strict",
        "--strict-scope",
    )
    out = invoke(
        "session", "start", "--workflow", "table-health", "--table", "DB.S.T1", "--skip-snapshot"
    )
    assert out["guard_profile_source"] == "workspace_workflow_default"
    assert out["session"]["guard_profile"] == "strict"
    assert out["session"]["strict_scope"] is True


def test_explicit_flags_outrank_workflow_defaults(workspace, fake_snow_env):
    invoke(
        "config",
        "workflow-defaults",
        "table-health",
        "--guard-profile",
        "strict",
        "--strict-scope",
    )
    out = invoke(
        "session",
        "start",
        "--workflow",
        "table-health",
        "--table",
        "DB.S.T2",
        "--guard-profile",
        "generous",
        "--no-strict-scope",
        "--skip-snapshot",
    )
    assert out["guard_profile_source"] == "flag"
    assert out["session"]["guard_profile"] == "generous"
    assert out["session"]["strict_scope"] is False


def test_table_onboarding_suggests_strict_scope(workspace, fake_snow_env):
    # bounded shape: the core template itself suggests strict scope on
    out = invoke(
        "session",
        "start",
        "--workflow",
        "table-onboarding",
        "--table",
        "DB.S.T1",
        "--skip-snapshot",
    )
    assert out["session"]["strict_scope"] is True
    # the flag still wins over the template's suggestion
    out = invoke(
        "session",
        "start",
        "--workflow",
        "table-onboarding",
        "--table",
        "DB.S.T2",
        "--no-strict-scope",
        "--skip-snapshot",
    )
    assert out["session"]["strict_scope"] is False


def test_mcp_session_start_honors_workflow_defaults(workspace, fake_snow_env):
    from grayson.mcp.server import build_server

    set_workflow_defaults(workspace.root, "table-health", guard_profile="strict", strict_scope=True)
    workspace.reload_config()
    server = build_server(workspace)
    result = asyncio.run(
        server.call_tool("session_start", {"workflow": "table-health", "tables": ["DB.S.T9"]})
    )
    content = getattr(result, "content", None) or []
    data = json.loads(content[0].text)
    assert data["session"]["guard_profile"] == "strict"
    assert data["session"]["strict_scope"] is True


# -- live session guard changes -------------------------------------------


def test_session_guard_change_is_a_logged_user_action(workspace, fake_snow_env, sid, at_a_terminal):
    out = invoke("session", "guard", sid, "--guard-profile", "strict", "--strict-scope")
    assert out["guard_profile"] == "strict" and out["strict_scope"] is True
    s = Session(workspace, sid)
    assert s.strict_scope is True
    assert s.guard_settings.auto_limit == 1000  # the strict profile, snapshotted
    assert s.summary()["guard_profile"] == "strict"
    ev = next(e for e in s.events(10) if e["type"] == "guard_changed")
    assert ev["actor"] == "user"
    assert ev["payload"]["strict_scope"] is True


def test_session_guard_needs_an_interactive_terminal(workspace, fake_snow_env, sid):
    err = invoke_err("session", "guard", sid, "--strict-scope")
    assert "interactive terminal" in err["error"]
    assert Session(workspace, sid).strict_scope is False  # nothing changed


def test_session_guard_with_nothing_to_change_fails(workspace, fake_snow_env, sid, at_a_terminal):
    err = invoke_err("session", "guard", sid)
    assert "nothing to change" in err["error"]


def test_session_guard_refuses_closed_sessions(workspace, fake_snow_env, sid, at_a_terminal):
    Session(workspace, sid).set_meta("stage", "closed")
    err = invoke_err("session", "guard", sid, "--strict-scope")
    assert "closed" in err["error"]


def test_session_guard_rejects_unknown_profile(workspace, fake_snow_env, sid, at_a_terminal):
    err = invoke_err("session", "guard", sid, "--guard-profile", "nope")
    assert "unknown guard profile" in err["error"]
