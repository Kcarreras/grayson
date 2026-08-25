"""Supersession (agent proposes, user accept executes) and rejection."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core import engine
from grayson.core.engine import EnforcementError
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.ui.format import paragraphs
from grayson.ui.server import build_app

TOKEN = "test-token"


@pytest.fixture
def session(workspace):
    s = Session.create(
        workspace,
        workflow="semantic-rule-qa",
        targets=["DB.S.URLS"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    return s


def _finding(session, qid, title="Wrong buckets", supersedes=None):
    payload = {
        "title": title,
        "severity": "medium",
        "confidence": "high",
        "summary": "Some URLs land in the wrong category bucket for this rule.",
        "evidence": [qid],
    }
    if supersedes:
        payload["supersedes"] = supersedes
    return engine.record_finding(session, payload)


@pytest.fixture
def qid(session):
    return run_statement(session, "SELECT * FROM DB.S.URLS", executor=FakeExecutor())["qid"]


def test_supersedes_must_cite_existing_finding(session, qid):
    with pytest.raises(EnforcementError, match="unknown finding"):
        _finding(session, qid, supersedes="f_999")


def test_supersession_executes_only_on_user_accept(session, qid):
    old = _finding(session, qid, title="First take")["fid"]
    session.accept_finding(old)
    new = _finding(session, qid, title="Corrected take", supersedes=old)["fid"]

    # recording the proposal changes nothing on the old finding
    assert session.finding(old)["superseded_by"] is None
    assert session.finding(old)["accepted"] is True

    # the user accept of the successor performs the swap deterministically
    session.accept_finding(new)
    assert session.finding(old)["superseded_by"] == new

    # superseded findings stop counting as accepted for gates
    ready = engine.readiness(session)
    assert old in ready["findings_unaccepted"]
    assert ready["findings_superseded"] == [{"fid": old, "by": new}]


def test_cannot_supersede_an_already_superseded_finding(session, qid):
    old = _finding(session, qid, title="take one")["fid"]
    mid = _finding(session, qid, title="take two", supersedes=old)["fid"]
    session.accept_finding(mid)
    with pytest.raises(EnforcementError, match="already superseded"):
        _finding(session, qid, title="take three", supersedes=old)
    # superseding the head of the chain is the correct move
    _finding(session, qid, title="take three", supersedes=mid)


def test_reject_requires_reason_and_is_agent_visible(session, qid):
    fid = _finding(session, qid)["fid"]
    with pytest.raises(ValueError, match="reason"):
        session.reject_finding(fid, "   ")
    session.reject_finding(fid, "The join direction is wrong; check ORDERS side.")
    f = session.finding(fid)
    assert f["rejected"] and not f["accepted"]
    ready = engine.readiness(session)
    assert ready["findings_rejected"] == [
        {"fid": fid, "reason": "The join direction is wrong; check ORDERS side."}
    ]
    # accepting later clears the rejection (user reversal)
    session.accept_finding(fid)
    assert not session.finding(fid)["rejected"]


def test_reject_via_ui_requires_reason(workspace, session, qid):
    fid = _finding(session, qid)["fid"]
    client = TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")
    r = client.post(f"/session/{session.id}/finding/{fid}/reject?t={TOKEN}", data={"reason": " "})
    assert r.status_code == 400 and "requires a reason" in r.text
    r = client.post(
        f"/session/{session.id}/finding/{fid}/reject?t={TOKEN}",
        data={"reason": "Numbers don't reconcile with q_0001."},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "rejected" in r.text and "reconcile" in r.text


def test_session_page_shows_lineage_and_collapses_old_findings(workspace, session, qid):
    fids = [_finding(session, qid, title=f"take {i}")["fid"] for i in range(5)]
    new = _finding(session, qid, title="corrected", supersedes=fids[0])["fid"]
    session.accept_finding(new)
    client = TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert f'superseded by <a href="#{new}"' in page
    assert f'replaces <a href="#{fids[0]}"' in page
    # findcard tiles render; superseded one carries the class
    assert page.count('class="card findcard') == 6
    assert 'findcard superseded"' in page
    # records: lineage timeline on the detail page
    detail = client.get(f"/records/{session.id}/finding/{new}?t={TOKEN}").text
    assert "Finding history" in detail and "replaced by" in detail


def test_paragraphs_filter_splits_walls_of_text():
    wall = " ".join(f"Sentence number {i} is here." for i in range(7))
    html = str(paragraphs(wall))
    assert html.count("<p>") == 3  # 3 + 3 + 1 sentences
    assert "<script>" not in str(paragraphs("<script>x</script> Bad. Actor. Here. Again."))
