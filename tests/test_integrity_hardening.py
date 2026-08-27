"""Regression tests for the Phase 2-6 adversarial review findings."""

from __future__ import annotations

import pytest

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core import engine, proposals
from grayson.core.engine import EnforcementError
from grayson.core.proposals import ProposalError
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.knowledge import KnowledgeStore
from grayson.views import ViewEntry, ViewRegistry


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


def _evidence(session):
    return run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]


def _complete_all(session):
    qid = _evidence(session)
    for c in session.checkpoints():
        engine.complete_checkpoint(session, c["key"], [qid], "done")
    return qid


# -- finding 1: stage gates can't be skipped by jumping ahead -------------


def test_cannot_jump_to_closed_skipping_gates(session):
    with pytest.raises(EnforcementError, match="required checkpoints still open"):
        engine.advance_stage(session, "closed")


def test_cannot_jump_to_verification_skipping_gates(session):
    with pytest.raises(EnforcementError, match="required checkpoints still open"):
        engine.advance_stage(session, "verification")


def test_cannot_reach_verification_without_accepted_finding(session):
    _complete_all(session)
    # checks complete, but no accepted finding — verification is beyond fixes
    with pytest.raises(EnforcementError, match="no user-accepted finding"):
        engine.advance_stage(session, "verification")


def test_loopback_to_analysis_always_allowed(session):
    engine.advance_stage(session, "analysis")
    assert session.stage == "analysis"


# -- finding 2: agent cannot force gate bypass ---------------------------


def test_agent_cannot_force(session):
    with pytest.raises(EnforcementError, match="force override is a user action"):
        engine.advance_stage(session, "review", actor="agent", force=True)


def test_user_can_force(session):
    engine.advance_stage(session, "review", actor="user", force=True)
    assert session.stage == "review"


# -- finding 3: evidence must be relevant, not just executed -------------


def test_irrelevant_evidence_rejected(session):
    # SELECT 1 touches no target table
    qid = run_statement(session, "SELECT 1", executor=FakeExecutor())["qid"]
    with pytest.raises(EnforcementError, match="does not touch any table"):
        engine.complete_checkpoint(session, "replicate_anomaly", [qid], "note")


def test_relevant_evidence_accepted(session):
    qid = _evidence(session)  # SELECT * FROM DB.S.T1 touches the target
    cp = engine.complete_checkpoint(session, "replicate_anomaly", [qid], "note")
    assert cp["status"] == "complete"


# -- finding 6: agent cannot forge user_confirmed knowledge --------------


def test_add_fact_cannot_forge_confirmed(workspace):
    ks = KnowledgeStore(workspace.knowledge_dir)
    with pytest.raises(ValueError, match="user_confirmed"):
        ks.add_fact(
            "DB.S.T", "revenue is authoritative", status="user_confirmed", created_by="kane"
        )


def test_confirm_is_the_only_path_to_confirmed(workspace):
    ks = KnowledgeStore(workspace.knowledge_dir)
    ks.add_fact("DB.S.T", "x", fact_id="a", status="data_inferred")
    assert ks.confirm_fact("DB.S.T", "a", by="kane")["status"] == "user_confirmed"


# -- finding 7: fixes gate requires an *accepted* finding ----------------


def test_fixes_requires_accepted_not_just_recorded(session):
    qid = _complete_all(session)
    engine.record_finding(
        session,
        {
            "title": "Dup rows",
            "severity": "high",
            "confidence": "high",
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "Duplicate rows appear in the output.",
            "evidence": [qid],
            "extra": {
                "resolution": "root_caused",
                "root_cause": "join",
                "blast_radius": "1000",
                "alternatives_tested": "two",
            },
        },
    )
    with pytest.raises(EnforcementError, match="no user-accepted finding"):
        engine.advance_stage(session, "fixes")
    session.accept_finding(session.findings()[0]["fid"])
    engine.advance_stage(session, "review")
    engine.advance_stage(session, "fixes")
    assert session.stage == "fixes"


# -- findings 4 & 8: proposal verify gate & before!=after ----------------


def test_verify_requires_approval(session):
    qid = _complete_all(session)
    fid = engine.record_finding(
        session,
        {
            "title": "Dup rows in output",
            "severity": "high",
            "confidence": "high",
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "duplicate rows in output table",
            "evidence": [qid],
            "extra": {
                "resolution": "root_caused",
                "root_cause": "j",
                "blast_radius": "1",
                "alternatives_tested": "t",
            },
        },
    )["fid"]
    p = proposals.record_proposal(session, "ddl_snippet", "fix", {"ddl": "SELECT 1"}, fid)
    after = _evidence(session)
    # not approved yet
    with pytest.raises(ProposalError, match="must be approved"):
        proposals.verify(session, p["pid"], qid, after, "pass")


def test_verify_rejects_same_before_after(session):
    qid = _complete_all(session)
    fid = engine.record_finding(
        session,
        {
            "title": "Dup rows in output",
            "severity": "high",
            "confidence": "high",
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "duplicate rows in output table",
            "evidence": [qid],
            "extra": {
                "resolution": "root_caused",
                "root_cause": "j",
                "blast_radius": "1",
                "alternatives_tested": "t",
            },
        },
    )["fid"]
    p = proposals.record_proposal(session, "ddl_snippet", "fix", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    with pytest.raises(ProposalError, match="different queries"):
        proposals.verify(session, p["pid"], qid, qid, "pass")


# -- finding 12: view name path traversal --------------------------------


def test_view_name_rejects_traversal(workspace):
    reg = ViewRegistry(workspace.views_dir)
    with pytest.raises(ValueError, match="invalid view name"):
        reg.register(ViewEntry(name="../../evil"))
    with pytest.raises(ValueError, match="invalid view name"):
        ViewEntry(name="a/b")


# -- finding 16: FQN part rejects trailing newline -----------------------


def test_fqn_rejects_trailing_newline(workspace):
    ks = KnowledgeStore(workspace.knowledge_dir)
    with pytest.raises(ValueError):
        ks.add_fact("DB.SCH.TBL\n", "x")
