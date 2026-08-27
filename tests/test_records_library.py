"""Records compound across the team: accepted findings and verified fixes
publish into the library, and teammates read them from their own workspaces."""

from __future__ import annotations

import json

import pytest

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core import engine, proposals
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.identity import set_user_id
from grayson.library import set_library_config
from grayson.records import get_record, search_library_records, search_records
from grayson.workspace import Workspace


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


def _finding(session, title="Dup rows"):
    qid = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    f = engine.record_finding(
        session,
        {
            "title": title,
            "severity": "high",
            "confidence": "high",
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


def test_accept_publishes_to_library(workspace, session):
    set_user_id("kcg")
    fid, _ = _finding(session)
    assert not any(workspace.records_dir.rglob("*.json"))  # nothing until accepted
    session.accept_finding(fid)
    path = workspace.records_dir / session.id / f"{fid}.json"
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["kind"] == "finding" and doc["accepted"] is True
    assert doc["author"] == "kcg"
    assert doc["record"]["payload"]["extra"]["root_cause"] == "join fan-out"


def test_rejected_findings_do_not_publish(workspace, session):
    fid, _ = _finding(session)
    session.reject_finding(fid, "not convinced")
    assert not any(workspace.records_dir.rglob("*.json"))


def test_verification_publishes_proposal(workspace, session):
    fid, before = _finding(session)
    p = proposals.record_proposal(session, "ddl_snippet", "fix join", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    after = run_statement(
        session, "SELECT * FROM DB.S.T1 WHERE dup > 1", executor=FakeExecutor(rows=[])
    )["qid"]
    proposals.verify(session, p["pid"], before, after, "pass", "anomaly gone")
    path = workspace.records_dir / session.id / f"{p['pid']}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["kind"] == "proposal" and doc["verdict"] == "pass"
    assert doc["record"]["verification"]["comparison"]["after_empty"] is True


def test_supersession_republishes_old_finding(workspace, session):
    f1, _ = _finding(session, title="First read")
    session.accept_finding(f1)
    qid = run_statement(session, "SELECT id FROM DB.S.T1", executor=FakeExecutor())["qid"]
    f2 = engine.record_finding(
        session,
        {
            "title": "Corrected read",
            "severity": "high",
            "confidence": "high",
            "summary": "The first finding misread the grain.",
            "evidence": [qid],
            "supersedes": f1,
            "extra": {
                "resolution": "root_caused",
                "root_cause": "grain misread",
                "blast_radius": "same rows",
                "alternatives_tested": "one ruled out",
            },
        },
    )["fid"]
    session.accept_finding(f2)
    old = json.loads(
        (workspace.records_dir / session.id / f"{f1}.json").read_text(encoding="utf-8")
    )
    assert old["superseded_by"] == f2  # the library copy no longer reads as current


def test_teammate_workspace_sees_published_records(workspace, session, tmp_path):
    """The compounding loop: A accepts in their workspace; B searches from theirs."""
    set_user_id("kcg")
    fid, _ = _finding(session)
    session.accept_finding(fid)
    # both workspaces point at the same library ("clone" shared for the test)
    set_library_config(workspace.root, workspace.root, auto_push=False)
    ws_b = Workspace.init(tmp_path / "teammate")
    set_library_config(ws_b.root, workspace.root, auto_push=False)
    ws_b = Workspace(ws_b.root)

    rows = search_records(ws_b, "duplicate")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "library" and row["author"] == "kcg"

    full = get_record(ws_b, row["session_id"], "finding", row["id"])
    assert full["source"] == "library"
    assert full["record"]["payload"]["summary"].startswith("Duplicate rows")


def test_local_session_wins_over_library_copy(workspace, session):
    fid, _ = _finding(session)
    session.accept_finding(fid)
    rows = search_records(workspace, "duplicate")
    assert len(rows) == 1  # deduped: published copy does not double the local row
    assert rows[0]["source"] == "session"


def test_library_search_is_verdict_scoped(workspace, session):
    fid, _ = _finding(session)
    session.accept_finding(fid)
    rows = search_library_records(workspace.records_dir, "fan-out")
    assert len(rows) == 1
    assert "payload" not in rows[0]  # summaries only; records_get has the full record
