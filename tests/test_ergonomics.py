"""Friction-reduction features: latest alias, status command, hints, UI refresh."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from seekql.cli import app
from seekql.ui.server import build_app

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
    assert "seekql ui serve" in hints
    assert out["session"]["id"] in hints


def test_ui_pages_auto_refresh(workspace, fake_snow_env, sid):
    client = TestClient(build_app(workspace, token="tok"), base_url="http://127.0.0.1")
    dash = client.get("/?t=tok")
    assert 'http-equiv="refresh"' in dash.text
    detail = client.get(f"/session/{sid}?t=tok")
    assert 'http-equiv="refresh"' in detail.text
    # the intervention form page must NOT refresh (it would clear user input)
    from seekql.core.session import Session
    from seekql.interventions import build_request
    from seekql.workspace import Workspace

    s = Session(Workspace.find(), sid)
    iid = s.add_intervention("choose", "pick", "", build_request("choose", {"options": ["a", "b"]}))
    form = client.get(f"/session/{sid}/intervention/{iid}?t=tok")
    assert 'http-equiv="refresh"' not in form.text
