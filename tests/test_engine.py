from __future__ import annotations

import pytest

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core import engine
from grayson.core.engine import EnforcementError
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


def _run(session, n=1):
    qids = []
    for _ in range(n):
        out = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())
        qids.append(out["qid"])
    return qids


def test_checkpoints_seeded(session):
    keys = {c["key"] for c in session.checkpoints()}
    assert "replicate_anomaly" in keys
    assert all(c["status"] == "open" for c in session.checkpoints())


def test_checkpoint_requires_evidence(session):
    with pytest.raises(EnforcementError, match="evidence required"):
        engine.complete_checkpoint(session, "replicate_anomaly", [], "note")


def test_checkpoint_rejects_nonexistent_evidence(session):
    with pytest.raises(EnforcementError, match="not executed"):
        engine.complete_checkpoint(session, "replicate_anomaly", ["q_9999"], "note")


def test_checkpoint_rejects_rejected_query_as_evidence(session):
    # a rejected query exists in the audit log but is not "executed"
    out = run_statement(session, "DROP TABLE DB.S.T1", executor=FakeExecutor())
    assert out["status"] == "rejected"
    with pytest.raises(EnforcementError, match="not executed"):
        engine.complete_checkpoint(session, "replicate_anomaly", [out["qid"]], "note")


def test_checkpoint_completes_with_real_evidence(session):
    qids = _run(session, 1)
    cp = engine.complete_checkpoint(session, "replicate_anomaly", qids, "reproduced")
    assert cp["status"] == "complete"
    assert cp["evidence"] == qids


def test_unknown_checkpoint(session):
    qids = _run(session, 1)
    with pytest.raises(EnforcementError, match="unknown checkpoint"):
        engine.complete_checkpoint(session, "not_a_check", qids, "x")


def test_finding_requires_valid_schema_and_evidence(session):
    qids = _run(session, 1)
    # missing bug_hunter required extras
    with pytest.raises(EnforcementError, match="root_cause"):
        engine.record_finding(
            session,
            {
                "title": "Anomaly found",
                "severity": "high",
                "confidence": "high",
                "summary": "Something is off in the data pipeline output.",
                "evidence": qids,
            },
        )


def test_finding_recorded(session):
    qids = _run(session, 1)
    f = engine.record_finding(
        session,
        {
            "title": "Fan-out duplicates",
            "severity": "high",
            "confidence": "high",
            "summary": "Join fan-out creates duplicate rows in the output.",
            "evidence": qids,
            "extra": {
                "root_cause": "one-to-many join",
                "blast_radius": "1200 rows",
                "alternatives_tested": "two ruled out",
            },
        },
    )
    assert f["fid"] == "f_001"
    assert session.findings()[0]["title"] == "Fan-out duplicates"


def test_stage_gate_blocks_review_until_checks_complete(session):
    qids = _run(session, 1)
    with pytest.raises(EnforcementError, match="required checkpoints still open"):
        engine.advance_stage(session, "review")
    # complete all checks
    for c in session.checkpoints():
        engine.complete_checkpoint(session, c["key"], qids, "done")
    result = engine.advance_stage(session, "review")
    assert session.stage == "review"
    assert result["checks_complete"]


def test_stage_gate_force_overrides(session):
    engine.advance_stage(session, "review", force=True)
    assert session.stage == "review"
    events = [e["type"] for e in session.events()]
    assert "stage_gate_forced" in events


def test_fixes_gate_requires_accepted_finding(session):
    # complete checks first so the cumulative gate reaches the findings requirement
    qids = _run(session, 1)
    for c in session.checkpoints():
        engine.complete_checkpoint(session, c["key"], qids, "done")
    with pytest.raises(EnforcementError, match="no user-accepted finding"):
        engine.advance_stage(session, "fixes")


def test_readiness_report(session):
    r = engine.readiness(session)
    assert set(r["open_checks"]) == set(r["required_checks"])
    assert not r["checks_complete"]
