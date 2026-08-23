"""Console v2: tabs, knowledge library, records archive, agent-text sectioning."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FakeExecutor
from seekql.cli import app as cli_app
from seekql.config import GuardSettings
from seekql.core import engine, proposals
from seekql.core.run import run_statement
from seekql.core.session import Session
from seekql.knowledge import KnowledgeStore
from seekql.ui.format import split_sections
from seekql.ui.server import build_app

runner = CliRunner()
TOKEN = "tok"


def invoke(*args):
    result = runner.invoke(cli_app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def client(workspace):
    return TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")


@pytest.fixture
def rich_session(workspace):
    """A session with a query, a finding, and a verified-shape proposal."""
    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
        title="health run",
    )
    engine.seed_from_workflow(s)
    qid = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    f = engine.record_finding(
        s,
        {
            "title": "Duplicate rows in output",
            "severity": "high",
            "confidence": "high",
            "summary": "WHY THIS MATTERS: totals are inflated. BLAST RADIUS: 396 rows affected.",
            "evidence": [qid],
        },
    )
    proposals.record_proposal(
        s,
        "ddl_snippet",
        "Add uniqueness guard",
        {
            "ddl": "CREATE OR REPLACE VIEW qa_guard AS SELECT 1",
            "rationale": "WHY THIS FIX: the join key is not unique. RISKS: rebuild required.",
        },
        f["fid"],
    )
    return s


# -- text sectioning ------------------------------------------------------


def test_split_sections_parses_markers():
    text = (
        "The join fans out. WHY THIS FIX: CODE is not unique. "
        "RISKS: full rebuild. ROLLOUT: run step 1 then step 2."
    )
    secs = split_sections(text)
    headings = [x["heading"] for x in secs]
    assert headings == [None, "Why This Fix", "Risks", "Rollout"]
    assert secs[0]["body"] == "The join fans out."
    assert secs[2]["body"] == "full rebuild."


def test_split_sections_plain_text_single_chunk():
    secs = split_sections("just a plain sentence with no markers.")
    assert len(secs) == 1 and secs[0]["heading"] is None


# -- tabs & pages ---------------------------------------------------------


def test_nav_tabs_present(client, rich_session):
    page = client.get(f"/?t={TOKEN}")
    for tab in ("Sessions", "Knowledge", "Records"):
        assert tab in page.text


def test_dashboard_splits_active_and_closed(client, workspace, rich_session):
    rich_session.set_stage("closed")
    page = client.get(f"/?t={TOKEN}")
    assert "Closed sessions (1)" in page.text


def test_session_page_sections_and_language(client, rich_session):
    page = client.get(f"/session/{rich_session.id}?t={TOKEN}")
    assert "Target tables" in page.text  # clear language, not "targets"
    assert "Why This Fix" in page.text  # agent text parsed into sections
    assert "awaiting decision" in page.text
    assert "h-pop" in page.text  # info widgets rendered


def test_knowledge_tab_lists_and_searches(client, workspace):
    store = KnowledgeStore(workspace.knowledge_dir)
    store.set_profile(
        "DB.S.T1",
        {
            "grain": "one row per id",
            "relationships": [{"to": "DB.S.T2", "on": "ID", "cardinality": "one-to-many"}],
        },
    )
    store.add_fact("DB.S.T1", "amounts are gross")
    page = client.get(f"/knowledge?t={TOKEN}")
    assert "DB.S.T1" in page.text and "one row per id" in page.text
    hit = client.get(f"/knowledge?t={TOKEN}&q=gross")
    assert "amounts are gross" in hit.text
    miss = client.get(f"/knowledge?t={TOKEN}&q=zzzznope")
    assert "Nothing matches" in miss.text
    detail = client.get(f"/knowledge/DB.S.T1?t={TOKEN}")
    assert "<svg" in detail.text  # relationship diagram
    assert "T2" in detail.text


def test_records_tab_and_viewer(client, rich_session):
    page = client.get(f"/records?t={TOKEN}")
    assert "Duplicate rows in output" in page.text
    assert "Add uniqueness guard" in page.text
    hit = client.get(f"/records?t={TOKEN}&q=uniqueness&kind=proposal")
    assert "Add uniqueness guard" in hit.text
    assert "Duplicate rows in output" not in hit.text
    viewer = client.get(f"/records/{rich_session.id}/finding/f_001?t={TOKEN}")
    assert "Blast Radius" in viewer.text  # sectioned in the viewer too
    assert client.get(f"/records/{rich_session.id}/finding/f_999?t={TOKEN}").status_code == 404


# -- CLI / MCP records ----------------------------------------------------


def test_records_cli_search_and_show(workspace, rich_session, monkeypatch):
    monkeypatch.chdir(workspace.root)
    hits = invoke("records", "search", "uniqueness")
    assert len(hits) == 1 and hits[0]["kind"] == "proposal"
    assert "payload" not in hits[0]  # summaries only
    full = invoke("records", "show", rich_session.id, "proposal", "p_001")
    assert full["record"]["payload"]["ddl"].startswith("CREATE OR REPLACE VIEW")


def test_records_mcp_tools_registered(workspace, fake_snow_env):
    import asyncio

    from seekql.mcp.server import build_server

    server = build_server(workspace)
    names = {getattr(t, "name", None) for t in asyncio.run(server.list_tools())}
    assert {"records_search", "records_get"} <= names
