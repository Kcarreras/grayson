from __future__ import annotations

import pytest

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core import engine, proposals
from grayson.core.proposals import ProposalError
from grayson.core.run import run_statement
from grayson.core.session import Session


@pytest.fixture
def session(workspace):
    s = Session.create(
        workspace,
        workflow="bug-hunter",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    return s


def _finding(session):
    qid = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    f = engine.record_finding(
        session,
        {
            "title": "Dup rows",
            "severity": "high",
            "confidence": "high",
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "Duplicate rows appear in the output table.",
            "evidence": [qid],
            "extra": {
                "resolution": "root_caused",
                "root_cause": "join fan-out",
                "blast_radius": "1000 rows",
                "alternatives_tested": "two ruled out",
            },
        },
    )
    return f["fid"], qid


# -- payload validation --------------------------------------------------


def test_file_diff_requires_target_and_body(session):
    with pytest.raises(ProposalError, match="target_file"):
        proposals.build_proposal_payload("file_diff", {"diff": "x"})
    with pytest.raises(ProposalError, match="diff.*new_content"):
        proposals.build_proposal_payload("file_diff", {"target_file": "a.sql"})


def test_ddl_requires_ddl(session):
    with pytest.raises(ProposalError, match="ddl"):
        proposals.build_proposal_payload("ddl_snippet", {})


def test_unknown_kind(session):
    with pytest.raises(ProposalError, match="unknown proposal kind"):
        proposals.build_proposal_payload("magic", {})


# -- lifecycle -----------------------------------------------------------


def test_record_and_approve(session):
    fid, _ = _finding(session)
    p = proposals.record_proposal(
        session,
        "file_diff",
        "Add dedup",
        {
            "target_file": "models/output.sql",
            "new_content": "-- fixed\nSELECT DISTINCT ...",
            "rationale": "dedup at the join",
        },
        fid,
    )
    assert p["status"] == "proposed"
    approved = proposals.decide(session, p["pid"], approve=True)
    assert approved["status"] == "approved"
    applied = proposals.mark_applied(session, p["pid"])
    assert applied["status"] == "applied"


def test_reject(session):
    fid, _ = _finding(session)
    p = proposals.record_proposal(
        session,
        "ddl_snippet",
        "Recreate view",
        {"ddl": "CREATE OR REPLACE VIEW v AS SELECT 1"},
        fid,
    )
    rejected = proposals.decide(session, p["pid"], approve=False)
    assert rejected["status"] == "rejected"


def test_cannot_decide_twice(session):
    fid, _ = _finding(session)
    p = proposals.record_proposal(session, "ddl_snippet", "x", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    with pytest.raises(ProposalError, match="not pending"):
        proposals.decide(session, p["pid"], approve=False)


def test_cannot_apply_unapproved(session):
    fid, _ = _finding(session)
    p = proposals.record_proposal(session, "ddl_snippet", "x", {"ddl": "SELECT 1"}, fid)
    with pytest.raises(ProposalError, match="must be approved"):
        proposals.mark_applied(session, p["pid"])


def test_proposal_unknown_finding(session):
    with pytest.raises(ProposalError, match="unknown finding"):
        proposals.record_proposal(session, "ddl_snippet", "x", {"ddl": "SELECT 1"}, "f_999")


# -- verification --------------------------------------------------------


def test_verify_requires_executed_evidence(session):
    fid, _ = _finding(session)
    p = proposals.record_proposal(session, "ddl_snippet", "x", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    with pytest.raises(ProposalError, match="missing"):
        proposals.verify(session, p["pid"], "q_9990", "q_9991", "pass")


def test_verify_pass_marks_verified(session):
    fid, before = _finding(session)
    p = proposals.record_proposal(session, "ddl_snippet", "x", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    # simulate a post-fix re-run: anomaly count now zero
    ex = FakeExecutor(rows=[])
    after = run_statement(session, "SELECT * FROM DB.S.T1 WHERE dup > 1", executor=ex)["qid"]
    result = proposals.verify(session, p["pid"], before, after, "pass", "anomaly gone")
    assert result["status"] == "verified"
    assert result["verification"]["comparison"]["after_empty"] is True


def test_verify_fail_marks_failed(session):
    fid, before = _finding(session)
    p = proposals.record_proposal(session, "ddl_snippet", "x", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    after = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    result = proposals.verify(session, p["pid"], before, after, "fail", "still broken")
    assert result["status"] == "verification_failed"


@pytest.mark.parametrize("verdict", ["pass", "fail"])
def test_verification_advances_timeline_through_gates(session, verdict):
    from fastapi.testclient import TestClient

    from conftest import close_checkpoint
    from grayson.ui.server import build_app

    fid, before = _finding(session)
    session.accept_finding(fid)
    for checkpoint in session.checkpoints():
        close_checkpoint(session, checkpoint["key"], [before])
    engine.advance_stage(session, "fixes")
    p = proposals.record_proposal(session, "ddl_snippet", "Fix", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    after = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    proposals.verify(session, p["pid"], before, after, verdict)
    assert session.stage == "verification"
    client = TestClient(build_app(session.workspace, token="test"), base_url="http://127.0.0.1")
    page = client.get(f"/session/{session.id}?t=test")
    assert page.status_code == 200
    assert f"1 checked · {int(verdict == 'pass')} passed" in page.text


def test_verification_does_not_bypass_open_checkpoint_gate(session):
    fid, before = _finding(session)
    session.set_stage("fixes")
    p = proposals.record_proposal(session, "ddl_snippet", "Fix", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    after = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    proposals.verify(session, p["pid"], before, after, "pass")
    assert session.stage == "fixes"


def test_verify_bad_verdict(session):
    fid, before = _finding(session)
    p = proposals.record_proposal(session, "ddl_snippet", "x", {"ddl": "SELECT 1"}, fid)
    after = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    with pytest.raises(ProposalError, match="verdict"):
        proposals.verify(session, p["pid"], before, after, "maybe")
