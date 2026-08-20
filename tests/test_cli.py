"""End-to-end CLI flow against the fake snow binary."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from seekql.cli import app

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def sid(workspace, fake_snow_env) -> str:
    out = invoke(
        "session",
        "start",
        "--workflow",
        "table-health",
        "--table",
        "DB.S.T1",
        "--guard-profile",
        "moderate",
        "--title",
        "e2e",
    )
    assert out["metadata_snapshot"]["status"] == "ok"
    return out["session"]["id"]


def test_full_flow(workspace, fake_snow_env, sid):
    # doctor: snow check may fail (no real snow) but workspace check passes
    out = invoke("session", "status", sid)
    assert out["stage"] == "setup"

    worker = invoke("worker", "join", sid, "--label", "main")["worker"]

    check = invoke("guard", "check", sid, "-q", "DELETE FROM DB.S.T1")
    assert check["allowed"] is False

    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1", "--worker", worker)
    assert run["status"] == "executed"
    assert run["row_count"] == 5
    assert len(run["preview"]) == 5

    found = invoke("cache", "find", sid, "--table", "DB.S.T1")
    assert found and found[0]["qid"] == run["qid"]

    local = invoke("cache", "query", sid, "-q", f"SELECT COUNT(*) AS n FROM {run['qid']}")
    assert local["rows"][0]["n"] == 5

    shown = invoke("cache", "show", sid, run["qid"], "--rows", "2")
    assert len(shown["preview"]) == 2

    log = invoke("query", "log", sid)
    assert any(e["qid"] == run["qid"] for e in log)

    invoke("session", "advance", sid, "--to", "analysis")
    invoke("session", "close", sid)
    assert invoke("session", "status", sid)["stage"] == "closed"


def test_rejected_query_exit_zero_with_verdict(workspace, fake_snow_env, sid):
    out = invoke("query", "run", sid, "-q", "DROP TABLE DB.S.T1")
    assert out["status"] == "rejected"
    assert out["rule"] == "statement_type"
    assert out["suggestion"]


def test_auth_failure_pauses_agent(workspace, fake_snow_env, sid):
    out = invoke("query", "run", sid, "-q", "SELECT 'FAIL_AUTH' FROM DB.S.T1")
    assert out["status"] == "auth_required"
    assert "action_needed" in out


def test_budget_extension(workspace, fake_snow_env, sid):
    out = invoke("session", "budget", sid, "--cap", "7")
    assert out["guard"]["budget_cap"] == 7


def test_init_is_idempotent_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    invoke("init", str(tmp_path / "w1"))
    result = runner.invoke(app, ["init", str(tmp_path / "w1")])
    assert result.exit_code == 1
