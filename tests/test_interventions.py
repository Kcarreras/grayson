from __future__ import annotations

import pytest

from grayson.config import GuardSettings
from grayson.core.session import Session
from grayson.interventions import build_request, validate_response
from grayson.interventions.types import InterventionError


@pytest.fixture
def session(workspace):
    return Session.create(
        workspace,
        workflow="semantic-rule-qa",
        targets=["DB.S.URLS"],
        guard=GuardSettings(),
        guard_profile="moderate",
    )


# -- request building ----------------------------------------------------


def test_label_sample_request():
    req = build_request(
        "label_sample",
        {"rows": [{"url": "a.com"}, {"url": "b.com"}], "labels": ["news", "shop", "other"]},
    )
    assert req["labels"] == ["news", "shop", "other"]
    assert len(req["rows"]) == 2


def test_label_sample_needs_rows_and_labels():
    with pytest.raises(InterventionError):
        build_request("label_sample", {"rows": [], "labels": ["a", "b"]})
    with pytest.raises(InterventionError):
        build_request("label_sample", {"rows": [{"x": 1}], "labels": ["only"]})


def test_unknown_kind():
    with pytest.raises(InterventionError):
        build_request("nope", {})


# -- response validation -------------------------------------------------


def test_label_response_valid():
    req = build_request("label_sample", {"rows": [{"u": 1}, {"u": 2}], "labels": ["a", "b"]})
    out = validate_response(
        "label_sample",
        req,
        {"labels": [{"row_index": 0, "label": "a"}, {"row_index": 1, "label": "b"}]},
    )
    assert out["labeled_count"] == 2


def test_label_response_rejects_bad_label():
    req = build_request("label_sample", {"rows": [{"u": 1}], "labels": ["a", "b"]})
    with pytest.raises(InterventionError):
        validate_response("label_sample", req, {"labels": [{"row_index": 0, "label": "z"}]})


def test_label_response_rejects_out_of_range():
    req = build_request("label_sample", {"rows": [{"u": 1}], "labels": ["a", "b"]})
    with pytest.raises(InterventionError):
        validate_response("label_sample", req, {"labels": [{"row_index": 5, "label": "a"}]})


def test_confirm_semantics_flow():
    req = build_request("confirm_semantics", {"statement": "'other' means uncategorized"})
    out = validate_response("confirm_semantics", req, {"decision": "confirm", "note": "yes"})
    assert out["decision"] == "confirm"
    with pytest.raises(InterventionError):
        validate_response("confirm_semantics", req, {"decision": "maybe"})


def test_choose_flow():
    req = build_request("choose", {"options": ["x", "y", "z"], "question": "which?"})
    assert validate_response("choose", req, {"selected": "y"})["selected"] == "y"
    with pytest.raises(InterventionError):
        validate_response("choose", req, {"selected": "w"})


def test_choose_multi():
    req = build_request("choose", {"options": ["x", "y", "z"], "multi": True})
    assert validate_response("choose", req, {"selected": ["x", "z"]})["selected"] == ["x", "z"]
    with pytest.raises(InterventionError):
        validate_response("choose", req, {"selected": ["x", "w"]})


# -- session storage & lifecycle -----------------------------------------


def test_intervention_lifecycle(session):
    req = build_request("free_response", {"question": "what does ID 7 mean?"})
    iid = session.add_intervention("free_response", "Clarify ID 7", "for taxonomy", req)
    assert session.intervention(iid)["status"] == "open"
    assert len(session.interventions("open")) == 1

    response = validate_response("free_response", req, {"text": "it's a legacy region code"})
    session.respond_intervention(iid, response)
    item = session.intervention(iid)
    assert item["status"] == "answered"
    assert item["response"]["text"] == "it's a legacy region code"
    assert len(session.interventions("open")) == 0


def test_cannot_respond_twice(session):
    req = build_request("free_response", {"question": "q?"})
    iid = session.add_intervention("free_response", "t", "", req)
    session.respond_intervention(iid, {"text": "a"})
    with pytest.raises(ValueError, match="not open"):
        session.respond_intervention(iid, {"text": "b"})


def test_respond_unknown_raises(session):
    with pytest.raises(KeyError):
        session.respond_intervention("i_999", {"text": "x"})


def test_cancel_intervention(session):
    req = build_request("free_response", {"question": "q?"})
    iid = session.add_intervention("free_response", "t", "", req)
    session.cancel_intervention(iid)
    assert session.intervention(iid)["status"] == "cancelled"


def test_await_intervention_blocks_until_answered(session):
    """One wait loop for CLI and MCP: sleeps between polls, returns the moment the
    status leaves `open`, and on timeout says `waiting` rather than guessing."""
    req = build_request("free_response", {"question": "q"})
    iid = session.add_intervention("free_response", "t", "", req)

    assert session.await_intervention(iid, timeout=0)["waiting"] is True

    slept: list[float] = []

    def fake_sleep(secs: float) -> None:
        slept.append(secs)
        if len(slept) == 2:  # the user answers while the agent is waiting
            session.respond_intervention(iid, {"text": "yes"})

    item = session.await_intervention(iid, timeout=60, interval=0.5, sleep=fake_sleep)
    assert item["status"] == "answered" and item["response"] == {"text": "yes"}
    assert len(slept) == 2 and all(s <= 0.5 for s in slept)

    with pytest.raises(KeyError):
        session.await_intervention("i_999", timeout=0)
