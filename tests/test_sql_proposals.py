"""Copy-and-run proposals never execute SQL or require a local source file."""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import FakeExecutor, call_mcp
from grayson.core import criteria, file_fixes, proposals
from grayson.core.brief import build_brief, render_brief
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.mcp.server import build_server
from grayson.ui.server import build_app


@pytest.fixture
def session(workspace):
    return Session.create(
        workspace,
        workflow="bug-hunter",
        targets=["DB.S.T1"],
        guard=workspace.config.resolve_profile("moderate"),
        guard_profile="moderate",
    )


@pytest.fixture
def client(workspace):
    return TestClient(build_app(workspace, token="test"), base_url="http://127.0.0.1")


def snippet(session, sql="ALTER TABLE DB.S.T1 ADD COLUMN label VARCHAR;"):
    return proposals.record_proposal(
        session,
        "ddl_snippet",
        "Add label",
        {
            "ddl": sql,
            "run_target": "DB.S · warehouse SQL editor",
            "rationale": "Apply in the warehouse; no local source file is available.",
        },
        None,
    )


def url(session, p, action):
    return f"/session/{session.id}/proposal/{p['pid']}/{action}?t=test"


def test_sql_review_and_download_preserve_exact_text_without_executing(client, session):
    sql = "-- café <script>alert(1)</script>\r\nALTER TABLE DB.S.T1\r\n ADD label VARCHAR;\r\n"
    p = snippet(session, sql)
    page = client.get(f"/session/{session.id}?t=test").text
    assert "SQL to copy and run" in page and "Copy SQL" in page and "Download .sql" in page
    assert "DB.S · warehouse SQL editor" in page
    assert '<span class="sql-k">ALTER</span>' in page
    assert "&lt;script&gt;" in page and "<script>alert(1)</script>" not in page
    assert "I've applied this" not in page
    download = client.get(
        url(session, p, "sql"), params={"t": "test", "digest": file_fixes.review_digest(p)}
    )
    assert download.status_code == 200
    assert download.content == sql.encode("utf-8")
    assert download.headers["content-disposition"] == f'attachment; filename="{p["pid"]}.sql"'
    assert download.headers["cache-control"] == "no-store"
    assert session.query_log() == []
    assert session.proposal(p["pid"])["status"] == "proposed"
    assert not list(session.workspace.root.glob("*.sql"))


def test_sql_download_requires_auth_and_current_review(client, session):
    p = snippet(session)
    assert client.get(f"/session/{session.id}/proposal/{p['pid']}/sql").status_code == 403
    assert client.get(url(session, p, "sql")).status_code == 409
    assert (
        client.get(url(session, p, "sql"), params={"t": "test", "digest": "stale"}).status_code
        == 409
    )
    assert client.get(f"/session/{session.id}/proposal/p_999/sql?t=test").status_code == 404


def test_external_apply_is_explicit_and_does_not_claim_verification(client, session):
    p = snippet(session)
    digest = file_fixes.review_digest(p)
    endpoint = url(session, p, "applied")
    assert client.post(endpoint, data={"digest": digest}).status_code == 400
    assert client.post(url(session, p, "approve"), follow_redirects=False).status_code == 303
    page = client.get(f"/session/{session.id}?t=test").text
    assert "I've applied this" in page and "Grayson does not execute this SQL" in page
    assert "Apply approved file fix" not in page
    assert client.post(endpoint, data={"digest": "stale"}).status_code == 400
    assert client.post(endpoint, data={"digest": digest}, follow_redirects=False).status_code == 303
    stored = session.proposal(p["pid"])
    assert stored["status"] == "applied" and stored["verification"] is None
    assert session.query_log() == []
    assert client.post(endpoint, data={"digest": digest}).status_code == 400
    assert (
        "Execution was reported outside Grayson" in client.get(f"/session/{session.id}?t=test").text
    )


def test_external_apply_cannot_substitute_for_managed_file_apply(client, session):
    p = file_fixes.draft(session, "model.sql", "select 1;", "New model")
    proposals.decide(session, p["pid"], True, digest=file_fixes.review_digest(p))
    assert (
        client.post(
            url(session, p, "applied"), data={"digest": file_fixes.review_digest(p)}
        ).status_code
        == 400
    )
    assert (
        client.get(
            url(session, p, "sql"), params={"t": "test", "digest": file_fixes.review_digest(p)}
        ).status_code
        == 404
    )
    assert not (session.workspace.root / "model.sql").exists()


@pytest.mark.parametrize("status", ["rejected", "closed"])
def test_external_apply_refuses_rejected_or_closed_proposals(client, session, status):
    p = snippet(session)
    proposals.decide(session, p["pid"], status == "closed")
    if status == "closed":
        session.set_meta("stage", "closed")
    assert (
        client.post(
            url(session, p, "applied"), data={"digest": file_fixes.review_digest(p)}
        ).status_code
        == 400
    )


def test_sql_criteria_page_has_transfer_and_external_confirmation(client, session):
    p = snippet(session)
    qid = run_statement(
        session, "SELECT COUNT(*) AS n FROM DB.S.T1", executor=FakeExecutor(rows=[{"n": 1}])
    )["qid"]
    contract = criteria.set_criteria(
        session,
        p["pid"],
        {
            "format": 1,
            "criteria": [
                {
                    "name": "Preserve rows",
                    "source_qid": qid,
                    "expectation": {"kind": "scalar", "column": "n", "value": 1},
                }
            ],
        },
    )
    endpoint = f"/session/{session.id}/criteria/{p['pid']}?t=test"
    page = client.get(endpoint).text
    assert "Copy SQL" in page and "Download .sql" in page and 'class="sql-k"' in page
    assert (
        client.post(url(session, p, "applied"), data={"digest": contract["digest"]}).status_code
        == 400
    )
    proposals.decide(session, p["pid"], True, digest=contract["digest"])
    assert "I've applied this" in client.get(endpoint).text
    assert (
        client.post(
            url(session, p, "applied"), data={"digest": contract["digest"]}, follow_redirects=False
        ).status_code
        == 303
    )
    assert session.get_meta(f"criteria_applied:{p['pid']}")
    assert session.proposal(p["pid"])["verification"] is None
    assert len(session.query_log()) == 1


def test_delivery_choice_reaches_agents_and_does_not_convert_existing_proposals(client, session):
    p = snippet(session)
    assert session.summary()["fix_delivery"] == "auto"
    response = client.post(
        f"/session/{session.id}/fix-delivery?t=test",
        data={"delivery": "sql_snippet"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("?t=test#fix-delivery")
    status = call_mcp(build_server(session.workspace), "session_status", {"session_id": session.id})
    assert status["fix_delivery"] == "sql_snippet"
    brief = build_brief(session, session.workspace.workflows_dir)
    assert brief["fix_delivery"] == "sql_snippet"
    assert "Fix delivery preference: SQL to copy and run" in render_brief(brief)
    session.set_fix_delivery("local_file")
    assert session.proposal(p["pid"]) == p
    assert len(session.proposals()) == 1


def test_invalid_or_closed_delivery_changes_are_refused(client, session):
    endpoint = f"/session/{session.id}/fix-delivery?t=test"
    assert client.post(endpoint, data={"delivery": "execute_now"}).status_code == 400
    assert session.summary()["fix_delivery"] == "auto"
    with pytest.raises(ValueError, match="user action"):
        session.set_fix_delivery("sql_snippet", actor="agent")
    session.set_meta("stage", "closed")
    assert client.post(endpoint, data={"delivery": "sql_snippet"}).status_code == 400


def test_changed_sql_cannot_be_confirmed_from_a_stale_page(client, session):
    p = snippet(session)
    digest = file_fixes.review_digest(p)
    proposals.decide(session, p["pid"], True)
    p["payload"]["ddl"] = "ALTER TABLE DB.S.T1 ADD COLUMN other VARCHAR;"
    con = session._con()
    try:
        con.execute(
            "UPDATE proposals SET payload=? WHERE pid=?", (json.dumps(p["payload"]), p["pid"])
        )
        con.commit()
    finally:
        con.close()
    assert client.post(url(session, p, "applied"), data={"digest": digest}).status_code == 400
    assert (
        client.get(url(session, p, "sql"), params={"t": "test", "digest": digest}).status_code
        == 409
    )
