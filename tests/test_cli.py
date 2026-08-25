"""End-to-end CLI flow against the fake snow binary."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from grayson.cli import app

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


def test_workflow_list_and_show(workspace):
    names = {w["name"] for w in invoke("workflow", "list")}
    assert "bug-hunter" in names
    show = invoke("workflow", "show", "bug-hunter")
    assert show["findings_schema"] == "bug_hunter_v1"


def test_unknown_workflow_start_fails(workspace, fake_snow_env):
    result = runner.invoke(app, ["session", "start", "--workflow", "nope", "--table", "DB.S.T1"])
    assert result.exit_code == 1


def test_checkpoint_and_findings_flow(workspace, fake_snow_env):
    out = invoke(
        "session",
        "start",
        "--workflow",
        "bug-hunter",
        "--table",
        "DB.S.T1",
        "--title",
        "bug",
    )
    sid = out["session"]["id"]
    assert out["workflow"]["required_checks"]

    # checkpoints seeded and open
    cps = invoke("checkpoint", "list", sid)
    assert cps and all(c["status"] == "open" for c in cps)

    # cannot enter review yet
    blocked = runner.invoke(app, ["session", "advance", sid, "--to", "review"])
    assert blocked.exit_code == 1

    # run a query for evidence
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    qid = run["qid"]

    # completing a checkpoint without evidence fails
    no_ev = runner.invoke(app, ["checkpoint", "complete", sid, "replicate_anomaly"])
    assert no_ev.exit_code == 1

    # complete every checkpoint with evidence
    for c in cps:
        invoke("checkpoint", "complete", sid, c["key"], "-e", qid, "--note", "done")

    ready = invoke("session", "readiness", sid)
    assert ready["checks_complete"]

    # add a finding
    finding = {
        "title": "Fan-out duplicates in output",
        "severity": "high",
        "confidence": "high",
        "summary": "A one-to-many join duplicates rows in the final table.",
        "evidence": [qid],
        "extra": {
            "root_cause": "join fan-out on non-unique key",
            "blast_radius": "1200 rows since 2026-08-01",
            "alternatives_tested": "source dup and dedup bug both ruled out",
        },
    }
    added = invoke("finding", "add", sid, "--json", json.dumps(finding))
    assert added["fid"] == "f_001"

    # now review is reachable
    adv = invoke("session", "advance", sid, "--to", "review")
    assert adv["stage"] == "review"

    invoke("finding", "accept", sid, "f_001")
    assert invoke("finding", "show", sid, "f_001")["accepted"] is True

    # fixes reachable now that a finding exists
    invoke("session", "advance", sid, "--to", "fixes")


def test_upgrade_dev_checkout_gets_instructions(monkeypatch):
    import grayson.cli as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv")

    def fake_run(args, **kwargs):
        class R:
            returncode = 0
            stdout = "some-other-tool v1.0.0\n"
            stderr = ""

        assert args[:3] == ["/usr/bin/uv", "tool", "list"]
        return R()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    out = invoke("upgrade")
    assert out["upgraded"] is False
    assert "git pull" in out["detail"]


def test_upgrade_runs_uv_tool_upgrade(monkeypatch):
    import grayson.cli as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)

        class R:
            returncode = 0
            stdout = "grayson-sql v0.1.0\n" if args[2] == "list" else "Updated grayson-sql\n"
            stderr = ""

        return R()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    out = invoke("upgrade")
    assert out["upgraded"] is True
    assert calls[-1] == ["/usr/bin/uv", "tool", "upgrade", "grayson-sql"]


def test_upgrade_without_uv_fails(monkeypatch):
    import grayson.cli as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    result = runner.invoke(app, ["upgrade"])
    assert result.exit_code == 1
