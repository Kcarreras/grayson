"""Session reports, cache export, and query rerun."""

from __future__ import annotations

import csv
import json

import pytest
from typer.testing import CliRunner

from conftest import FakeExecutor, close_checkpoint
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
        close_checkpoint(s, key, [qid], "checked")
    engine.waive_checkpoint(s, keys[-1], "static reference table")
    engine.close_session(s, "user", "everything came back sound")

    text = render_markdown(build_report(s))
    assert "clean — checks cleared" in text
    assert "everything came back sound" in text
    # a waived check is neither ticked nor left looking unfinished
    assert "[~]" in text and "**waived**" in text
    assert "static reference table" in text


# -- profiles, narrative, charts, publication ------------------------------


def invoke_err(*args) -> dict:
    result = runner.invoke(app, list(args))
    assert result.exit_code != 0, result.output
    return json.loads(result.stderr or result.output)


def _session_with_work(workspace):
    from grayson.config import GuardSettings
    from grayson.core import engine
    from grayson.core.run import run_statement
    from grayson.core.session import Session

    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    qid = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    for key in engine.workflow_for(s).required_check_keys():
        close_checkpoint(s, key, [qid], "checked")
    return s, qid


def test_narrative_renders_labeled_and_must_cite_evidence(workspace, fake_snow_env, sid):
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    # a narrative citing nothing is refused — it is the story of the evidence
    err = invoke_err("session", "narrate", sid, "--text", "all looked fine to me")
    assert "must cite" in err["error"]
    out = invoke(
        "session", "narrate", sid, "--text", f"Nulls concentrate after the backfill ({run['qid']})."
    )
    assert out["cites"] == [run["qid"]]
    report = invoke("session", "report", sid, "--markdown")
    assert "## Narrative (agent-written)" in report["markdown"]
    assert run["qid"] in report["narrative"]


def test_charts_appear_in_the_report(workspace, fake_snow_env, sid):
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    invoke(
        "chart",
        "add",
        sid,
        "--artifact",
        run["qid"],
        "--kind",
        "bar",
        "-x",
        "VAL",
        "-y",
        "ID",
        "--title",
        "ids by val",
    )
    report = invoke("session", "report", sid, "--markdown")
    assert report["charts"][0]["title"] == "ids by val"
    assert "## Charts" in report["markdown"] and "ids by val" in report["markdown"]


def test_profile_controls_sections_and_audience(workspace, fake_snow_env, sid):
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    cps = invoke("checkpoint", "list", sid)
    invoke("checkpoint", "complete", sid, cps[0]["key"], "-e", run["qid"])
    (workspace.reports_dir).mkdir(parents=True, exist_ok=True)
    (workspace.reports_dir / "exec.yaml").write_text(
        "audience: stakeholder\nsections: [checkpoints, findings]\n"
        'header: "INTERNAL — Data QA"\nfooter: "Questions: #data-quality"\n',
        encoding="utf-8",
    )
    md = invoke("session", "report", sid, "--markdown", "--profile", "exec")["markdown"]
    assert md.startswith("INTERNAL — Data QA")
    assert "Questions: #data-quality" in md
    assert "## Queries" not in md  # section dropped by the profile
    # stakeholder audience summarizes qid lists but keeps the count
    assert "1 executed query cited" in md and run["qid"] not in md.split("## Checkpoints")[1]
    # facts stay deterministic regardless of profile: the JSON carries the ids
    report = invoke("session", "report", sid, "--profile", "exec")
    assert run["qid"] in report["checkpoints"][0]["evidence"]


def test_unknown_profile_fails_loudly_unknown_section_tolerated(workspace, fake_snow_env, sid):
    err = invoke_err("session", "report", sid, "--profile", "nope")
    assert "no report profile" in err["error"]
    # An unknown section is a newer grayson's profile (or a typo): the profile
    # still loads, the section is skipped at render, and the warning names it.
    (workspace.reports_dir).mkdir(parents=True, exist_ok=True)
    (workspace.reports_dir / "newer.yaml").write_text(
        "sections: [checkpoints, from_the_future]\n", encoding="utf-8"
    )
    out = invoke("session", "report", sid, "--markdown", "--profile", "newer")
    assert "from_the_future" in out["profile_warnings"][0]
    assert "## Checkpoints" in out["markdown"]
    assert "from_the_future" not in out["markdown"]


def test_report_publishes_to_library_records_at_close(workspace):
    from grayson.core import engine
    from grayson.records import search_records

    s, qid = _session_with_work(workspace)
    engine.close_session(s, actor="user", note="all four checks came back sound")
    md = workspace.records_dir / s.id / "report.md"
    assert md.is_file()
    text = md.read_text(encoding="utf-8")
    assert "clean — checks cleared" in text and qid in text
    row_path = workspace.records_dir / s.id / "report.json"
    assert row_path.is_file()
    row = json.loads(row_path.read_text(encoding="utf-8"))
    assert row["kind"] == "report" and row["outcome"] == "clean"
    assert "all four checks came back sound" in row["summary"]
    hits = search_records(workspace, "came back sound", kind="report")
    assert hits and hits[0]["session_id"] == s.id


def test_narrate_refused_after_close(workspace):
    from grayson.core import engine

    s, qid = _session_with_work(workspace)
    engine.close_session(s, actor="user")
    result = runner.invoke(app, ["session", "narrate", s.id, "--text", f"late thoughts {qid}"])
    assert result.exit_code == 1
    assert "closed" in (result.stderr or result.output)


# -- charts in the published report: text by default, SVG files on request --


def _closed_session_with_a_chart(workspace):
    from conftest import CHART_ROWS
    from grayson.charts import add_chart
    from grayson.config import GuardSettings
    from grayson.core import engine
    from grayson.core.run import run_statement
    from grayson.core.session import Session

    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    qid = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=CHART_ROWS))["qid"]
    add_chart(s, qid, "bar", "K", ["V"], "V by K", note="the shape")
    for key in engine.workflow_for(s).required_check_keys():
        close_checkpoint(s, key, [qid], "done")
    return s


def test_report_carries_text_charts_by_default(workspace):
    from grayson.core import engine

    s = _closed_session_with_a_chart(workspace)
    engine.close_session(s, actor="user", note="fine", overrides_dir=workspace.workflows_dir)
    folder = workspace.records_dir / s.id
    md = (folder / "report.md").read_text()
    assert "```text" in md and "V by K  [bar" in md
    assert "![" not in md and not (folder / "charts").exists()


def test_profile_svg_publishes_chart_files_beside_the_report(workspace):
    from grayson.report import ReportProfile, load_profile

    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    (workspace.reports_dir / "default.yaml").write_text("charts: svg\n")
    assert load_profile(workspace.reports_dir).charts == "svg"
    assert ReportProfile().charts == "text"
    from grayson.core import engine

    s = _closed_session_with_a_chart(workspace)
    engine.close_session(s, actor="user", note="fine", overrides_dir=workspace.workflows_dir)
    folder = workspace.records_dir / s.id
    md = (folder / "report.md").read_text()
    files = sorted(p.name for p in (folder / "charts").glob("*.svg"))
    assert files  # one per chart the session drew
    assert f"![V by K](charts/{files[0]})" in md and "```text" not in md
    svg = (folder / "charts" / files[0]).read_text()
    assert svg.startswith("<svg") and "gray" in svg  # the export mark, like a download
    # the console serves the published file on the record page
    from fastapi.testclient import TestClient

    from grayson.ui.server import build_app

    client = TestClient(build_app(workspace, token="tok"), base_url="http://127.0.0.1")
    page = client.get(f"/records/{s.id}/report/report?t=tok")
    assert page.status_code == 200 and f"/records/{s.id}/charts/{files[0]}" in page.text
    img = client.get(f"/records/{s.id}/charts/{files[0]}?t=tok")
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/svg+xml")
    assert client.get(f"/records/{s.id}/charts/../report.md?t=tok").status_code == 404
    assert client.get(f"/records/{s.id}/charts/c_999.svg?t=tok").status_code == 404


def test_session_report_out_writes_svgs_when_asked(workspace, fake_snow_env, tmp_path):
    s = _closed_session_with_a_chart(workspace)
    dest = tmp_path / "out" / "report.md"
    dest.parent.mkdir()
    result = CliRunner().invoke(
        app, ["session", "report", s.id, "--out", str(dest), "--charts", "both"]
    )
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    # the session's own chart plus the three table-health's gates required
    assert len(out["chart_files"]) == 4
    assert str(dest.parent / "charts" / "c_001.svg") in out["chart_files"]
    md = dest.read_text()
    assert "![V by K](charts/c_001.svg)" in md and "```text" in md  # both
    assert (dest.parent / "charts" / "c_001.svg").is_file()
    # the default (profile: text) writes no files
    plain = tmp_path / "plain.md"
    result = CliRunner().invoke(app, ["session", "report", s.id, "--out", str(plain)])
    assert json.loads(result.output)["chart_files"] == []
    assert "![" not in plain.read_text() and not (tmp_path / "charts").exists()
    bad = CliRunner().invoke(app, ["session", "report", s.id, "--charts", "png"])
    assert bad.exit_code == 1
