"""Console v2: tabs, knowledge library, records archive, agent-text sectioning."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FakeExecutor
from grayson import __version__
from grayson.cli import app as cli_app
from grayson.config import GuardSettings
from grayson.core import engine, proposals
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.knowledge import KnowledgeStore
from grayson.ui.format import relationship_graph, split_sections
from grayson.ui.server import build_app

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
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "WHY THIS MATTERS: totals are inflated. BLAST RADIUS: 396 rows affected.",
            "evidence": [qid],
            "extra": {
                "finding_kind": "rule_defect",
                "rule_location": "categorize_url()",
                "observed_behaviour": "some URLs land in the wrong bucket",
                "expected_behaviour": "match the documented taxonomy",
            },
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
    assert "relgraph" in detail.text  # relationship canvas
    assert "cytoscape.min.js" in detail.text and "elk.bundled.js" in detail.text
    assert "DB.S.T2" in detail.text  # and in the table fallback below it


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

    from grayson.mcp.server import build_server

    server = build_server(workspace)
    names = {getattr(t, "name", None) for t in asyncio.run(server.list_tools())}
    assert {"records_search", "records_get"} <= names


# -- relationship canvas -------------------------------------------------


def test_relationship_graph_merges_both_sides_and_marks_gaps():
    docs = {
        "DB.S.ORDERS": {
            "relationships": [
                {"to": "DB.S.CUSTOMERS", "on": "CUSTOMER_ID", "cardinality": "many-to-one"},
                {"to": "DB.S.SHIPMENTS", "on": "ORDER_ID"},
            ]
        },
        # Declares the same relationship from the other side, with whitespace
        # and case that must not defeat the merge.
        "DB.S.CUSTOMERS": {
            "relationships": [
                {"to": "DB.S.ORDERS", "on": " customer_id ", "cardinality": "one-to-many"}
            ]
        },
    }
    g = relationship_graph(docs)
    assert len(g["edges"]) == 2  # not 3: the reciprocal pair is one relationship
    by_target = {e["target"]: e for e in g["edges"]}
    assert by_target["DB.S.CUSTOMERS"]["mutual"] is True
    assert by_target["DB.S.SHIPMENTS"]["mutual"] is False  # only one side declared it
    known = {n["id"]: n["known"] for n in g["nodes"]}
    assert known["DB.S.ORDERS"] is True
    assert known["DB.S.SHIPMENTS"] is False  # referenced but never described


def test_relationship_graph_focus_is_a_neighbourhood():
    docs = {
        "DB.S.A": {"relationships": [{"to": "DB.S.B", "on": "K"}]},
        "DB.S.B": {"relationships": [{"to": "DB.S.C", "on": "K2"}]},
        "DB.S.C": {"relationships": [{"to": "DB.S.D", "on": "K3"}]},
    }
    g = relationship_graph(docs, focus="DB.S.B")
    ids = {n["id"] for n in g["nodes"]}
    assert ids == {"DB.S.A", "DB.S.B", "DB.S.C"}  # D is two hops out
    assert next(n for n in g["nodes"] if n["id"] == "DB.S.B")["focus"] is True
    assert relationship_graph(docs, focus="DB.S.NOTHING") is None
    assert relationship_graph({"DB.S.A": {"relationships": []}}) is None


def test_relationship_graph_truncates_least_connected_first():
    docs = {"DB.S.HUB": {"relationships": [{"to": f"DB.S.T{i}", "on": "K"} for i in range(10)]}}
    g = relationship_graph(docs, max_nodes=4)
    assert g["truncated"] == 7  # 11 nodes down to 4
    assert "DB.S.HUB" in {n["id"] for n in g["nodes"]}  # the hub survives
    assert all(e["source"] in {n["id"] for n in g["nodes"]} for e in g["edges"])


def test_graph_assets_are_vendored_and_served(client):
    for asset in (
        "vendor/cytoscape.min.js",
        "vendor/elk.bundled.js",
        "vendor/cytoscape-elk.js",
        "graph.js",
    ):
        r = client.get(f"/static/{asset}")
        assert r.status_code == 200 and len(r.content) > 100
    # Public library files, so no token needed - but no traversal either.
    assert client.get("/static/../server.py").status_code in (404, 400)
    assert client.get("/static/nope.js").status_code == 404


def test_assets_only_cached_hard_when_version_stamped(client):
    # A year-long cache is only safe on the URL the current build asks for;
    # otherwise an upgraded grayson would keep serving the old bundle.
    stamped = client.get(f"/static/graph.js?v={__version__}")
    assert "immutable" in stamped.headers["cache-control"]
    assert client.get("/static/graph.js").headers["cache-control"] == "no-cache"
    assert client.get("/static/graph.js?v=0.0.0").headers["cache-control"] == "no-cache"


def test_hostile_library_content_cannot_inject_script(client, workspace):
    # Relationship fields are written by agents; the canvas payload and the
    # fallback table both render them, and neither may emit live markup.
    store = KnowledgeStore(workspace.knowledge_dir)
    store.set_profile(
        "DB.S.EVIL",
        {"relationships": [{"to": "DB.S.T2", "on": "<img src=x onerror=alert(1)>"}]},
    )
    page = client.get(f"/knowledge/DB.S.EVIL?t={TOKEN}").text
    assert "<img src=x" not in page  # nowhere in the document as live markup
    payload = page.split('id="relgraph-table-model">')[1].split("</script>")[0]
    assert "<" not in payload  # so it cannot break out of the script element
    # ...while still carrying the real value through to the canvas.
    edge = json.loads(payload)["edges"][0]
    assert edge["on"] == "<img src=x onerror=alert(1)>"
    assert "&lt;img src=x" in page  # fallback table, escaped by autoescape


def test_library_map_on_the_knowledge_tab(client, workspace):
    store = KnowledgeStore(workspace.knowledge_dir)
    store.set_profile("DB.S.T1", {"relationships": [{"to": "DB.S.T2", "on": "ID"}]})
    page = client.get(f"/knowledge?t={TOKEN}")
    assert "Schema map" in page.text and "relgraph-library" in page.text


def test_checks_tab_lists_and_flags(client, workspace):
    (workspace.checks_dir / "airflow.json").write_text(
        json.dumps(
            [
                {
                    "check_id": "orders_null_email",
                    "name": "orders: email not NULL",
                    "status": "fail",
                    "tables": ["DB.S.T1"],
                    "run_at": "2026-08-24T06:00:00Z",
                    "source": "airflow",
                    "details": "812 rows with NULL email",
                    "sql": "SELECT COUNT(*) FROM DB.S.T1 WHERE EMAIL IS NULL",
                },
                {
                    "check_id": "orders_rowcount",
                    "status": "pass",
                    "tables": ["DB.S.T1"],
                    "run_at": "2026-08-24T06:00:00Z",
                },
            ]
        ),
        encoding="utf-8",
    )
    page = client.get(f"/checks?t={TOKEN}").text
    assert "Failing now" in page
    assert "orders: email not NULL" in page and "812 rows with NULL email" in page
    assert "orders_rowcount" in page
    # the table's knowledge page shows its checks too
    table_page = client.get(f"/knowledge/DB.S.T1?t={TOKEN}").text
    assert "External checks on this table" in table_page
    assert "orders_null_email" in table_page


def test_checks_tab_empty_state(client):
    page = client.get(f"/checks?t={TOKEN}").text
    assert "No external check results on file yet" in page


def test_list_pages_carry_sort_and_filter_markup(client, workspace, rich_session):
    """Knowledge, Checks and Records share one list treatment: a toolbar wired
    by id to a [data-list] container whose items carry sort keys and tags."""
    store = KnowledgeStore(workspace.knowledge_dir)
    store.set_profile(
        "DB.S.A_VERY_LONG_TABLE_NAME_THAT_SHOULD_WRAP_INSIDE_ITS_TILE",
        {"grain": "one row per id", "open_questions": ["is AMOUNT gross?"]},
    )
    store.set_profile("DB.S.T2", {"grain": "one row per day"})
    page = client.get(f"/knowledge?t={TOKEN}").text
    assert 'data-list-tools="tables"' in page and 'data-list="tables"' in page
    assert 'data-tags="open incomplete"' in page and 'data-s-open="1"' in page
    assert 'data-tags=" incomplete"' in page  # T2: no open questions
    assert "1 with open questions" in page
    assert 'id="schema-map"' not in page  # no relationships, no map to fold

    (workspace.checks_dir / "airflow.json").write_text(
        json.dumps(
            [
                {"check_id": "c_fail", "status": "fail", "tables": ["DB.S.T2"],
                 "run_at": "2026-08-24T06:00:00Z", "ttl_hours": 1},
                {"check_id": "c_pass", "status": "pass", "tables": ["DB.S.T2"],
                 "run_at": "2026-08-24T06:00:00Z"},
            ]
        ),
        encoding="utf-8",
    )
    page = client.get(f"/checks?t={TOKEN}").text
    assert 'data-list-tools="failing"' in page and 'data-list-tools="checks"' in page
    assert 'data-tags="fail overdue"' in page and 'data-tags="pass"' in page
    assert 'data-s-status="0"' in page and 'data-s-status="2"' in page
    assert 'th data-sortkey="status"' in page
    assert 'href="#failing"' in page  # the failing tile jumps to the section

    page = client.get(f"/records?t={TOKEN}").text
    assert 'data-list-tools="records"' in page and 'data-list="records"' in page
    assert 'data-tags="finding' in page and 'data-tags="proposal' in page
    assert 'data-s-kind="finding"' in page
