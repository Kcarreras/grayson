"""The scope loop closes: a human's grant widens a live session's scope.

Before this, scope widened only at session start (setup inputs flagged for it)
or through registered views. An agent that asked for a neighbour mid-session
and was told yes had no way to act on the yes except an out-of-scope read that
the audit trail could not tie back to the answer that allowed it.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FakeExecutor
from grayson.cli import app
from grayson.config import GuardSettings
from grayson.core import engine
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.interventions import build_request, validate_response
from grayson.interventions.types import InterventionError
from grayson.mcp.server import build_server
from grayson.ui.server import build_app

runner = CliRunner()
TOKEN = "test-token"


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def invoke_err(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 1, result.output
    return json.loads(result.stderr or result.output)


@pytest.fixture
def at_a_terminal(monkeypatch):
    import grayson.cli as cli

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)


@pytest.fixture
def strict_session(workspace) -> Session:
    s = Session.create(
        workspace,
        workflow="bug-hunter",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
        strict_scope=True,
    )
    engine.seed_from_workflow(s)
    return s


@pytest.fixture
def client(workspace):
    return TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")


# -- the intervention kind -------------------------------------------------


def test_scope_request_normalizes_and_requires_a_reason():
    req = build_request(
        "scope_request",
        {"tables": [" db.s.other ", "DB.S.OTHER", "DB.S.MORE"], "reason": "join-key validation"},
    )
    assert req["tables"] == ["DB.S.OTHER", "DB.S.MORE"]
    assert req["reason"] == "join-key validation"
    with pytest.raises(InterventionError):
        build_request("scope_request", {"tables": ["DB.S.OTHER"]})
    with pytest.raises(InterventionError):
        build_request("scope_request", {"tables": [], "reason": "x"})


def test_scope_request_refuses_free_text_as_a_table_name():
    with pytest.raises(InterventionError, match="not a table name"):
        build_request("scope_request", {"tables": ["the webinar table"], "reason": "x"})


def test_scope_response_is_a_subset_of_the_ask():
    req = build_request("scope_request", {"tables": ["DB.S.A", "DB.S.B"], "reason": "x"})
    out = validate_response("scope_request", req, {"granted": ["db.s.a"], "note": "not B"})
    assert out == {"granted": ["DB.S.A"], "declined": ["DB.S.B"], "note": "not B"}
    out = validate_response("scope_request", req, {"granted": []})
    assert out["granted"] == [] and out["declined"] == ["DB.S.A", "DB.S.B"]
    with pytest.raises(InterventionError, match="not requested"):
        validate_response("scope_request", req, {"granted": ["DB.S.ZZZ"]})
    with pytest.raises(InterventionError):
        validate_response("scope_request", req, {"granted": "DB.S.A"})


# -- the loop, end to end ----------------------------------------------------


def test_granted_scope_request_widens_scope_and_the_read_counts(strict_session):
    s = strict_session
    # rows of a neighbour are walled off...
    out = run_statement(s, "SELECT * FROM DB.S.OTHER", executor=FakeExecutor())
    assert out["status"] == "rejected" and out["rule"] == "out_of_scope"
    assert "scope_request" in out["suggestion"]
    # ...its shape is not, and the guard says where the wall is
    out = run_statement(s, "DESCRIBE TABLE DB.S.OTHER", executor=FakeExecutor())
    assert out["status"] == "executed" and out["tables"] == ["DB.S.OTHER"]
    assert any("its rows are not" in w for w in out["warnings"])

    req = build_request(
        "scope_request", {"tables": ["DB.S.OTHER"], "reason": "validate the customer join"}
    )
    iid = s.add_intervention("scope_request", "Read CUSTOMER sibling?", "", req)
    answer = validate_response("scope_request", req, {"granted": ["DB.S.OTHER"]})
    s.respond_intervention(iid, answer)

    assert "DB.S.OTHER" in s.scope_tables
    assert s.summary()["scope_extra"] == ["DB.S.OTHER"]
    ev = next(e for e in s.events(20) if e["type"] == "scope_changed")
    assert ev["actor"] == "user"
    assert ev["payload"] == {"tables": ["DB.S.OTHER"], "added": ["DB.S.OTHER"], "via": iid}

    out = run_statement(s, "SELECT * FROM DB.S.OTHER", executor=FakeExecutor())
    assert out["status"] == "executed" and out["warnings"] == []
    # and the read is evidence that touched scope, not an off-scope citation
    cp = engine.complete_checkpoint(s, "replicate_anomaly", [out["qid"]], "sibling checked")
    assert cp["status"] == "complete" and not cp.get("evidence_off_scope")


def test_declined_scope_request_changes_nothing(strict_session):
    s = strict_session
    req = build_request("scope_request", {"tables": ["DB.S.OTHER"], "reason": "x"})
    iid = s.add_intervention("scope_request", "t", "", req)
    s.respond_intervention(iid, validate_response("scope_request", req, {"granted": []}))
    assert "DB.S.OTHER" not in s.scope_tables
    assert not [e for e in s.events(20) if e["type"] == "scope_changed"]
    assert s.intervention(iid)["response"]["declined"] == ["DB.S.OTHER"]
    out = run_statement(s, "SELECT * FROM DB.S.OTHER", executor=FakeExecutor())
    assert out["status"] == "rejected"


def test_widen_scope_validates_names_and_dedupes(strict_session):
    s = strict_session
    out = s.widen_scope(["db.s.other", "DB.S.OTHER", "DB.S.T1"], actor="user", via="test")
    assert out["added"] == ["DB.S.OTHER"]  # the target was already in scope
    assert out["scope"] == ["DB.S.OTHER", "DB.S.T1"]
    with pytest.raises(ValueError, match="not a table name"):
        s.widen_scope(["not a name"], actor="user")
    with pytest.raises(ValueError, match="no tables"):
        s.widen_scope([], actor="user")


# -- the user's own surfaces: CLI and console ----------------------------------


def test_cli_session_scope_shows_then_widens(workspace, fake_snow_env, at_a_terminal):
    sid = invoke(
        "session", "start", "--workflow", "bug-hunter", "--table", "DB.S.T1", "--strict-scope"
    )["session"]["id"]
    shown = invoke("session", "scope", sid)
    assert shown["scope"] == ["DB.S.T1"] and shown["strict_scope"] is True
    out = invoke("session", "scope", sid, "DB.S.OTHER,DB.S.MORE")
    assert out["added"] == ["DB.S.OTHER", "DB.S.MORE"]  # input order kept
    s = Session(workspace, sid)
    assert {"DB.S.OTHER", "DB.S.MORE"} <= s.scope_tables
    ev = next(e for e in s.events(20) if e["type"] == "scope_changed")
    assert ev["actor"] == "user" and ev["payload"]["via"] == "session scope"


def test_cli_session_scope_widening_needs_a_terminal(workspace, fake_snow_env):
    sid = invoke("session", "start", "--workflow", "bug-hunter", "--table", "DB.S.T1")["session"][
        "id"
    ]
    err = invoke_err("session", "scope", sid, "DB.S.OTHER")
    assert "interactive terminal" in err["error"]
    assert "DB.S.OTHER" not in Session(workspace, sid).scope_tables
    # reading the scope never needs one
    assert invoke("session", "scope", sid)["scope"] == ["DB.S.T1"]


def test_cli_session_scope_refuses_closed_and_bad_names(workspace, fake_snow_env, at_a_terminal):
    sid = invoke("session", "start", "--workflow", "bug-hunter", "--table", "DB.S.T1")["session"][
        "id"
    ]
    assert "not a table name" in invoke_err("session", "scope", sid, "nope!")["error"]
    Session(workspace, sid).set_meta("stage", "closed")
    assert "closed" in invoke_err("session", "scope", sid, "DB.S.OTHER")["error"]


def test_console_widens_scope(client, strict_session):
    s = strict_session
    page = client.get(f"/session/{s.id}?t={TOKEN}")
    assert page.status_code == 200 and "Also in scope" in page.text
    resp = client.post(
        f"/session/{s.id}/scope?t={TOKEN}",
        data={"tables": "DB.S.OTHER, DB.S.MORE"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert {"DB.S.OTHER", "DB.S.MORE"} <= s.scope_tables
    ev = next(e for e in s.events(20) if e["type"] == "scope_changed")
    assert ev["actor"] == "user" and ev["payload"]["via"] == "console"
    page = client.get(f"/session/{s.id}?t={TOKEN}")
    assert "DB.S.OTHER" in page.text
    bad = client.post(
        f"/session/{s.id}/scope?t={TOKEN}", data={"tables": "nope!"}, follow_redirects=False
    )
    assert bad.status_code == 400 and "not a table name" in bad.text


def test_console_renders_and_answers_a_scope_request(client, strict_session):
    s = strict_session
    req = build_request(
        "scope_request",
        {"tables": ["DB.S.OTHER", "DB.S.MORE"], "reason": "validate the customer join"},
    )
    iid = s.add_intervention("scope_request", "Read siblings?", "", req)
    page = client.get(f"/session/{s.id}/intervention/{iid}?t={TOKEN}")
    assert page.status_code == 200
    assert "DB.S.OTHER" in page.text and "validate the customer join" in page.text
    resp = client.post(
        f"/session/{s.id}/intervention/{iid}/respond?t={TOKEN}",
        data={"granted": ["DB.S.OTHER"], "note": "MORE is PII"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    item = s.intervention(iid)
    assert item["status"] == "answered"
    assert item["response"]["granted"] == ["DB.S.OTHER"]
    assert item["response"]["declined"] == ["DB.S.MORE"]
    assert "DB.S.OTHER" in s.scope_tables and "DB.S.MORE" not in s.scope_tables


# -- the agent's surface: MCP files the ask, never the grant --------------------


def _call(server, name: str, args: dict):
    result = asyncio.run(server.call_tool(name, args))
    content = getattr(result, "content", None) or []
    if content and getattr(content[0], "text", None):
        return json.loads(content[0].text)
    return getattr(result, "structured_content", None)


def test_mcp_files_a_scope_request_but_cannot_grant_one(workspace, fake_snow_env, strict_session):
    server = build_server(workspace)
    names = {getattr(t, "name", None) for t in asyncio.run(server.list_tools())}
    assert not [n for n in names if "scope" in (n or "")]
    item = _call(
        server,
        "intervention_request",
        {
            "session_id": strict_session.id,
            "kind": "scope_request",
            "title": "Read CUSTOMER sibling?",
            "payload": {"tables": ["db.s.other"], "reason": "join-key validation"},
        },
    )
    assert item["kind"] == "scope_request" and item["status"] == "open"
    assert item["request"]["tables"] == ["DB.S.OTHER"]
    assert "DB.S.OTHER" not in strict_session.scope_tables  # asking grants nothing
