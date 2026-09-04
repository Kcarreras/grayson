"""Records read as history when they are: resolved and superseded findings
rank below current ones, and a finding may supersede a published one from
another session."""

from __future__ import annotations

import json

import pytest

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core import engine, proposals
from grayson.core.engine import EnforcementError
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.records import annotate_states, search_library_records, search_records


def _session(workspace):
    s = Session.create(
        workspace,
        workflow="bug-hunter",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    return s


@pytest.fixture
def session(workspace):
    return _session(workspace)


def _finding(session, title="Dup rows", supersedes=None):
    qid = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    payload = {
        "title": title,
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
    }
    if supersedes:
        payload["supersedes"] = supersedes
    return engine.record_finding(session, payload)["fid"], qid


def _verified_fix(session, fid, before):
    p = proposals.record_proposal(session, "ddl_snippet", "fix join", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    after = run_statement(
        session, "SELECT * FROM DB.S.T1 WHERE dup > 1", executor=FakeExecutor(rows=[])
    )["qid"]
    proposals.verify(session, p["pid"], before, after, "pass", "anomaly gone")
    return p["pid"]


def test_resolved_findings_rank_below_current(workspace, session):
    fixed, before = _finding(session, title="Fixed one")
    session.accept_finding(fixed)
    pid = _verified_fix(session, fixed, before)
    still_open, _ = _finding(session, title="Open one")
    session.accept_finding(still_open)
    rows = search_records(workspace, kind="finding")
    by_id = {r["id"]: r for r in rows}
    assert by_id[fixed]["state"] == "resolved" and by_id[fixed]["resolved_by"] == pid
    assert by_id[still_open]["state"] == "current"
    assert [r["id"] for r in rows] == [still_open, fixed]  # current first, though older
    proposal = search_records(workspace, kind="proposal")[0]
    assert proposal["state"] == "verified"
    library = {r["id"]: r for r in search_library_records(workspace.records_dir, kind="finding")}
    assert library[fixed]["state"] == "resolved" and library[still_open]["state"] == "current"


def test_annotate_states_is_a_pure_join():
    rows = [
        {"kind": "finding", "session_id": "s", "id": "f_001", "ts": "2"},
        {"kind": "finding", "session_id": "s", "id": "f_002", "ts": "3", "superseded_by": "f_003"},
        {
            "kind": "proposal",
            "session_id": "s",
            "id": "p_001",
            "ts": "4",
            "finding_fid": "f_001",
            "verdict": "pass",
        },
        {"kind": "report", "session_id": "s", "id": "report", "ts": "5"},
    ]
    states = {r["id"]: r["state"] for r in annotate_states(rows)}
    assert states == {
        "f_001": "resolved",
        "f_002": "superseded",
        "p_001": "verified",
        "report": "current",
    }


def test_finding_may_supersede_a_published_record_from_another_session(workspace, session):
    f1, _ = _finding(session, title="First read")
    session.accept_finding(f1)
    later = _session(workspace)
    with pytest.raises(EnforcementError, match="not a published finding"):
        _finding(later, title="Bad ref", supersedes="nope/f_999")
    f2, _ = _finding(later, title="Corrected read", supersedes=f"{session.id}/{f1}")
    # a proposal only, until the user accepts
    published = json.loads(
        (workspace.records_dir / session.id / f"{f1}.json").read_text(encoding="utf-8")
    )
    assert published.get("superseded_by") is None
    later.accept_finding(f2)
    published = json.loads(
        (workspace.records_dir / session.id / f"{f1}.json").read_text(encoding="utf-8")
    )
    assert published["superseded_by"] == f"{later.id}/{f2}"
    # finding ids repeat across sessions (f_001 in each): key rows by session too
    rows = {
        (r["session_id"], r["id"]): r
        for r in search_library_records(workspace.records_dir, kind="finding")
    }
    assert rows[(session.id, f1)]["state"] == "superseded"
    assert rows[(later.id, f2)]["state"] == "current"
    # first wins across sessions too: the chain's head is the one to supersede
    third = _session(workspace)
    with pytest.raises(EnforcementError, match="already superseded"):
        _finding(third, title="Third read", supersedes=f"{session.id}/{f1}")
