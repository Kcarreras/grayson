"""Web console tests via FastAPI TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import FakeExecutor
from seekql.config import GuardSettings
from seekql.core import engine
from seekql.core.run import run_statement
from seekql.core.session import Session
from seekql.interventions import build_request
from seekql.ui.server import build_app

TOKEN = "test-token"


@pytest.fixture
def client(workspace):
    return TestClient(build_app(workspace, token=TOKEN))


@pytest.fixture
def session(workspace):
    s = Session.create(
        workspace,
        workflow="semantic-rule-qa",
        targets=["DB.S.URLS"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
        title="url QA",
    )
    engine.seed_from_workflow(s)
    return s


def test_token_required(client, session):
    assert client.get("/").status_code == 403
    assert client.get(f"/?t={TOKEN}").status_code == 200


def test_dashboard_lists_session(client, session):
    r = client.get(f"/?t={TOKEN}")
    assert r.status_code == 200
    assert "url QA" in r.text
    assert session.id in r.text


def test_session_detail(client, session):
    r = client.get(f"/session/{session.id}?t={TOKEN}")
    assert r.status_code == 200
    assert "Checkpoints" in r.text
    assert "sample_for_review" in r.text  # a required check key


def test_unknown_session_404(client):
    assert client.get(f"/session/nope?t={TOKEN}").status_code == 404


def test_label_intervention_render_and_respond(client, session):
    req = build_request(
        "label_sample",
        {
            "rows": [{"url": "cnn.com"}, {"url": "shop.com"}],
            "labels": ["news", "shop", "other"],
            "instructions": "label each URL",
        },
    )
    iid = session.add_intervention("label_sample", "Label URLs", "feeds accuracy", req)

    page = client.get(f"/session/{session.id}/intervention/{iid}?t={TOKEN}")
    assert page.status_code == 200
    assert "cnn.com" in page.text and "label each URL" in page.text

    resp = client.post(
        f"/session/{session.id}/intervention/{iid}/respond?t={TOKEN}",
        data={"label_0": "news", "note_0": "", "label_1": "shop", "note_1": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    item = session.intervention(iid)
    assert item["status"] == "answered"
    assert item["response"]["labeled_count"] == 2


def test_bad_label_response_shows_error(client, session):
    req = build_request("label_sample", {"rows": [{"u": 1}], "labels": ["a", "b"]})
    iid = session.add_intervention("label_sample", "t", "", req)
    resp = client.post(
        f"/session/{session.id}/intervention/{iid}/respond?t={TOKEN}",
        data={"label_0": "zzz"},
    )
    assert resp.status_code == 400
    assert session.intervention(iid)["status"] == "open"


def test_accept_finding_via_ui(client, session):
    qid = run_statement(session, "SELECT * FROM DB.S.URLS", executor=FakeExecutor())["qid"]
    finding = engine.record_finding(
        session,
        {
            "title": "Miscategorized URLs",
            "severity": "medium",
            "confidence": "medium",
            "summary": "About 8% of URLs fall into the wrong category bucket.",
            "evidence": [qid],
        },
    )
    r = client.post(
        f"/session/{session.id}/finding/{finding['fid']}/accept?t={TOKEN}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert session.finding(finding["fid"])["accepted"] is True


def test_advance_gate_blocks_in_ui(client, session):
    r = client.post(f"/session/{session.id}/advance?t={TOKEN}", data={"to": "review"})
    assert r.status_code == 400
    assert "required checkpoints still open" in r.text
