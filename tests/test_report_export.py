"""Session reports, cache export, and query rerun."""

from __future__ import annotations

import csv
import json

import pytest
from typer.testing import CliRunner

from conftest import FakeExecutor
from grayson.cli import app

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def sid(workspace, fake_snow_env) -> str:
    out = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1")
    return out["session"]["id"]


def test_session_report_structure(workspace, fake_snow_env, sid):
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    cps = invoke("checkpoint", "list", sid)
    invoke("checkpoint", "complete", sid, cps[0]["key"], "-e", run["qid"])

    report = invoke("session", "report", sid)
    assert report["session"]["id"] == sid
    assert report["query_stats"]["total"] >= 1
    assert report["query_stats"]["by_status"]["executed"] >= 1
    assert any(c["status"] == "complete" for c in report["checkpoints"])
    assert report["interventions"]["total"] == 0
    assert "open_checks" in report["readiness"]


def test_session_report_markdown_file(workspace, fake_snow_env, sid, tmp_path):
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    dest = tmp_path / "report.md"
    out = invoke("session", "report", sid, "--out", str(dest))
    assert out["written"] == str(dest)
    text = dest.read_text(encoding="utf-8")
    assert f"# grayson session report — {sid}" in text
    assert "## Checkpoints" in text
    assert "## Findings" in text
    assert run["qid"]  # a query ran and the report generated without error


def test_cache_export_csv(workspace, fake_snow_env, sid, tmp_path):
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    dest = tmp_path / "rows.csv"
    out = invoke("cache", "export", sid, run["qid"], "--out", str(dest))
    assert out["format"] == "csv"
    assert out["row_count"] == 5
    with dest.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["ID", "VAL"]
    assert len(rows) == 6  # header + 5 data rows


def test_cache_export_json(workspace, fake_snow_env, sid, tmp_path):
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    dest = tmp_path / "rows.json"
    out = invoke("cache", "export", sid, run["qid"], "--out", str(dest))
    assert out["format"] == "json"
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert len(data) == 5
    assert data[0]["ID"] == 1


def test_cache_export_unknown_qid_fails(workspace, fake_snow_env, sid, tmp_path):
    result = runner.invoke(
        app, ["cache", "export", sid, "q_9999", "--out", str(tmp_path / "x.csv")]
    )
    assert result.exit_code == 1


def test_query_rerun(workspace, fake_snow_env, sid):
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    rerun = invoke("query", "rerun", sid, run["qid"])
    assert rerun["status"] == "executed"
    assert rerun["qid"] != run["qid"]
    log = invoke("query", "log", sid)
    entry = next(e for e in log if e["qid"] == rerun["qid"])
    assert entry["label"] == f"rerun of {run['qid']}"


def test_query_rerun_unknown_qid_fails(workspace, fake_snow_env, sid):
    result = runner.invoke(app, ["query", "rerun", sid, "q_9999"])
    assert result.exit_code == 1


def test_report_distinguishes_a_clean_run_from_an_empty_one(workspace):
    """A clean session has no findings — without the outcome, its report reads as
    a session that gave up rather than one that checked and found nothing."""
    from grayson.config import GuardSettings
    from grayson.core import engine
    from grayson.core.run import run_statement
    from grayson.core.session import Session
    from grayson.report import build_report, render_markdown

    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    qid = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    keys = engine.workflow_for(s).required_check_keys()
    for key in keys[:-1]:
        engine.complete_checkpoint(s, key, [qid], "checked")
    engine.waive_checkpoint(s, keys[-1], "static reference table")
    engine.close_session(s, "user", "everything came back sound")

    text = render_markdown(build_report(s))
    assert "clean — checks cleared" in text
    assert "everything came back sound" in text
    # a waived check is neither ticked nor left looking unfinished
    assert "[~]" in text and "**waived**" in text
    assert "static reference table" in text
