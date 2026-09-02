"""The session brief: one read for an agent resuming in a fresh context."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from conftest import FakeExecutor
from grayson.charts import add_chart
from grayson.cli import app
from grayson.config import GuardSettings
from grayson.core import engine
from grayson.core.brief import build_brief, render_brief
from grayson.core.proposals import record_proposal
from grayson.core.run import run_statement
from grayson.core.session import Session

runner = CliRunner()

DAILY = [{"DAY": f"2026-08-{d:02d}", "NULL_RATE": d / 100} for d in range(1, 6)]


def _session(workspace) -> Session:
    return Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=500, timeout_seconds=30, budget_warn=0, budget_cap=40),
        guard_profile="moderate",
        title="Orders health",
    )


def _populate(workspace, s: Session) -> dict:
    s.set_setup_inputs({"grain": "one row per order", "table": "DB.S.T1"})
    q1 = run_statement(
        s, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=DAILY), label="null rate by day"
    )["qid"]
    q2 = run_statement(s, "SELECT COUNT(*) AS N FROM DB.S.T1", executor=FakeExecutor())["qid"]
    rejected = run_statement(s, "DELETE FROM DB.S.T1", executor=FakeExecutor())
    assert rejected["status"] == "rejected"
    engine.complete_checkpoint(s, "grain_uniqueness", [q2], note="ID unique at the grain")
    engine.waive_checkpoint(s, "freshness", "static reference table", actor="user")
    fid = engine.record_finding(
        s,
        {
            "title": "NULL rate climbs through August",
            "severity": "high",
            "confidence": "medium",
            "summary": "The NULL rate rises daily from 1% to 5% over the sample.",
            "evidence": [q1],
            "affected_objects": ["DB.S.T1.VAL"],
        },
    )["fid"]
    rejected_fid = engine.record_finding(
        s,
        {
            "title": "Column VAL looks unused",
            "severity": "low",
            "confidence": "low",
            "summary": "VAL is constant in the sample; possibly a dead column.",
            "evidence": [q1],
        },
    )["fid"]
    s.accept_finding(fid)
    s.reject_finding(rejected_fid, "VAL feeds the nightly export")
    iid = s.add_intervention(
        "confirm_semantics",
        "Is one row per order the grain?",
        "Confirm the grain",
        {"statement": "one row per order", "context": "", "sample": []},
    )
    s.respond_intervention(iid, {"confirmed": True, "note": "yes, since the 2025 migration"})
    open_iid = s.add_intervention(
        "choose",
        "Which export matters?",
        "Pick the downstream consumer to prioritise",
        {"options": ["finance", "marketing"], "question": "which?", "multi": False},
    )
    pid = record_proposal(
        s,
        "file_diff",
        "Coalesce VAL in the model",
        {"target_file": "models/t1.sql", "diff": "--- a\n+++ b\n-VAL\n+COALESCE(VAL, 0)"},
        fid,
    )["pid"]
    add_chart(s, q1, "line", "DAY", ["NULL_RATE"], "NULL rate by day")
    s.set_meta("report_narrative", f"The rate climbs ({q1}).")
    return {"q1": q1, "q2": q2, "fid": fid, "rejected": rejected_fid, "iid": iid,
            "open_iid": open_iid, "pid": pid}  # fmt: skip


def test_brief_carries_the_whole_record(workspace):
    s = _session(workspace)
    ids = _populate(workspace, s)
    brief = build_brief(s, workspace.workflows_dir)

    assert brief["id"] == s.id and brief["title"] == "Orders health"
    assert brief["workflow"] == "table-health" and brief["stage"] == "analysis"
    assert brief["setup_inputs"]["grain"] == "one row per order"
    assert brief["guard"]["budget_used"] == 2 and brief["guard"]["budget_cap"] == 40

    checks = {c["key"]: c for c in brief["checkpoints"]}
    assert checks["grain_uniqueness"]["status"] == "complete"
    assert checks["grain_uniqueness"]["evidence"] == [ids["q2"]]
    assert checks["freshness"]["status"] == "waived"
    assert checks["freshness"]["note"] == "static reference table"

    findings = {f["fid"]: f for f in brief["findings"]}
    assert findings[ids["fid"]]["status"] == "accepted"
    assert findings[ids["rejected"]]["status"] == "rejected"
    assert findings[ids["rejected"]]["rejected_reason"] == "VAL feeds the nightly export"

    interventions = {i["iid"]: i for i in brief["interventions"]}
    assert interventions[ids["iid"]]["status"] == "answered"
    assert interventions[ids["iid"]]["response"]["note"] == "yes, since the 2025 migration"
    assert interventions[ids["open_iid"]]["status"] == "open"

    assert brief["proposals"][0]["pid"] == ids["pid"]
    assert brief["proposals"][0]["finding"] == ids["fid"]
    assert brief["proposals"][0]["verification"] is None

    q = brief["queries"]
    assert q["executed"] == 2 and q["rejected_by_guard"] == 1
    assert [r["qid"] for r in q["recent"]] == [ids["q2"], ids["q1"]]  # newest first
    assert q["recent"][1]["label"] == "null rate by day"
    assert q["recent"][1]["tables"] == ["DB.S.T1"]
    assert brief["charts"][0]["title"] == "NULL rate by day"
    assert brief["narrative"].startswith("The rate climbs")
    assert brief["readiness"]["open_checks"]  # table-health has more required checks
    assert "freshness" in brief["readiness"]["waived_checks"]
    assert brief["readiness"]["next_action"]


def test_brief_text_reads_top_to_bottom(workspace):
    s = _session(workspace)
    ids = _populate(workspace, s)
    text = render_brief(build_brief(s, workspace.workflows_dir))

    assert text.startswith(f"# Session {s.id} — Orders health")
    assert "budget 2 used of 40 cap" in text
    assert "- grain: one row per order" in text
    assert f"[x] grain_uniqueness: complete ({ids['q2']})" in text
    assert "[~] freshness: waived — static reference table" in text
    assert "NULL rate climbs through August" in text and "— accepted" in text
    assert "rejected: VAL feeds the nightly export" in text
    assert "yes, since the 2025 migration" in text  # the user's answer is in the brief
    assert "OPEN (choose)" in text and "awaiting the user" in text
    assert "do not re-ask" in text
    assert f"{ids['pid']} file_diff" in text and f"(for {ids['fid']})" in text
    assert "2 executed, 1 rejected by the guard" in text
    assert "[null rate by day]" in text and "DB.S.T1" in text
    assert "## Charts" in text and "## Narrative" in text
    assert text.rstrip().splitlines()[-2:][0].startswith("close the remaining checkpoints") or (
        "## Next" in text
    )
    # sections come in the order an agent needs them
    order = ["## Setup inputs", "## Checkpoints", "## Findings", "## Interventions",
             "## Proposals", "## Queries", "## Charts", "## Narrative", "## Next"]  # fmt: skip
    positions = [text.index(h) for h in order]
    assert positions == sorted(positions)


def test_brief_of_a_fresh_session_is_honest_about_emptiness(workspace):
    s = _session(workspace)
    brief = build_brief(s, workspace.workflows_dir)
    text = render_brief(brief)
    assert brief["findings"] == [] and brief["interventions"] == [] and brief["charts"] == []
    assert "- none recorded yet" in text and "## Proposals" not in text
    assert "## Queries — 0 executed" in text


def test_brief_cli_and_mcp(workspace, fake_snow_env):
    import asyncio

    from grayson.mcp.server import build_server

    started = runner.invoke(
        app,
        ["session", "start", "--workflow", "table-health", "--table", "DB.S.T1",
         "--guard-profile", "moderate", "--skip-snapshot"],
    )  # fmt: skip
    sid = json.loads(started.output)["session"]["id"]
    s = Session(workspace, sid)
    q = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=DAILY))["qid"]
    engine.complete_checkpoint(s, "grain_uniqueness", [q])

    result = runner.invoke(app, ["session", "brief", sid])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["id"] == sid and "grain_uniqueness: complete" in out["text"]

    server = build_server(workspace)
    mcp_out = json.loads(
        asyncio.run(server.call_tool("session_brief", {"session_id": sid})).content[0].text
    )
    assert mcp_out["text"] == out["text"]
    missing = json.loads(
        asyncio.run(server.call_tool("session_brief", {"session_id": "nope"})).content[0].text
    )
    assert "error" in missing


def test_protocol_and_instructions_point_at_the_brief():
    from grayson.harness.generate import PROTOCOL
    from grayson.mcp.server import INSTRUCTIONS

    assert "session brief" in PROTOCOL
    assert "session_brief" in INSTRUCTIONS
