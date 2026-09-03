from __future__ import annotations

import threading

import pytest

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core.run import run_statement, snapshot_metadata
from grayson.core.session import Session


@pytest.fixture
def session(workspace):
    return Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=100, timeout_seconds=30, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
        title="test session",
    )


def test_create_and_summary(workspace, session):
    s = session.summary()
    assert s["workflow"] == "table-health"
    assert s["stage"] == "setup"
    assert s["targets"] == ["DB.S.T1"]
    assert session.id in workspace.list_session_ids()


def test_worker_join(session):
    w1 = session.worker_join("angle-a")
    w2 = session.worker_join("angle-b")
    assert w1 != w2
    assert len(session.workers()) == 2


def test_qid_allocation_is_safe_under_threads(session):
    ids: list[str] = []
    lock = threading.Lock()

    def alloc():
        qid = session.allocate_qid(None, "SELECT 1")
        with lock:
            ids.append(qid)

    threads = [threading.Thread(target=alloc) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == len(set(ids)) == 12


def test_stage_transitions(session):
    session.set_stage("analysis")
    assert session.stage == "analysis"
    with pytest.raises(ValueError):
        session.set_stage("nonsense")


def test_run_statement_executes_and_caches(session):
    ex = FakeExecutor()
    out = run_statement(session, "SELECT * FROM DB.S.T1", executor=ex)
    assert out["status"] == "executed"
    assert out["qid"] == "q_0001"
    assert out["row_count"] == 5
    assert session.cache.get("q_0001") is not None
    assert session.executed_count() == 1
    # timeout propagated to executor
    assert ex.calls[0][1] == 30


def test_first_executed_query_auto_advances_setup_to_analysis(session):
    assert session.stage == "setup"
    run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())
    assert session.stage == "analysis"
    # only the setup→analysis hop is automatic: later stages stay agent-declared
    session.set_stage("synthesis")
    run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())
    assert session.stage == "synthesis"


def test_rejected_query_does_not_advance_stage(session):
    assert session.stage == "setup"
    run_statement(session, "DROP TABLE DB.S.T1", executor=FakeExecutor())
    assert session.stage == "setup"


def test_run_statement_rejected_is_audited(session):
    out = run_statement(session, "DROP TABLE DB.S.T1", executor=FakeExecutor())
    assert out["status"] == "rejected"
    row = session.query_row(out["qid"])
    assert row["status"] == "rejected"
    assert session.executed_count() == 0


def test_run_statement_auth_required_surfaces_action(session):
    ex = FakeExecutor(status="auth_required", error="Authentication token has expired")
    out = run_statement(session, "SELECT 1", executor=ex)
    assert out["status"] == "auth_required"
    assert "re-authenticate" in out["action_needed"]


def test_snapshot_metadata_and_freshness_capture(session):
    ex = FakeExecutor()
    snap = snapshot_metadata(session, executor=ex)
    assert snap["status"] == "ok"
    assert "DB.S.T1" in snap["tables"]
    out = run_statement(session, "SELECT * FROM DB.S.T1", executor=ex)
    sidecar = session.cache.get(out["qid"])
    assert sidecar["source_last_altered"] == {"DB.S.T1": "2026-08-20 00:00:00"}


def test_scope_extension(session):
    session.add_scope(["db.s.t2"])
    assert "DB.S.T2" in session.scope_tables


def test_scrub_keeps_audit(session):
    run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())
    assert session.scrub_data() == 1
    assert session.query_row("q_0001")["status"] == "executed"
    assert session.cache.artifact_tables() == set()


def test_create_draws_a_fresh_id_on_collision(workspace, monkeypatch):
    """Session ids carry a second-resolution stamp and two random bytes, so
    two sessions created in the same second can draw the same id; the second
    must get a fresh one rather than fail on the existing directory."""
    import grayson.core.session as session_module
    from grayson.core.session import Session

    ids = iter(["20260903-113001-dead", "20260903-113001-dead", "20260903-113001-beef"])
    monkeypatch.setattr(session_module, "new_session_id", lambda: next(ids))
    guard = workspace.config.guard_profiles["moderate"].model_copy()
    first = Session.create(
        workspace, workflow="bug-hunter", targets=["DB.S.T"], guard=guard, guard_profile="moderate"
    )
    second = Session.create(
        workspace, workflow="bug-hunter", targets=["DB.S.T"], guard=guard, guard_profile="moderate"
    )
    assert (first.id, second.id) == ("20260903-113001-dead", "20260903-113001-beef")
