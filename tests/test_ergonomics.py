"""Friction-reduction features: latest alias, status command, hints, UI refresh."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from grayson.cli import app
from grayson.ui.server import build_app

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def sid(workspace, fake_snow_env) -> str:
    out = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1")
    return out["session"]["id"]


def test_latest_alias_resolves(workspace, fake_snow_env, sid):
    for alias in ("latest", "last", "."):
        assert invoke("session", "status", alias)["id"] == sid


def test_latest_alias_in_query_flow(workspace, fake_snow_env, sid):
    run = invoke("query", "run", "latest", "-q", "SELECT * FROM DB.S.T1")
    assert run["status"] == "executed"
    log = invoke("query", "log", "latest")
    assert any(e["qid"] == run["qid"] for e in log)


def test_latest_alias_without_sessions_fails_cleanly(workspace):
    result = runner.invoke(app, ["session", "status", "latest"])
    assert result.exit_code == 1
    assert "no sessions yet" in result.output


def test_status_no_sessions(workspace):
    out = invoke("status")
    assert out["sessions"] == 0
    assert out["latest_session"] is None
    assert any("session start" in h for h in out["hints"])


def test_status_with_session_hints(workspace, fake_snow_env, sid):
    out = invoke("status")
    assert out["latest_session"]["id"] == sid
    assert out["latest_session"]["open_checks"]  # freshly seeded, all open
    assert any("checkpoints still open" in h for h in out["hints"])


def test_session_start_returns_hints(workspace, fake_snow_env):
    out = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1")
    hints = "\n".join(out["hints"])
    assert "grayson ui serve" in hints
    assert out["session"]["id"] in hints


def test_session_start_flags_knowledge_gaps(workspace, fake_snow_env):
    out = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1")
    assert out["knowledge_gaps"] == ["DB.S.T1"]
    assert any("table-onboarding" in h for h in out["hints"])
    # once knowledge exists, the gap (and its hint) disappear
    invoke("knowledge", "add", "DB.S.T1", "--fact", "one row per ID")
    out2 = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1", "--new")
    assert out2["knowledge_gaps"] == []
    assert not any("table-onboarding" in h for h in out2["hints"])


def test_session_start_idempotent_on_quick_rerun(workspace, fake_snow_env):
    first = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1")
    again = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1")
    assert again["reused_existing"] is True
    assert again["session"]["id"] == first["session"]["id"]
    assert again["checkpoints"]  # enough context to continue working
    forced = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1", "--new")
    assert forced["session"]["id"] != first["session"]["id"]


def test_session_start_not_deduped_once_work_exists(workspace, fake_snow_env):
    first = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1")
    invoke("query", "run", first["session"]["id"], "-q", "SELECT * FROM DB.S.T1")
    second = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1")
    assert "reused_existing" not in second
    assert second["session"]["id"] != first["session"]["id"]


def test_table_onboarding_workflow_registered(workspace):
    names = {w["name"] for w in invoke("workflow", "list")}
    assert "table-onboarding" in names
    show = invoke("workflow", "show", "table-onboarding")
    assert {c["key"] for c in show["required_checks"]} == {
        "structure_profiled",
        "grain_established",
        "relationships_mapped",
        "sensitivity_classified",
        "definitions_located",
        "semantics_recorded",
    }
    # recording semantics is the last step, not a thing to do first
    recorded = next(c for c in show["required_checks"] if c["key"] == "semantics_recorded")
    assert "grain_established" in recorded["depends_on"]


def test_session_delete_requires_confirmation(workspace, fake_snow_env, sid):
    blocked = runner.invoke(app, ["session", "delete", sid])
    assert blocked.exit_code == 1
    assert "permanently deletes" in blocked.output
    assert invoke("session", "status", sid)["id"] == sid  # still there
    out = invoke("session", "delete", sid, "--yes")
    assert out["deleted"] == sid
    gone = runner.invoke(app, ["session", "status", sid])
    assert gone.exit_code == 1


def test_ui_pages_auto_refresh(workspace, fake_snow_env, sid):
    client = TestClient(build_app(workspace, token="tok"), base_url="http://127.0.0.1")
    dash = client.get("/?t=tok")
    assert 'http-equiv="refresh"' in dash.text
    detail = client.get(f"/session/{sid}?t=tok")
    # the session page refreshes by script instead: it waits while a field has
    # focus or a chart is enlarged, so a half-typed note is never lost
    assert 'http-equiv="refresh"' not in detail.text
    assert "location.reload()" in detail.text
    # the intervention form page must NOT refresh (it would clear user input)
    from grayson.core.session import Session
    from grayson.interventions import build_request
    from grayson.workspace import Workspace

    s = Session(Workspace.find(), sid)
    iid = s.add_intervention("choose", "pick", "", build_request("choose", {"options": ["a", "b"]}))
    form = client.get(f"/session/{sid}/intervention/{iid}?t=tok")
    assert 'http-equiv="refresh"' not in form.text
