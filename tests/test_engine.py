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
                "affected_objects": ["DB.S.T1"],
                "reproduction": "re-run the cited query",
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
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "Join fan-out creates duplicate rows in the output.",
            "evidence": qids,
            "extra": {
                "resolution": "root_caused",
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


# -- waiving inapplicable checks ------------------------------------------


def _clear_all_checks(session, waive_last=False):
    """Close every required check with real evidence (optionally waiving one)."""
    qids = _run(session, 1)
    keys = engine.workflow_for(session).required_check_keys()
    for key in keys[:-1] if waive_last else keys:
        engine.complete_checkpoint(session, key, qids, "done")
    if waive_last:
        engine.waive_checkpoint(session, keys[-1], "no lineage upstream of this table")
    return qids


def test_agent_cannot_waive_its_own_gate(session):
    with pytest.raises(EnforcementError, match="user action"):
        engine.waive_checkpoint(session, "upstream_trace", "does not apply", actor="agent")
    assert session.checkpoint("upstream_trace")["status"] == "open"


def test_waive_requires_a_reason(session):
    with pytest.raises(EnforcementError, match="requires a reason"):
        engine.waive_checkpoint(session, "upstream_trace", "   ")


def test_waive_unknown_checkpoint_rejected(session):
    with pytest.raises(EnforcementError, match="unknown checkpoint"):
        engine.waive_checkpoint(session, "not_a_check", "n/a")


def test_waived_check_satisfies_the_gate_but_stays_visible(session):
    _clear_all_checks(session, waive_last=True)
    ready = engine.readiness(session)
    assert ready["checks_complete"] is True
    assert ready["open_checks"] == []
    # a waived gate never reads as a closed one
    waived = ready["waived_checks"]
    assert [w["key"] for w in waived] == ["rule_out_alternatives"]
    assert waived[0]["reason"] == "no lineage upstream of this table"
    assert waived[0]["by"] == "user"
    assert session.checkpoint("rule_out_alternatives")["evidence"] == []
    engine.advance_stage(session, "review", actor="user")
    assert session.stage == "review"


def test_reopening_a_waived_check_reimposes_the_gate(session):
    _clear_all_checks(session, waive_last=True)
    session.reopen_checkpoint("rule_out_alternatives")
    ready = engine.readiness(session)
    assert ready["open_checks"] == ["rule_out_alternatives"]
    assert ready["waived_checks"] == []


# -- clean close ----------------------------------------------------------


def test_clean_run_can_close_without_inventing_a_finding(session):
    _clear_all_checks(session)
    ready = engine.readiness(session)
    assert ready["clean_close_available"] is True
    assert "clean result" in ready["next_action"]
    out = engine.close_session(session, actor="user", note="nothing anomalous")
    assert session.stage == "closed"
    assert session.outcome == "clean"
    assert out["outcome"] == "clean"


def test_clean_close_is_a_user_action(session):
    _clear_all_checks(session)
    with pytest.raises(EnforcementError, match="user action"):
        engine.close_session(session, actor="agent")
    assert session.stage != "closed"


def test_clean_close_blocked_while_checks_are_open(session):
    with pytest.raises(EnforcementError, match="checkpoints still open"):
        engine.close_session(session, actor="user")


def test_clean_close_blocked_while_a_finding_awaits_judgement(session):
    qids = _clear_all_checks(session)
    engine.record_finding(session, _finding(qids))
    ready = engine.readiness(session)
    assert ready["clean_close_available"] is False
    with pytest.raises(EnforcementError, match="awaiting the user"):
        engine.close_session(session, actor="user")


def test_rejected_findings_do_not_block_a_clean_close(session):
    qids = _clear_all_checks(session)
    got = engine.record_finding(session, _finding(qids))
    session.reject_finding(got["fid"], "that spike is a known backfill")
    # the user has already said it was not real — that is itself a clean result
    assert engine.readiness(session)["clean_close_available"] is True
    engine.close_session(session, actor="user")
    assert session.outcome == "clean"


def test_advance_to_closed_names_the_clean_route_instead_of_dead_ending(session):
    _clear_all_checks(session)
    with pytest.raises(EnforcementError, match="closes as one"):
        engine.advance_stage(session, "closed", actor="agent")


def test_session_with_accepted_findings_closes_as_findings(session):
    qids = _clear_all_checks(session)
    got = engine.record_finding(session, _finding(qids))
    session.accept_finding(got["fid"])
    assert engine.readiness(session)["clean_close_available"] is False
    engine.close_session(session, actor="user")
    assert session.stage == "closed"
    assert session.outcome == "findings"


def _finding(qids: list[str]) -> dict:
    return {
        "title": "Duplicate order ids",
        "severity": "high",
        "confidence": "high",
        "affected_objects": ["DB.S.T1"],
        "reproduction": "re-run the cited query",
        "summary": "Order ids repeat after the promo join.",
        "evidence": qids,
        "extra": {
            "resolution": "root_caused",
            "root_cause": "non-unique promo codes",
            "blast_radius": "412 rows",
            "alternatives_tested": "source duplication ruled out",
        },
    }


def test_forced_close_is_not_recorded_as_clean(session):
    # a bypass is not a human vouching that the data was sound
    engine.advance_stage(session, "closed", actor="user", force=True)
    assert session.stage == "closed"
    assert session.outcome == ""


# -- ordering and evidence relevance --------------------------------------


def test_dependency_blocks_out_of_order_closure(session):
    qids = _run(session, 1)
    # bug-hunter: no cause-hunting until the anomaly reproduces
    with pytest.raises(EnforcementError, match="depends on"):
        engine.complete_checkpoint(session, "upstream_trace", qids, "traced")
    engine.complete_checkpoint(session, "replicate_anomaly", qids, "reproduced")
    engine.complete_checkpoint(session, "upstream_trace", qids, "traced")
    assert session.checkpoint("upstream_trace")["status"] == "complete"


def test_a_waived_prerequisite_still_unblocks(session):
    qids = _run(session, 1)
    engine.waive_checkpoint(session, "replicate_anomaly", "user supplied the failing rows")
    engine.complete_checkpoint(session, "upstream_trace", qids, "traced")
    assert session.checkpoint("upstream_trace")["status"] == "complete"


def test_suggested_check_can_be_closed_but_never_gates(session):
    qids = _run(session, 1)
    engine.complete_checkpoint(session, "onset_dating", qids, "first appears 08-14")
    ready = engine.readiness(session)
    assert "onset_dating" not in ready["required_checks"]
    assert next(c for c in ready["suggested_checks"] if c["key"] == "onset_dating")["done"]
    assert ready["checks_complete"] is False  # the required ones are still open


def test_off_scope_evidence_is_reported_not_rejected(session):
    """upstream_trace walks lineage through tables that are NOT session targets;
    those probes are legitimate evidence. Requiring every qid to touch scope
    would just teach agents to staple a target table onto each one."""
    in_scope = _run(session, 1)
    out = run_statement(session, "SELECT * FROM DB.S.OTHER", executor=FakeExecutor())
    engine.complete_checkpoint(session, "replicate_anomaly", in_scope, "reproduced")
    cp = engine.complete_checkpoint(
        session, "upstream_trace", in_scope + [out["qid"]], "walked upstream"
    )
    assert cp["status"] == "complete"
    assert cp["evidence_off_scope"] == [out["qid"]]
    assert any(e["type"] == "evidence_off_scope" for e in session.events(20))


def test_wholly_off_scope_evidence_is_still_refused(session):
    out = run_statement(session, "SELECT * FROM DB.S.OTHER", executor=FakeExecutor())
    with pytest.raises(EnforcementError, match="does not touch any table"):
        engine.complete_checkpoint(session, "replicate_anomaly", [out["qid"]], "n/a")
