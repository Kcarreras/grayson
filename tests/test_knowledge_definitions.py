"""Where a table is defined and what it is made of: structured definitions,
sidecar snapshots, `knowledge sync`, column drift at session start, dbt
definitions from a manifest, and the verified-fix fact."""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core import engine, proposals
from grayson.core.run import run_statement, snapshot_metadata
from grayson.core.session import Session
from grayson.knowledge import (
    KnowledgeStore,
    column_drift,
    columns_from_describe,
    completeness,
    drift_report,
)
from grayson.knowledge.dbt import ingest_dbt_definitions, looks_like_dbt_manifest
from grayson.knowledge.sync import SyncError, sync_table

T = "DB.S.T1"
LIVE = [
    {"name": "ID", "type": "NUMBER", "nullable": False},
    {"name": "VAL", "type": "VARCHAR", "nullable": True},
]


@pytest.fixture
def ks(workspace):
    return KnowledgeStore(workspace.knowledge_dir)


@pytest.fixture
def session(workspace):
    s = Session.create(
        workspace,
        workflow="bug-hunter",
        targets=[T],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    return s


# -- definitions: both spellings, one record ------------------------------------


def test_definition_files_read_and_write_as_structured_entries(ks, workspace):
    ks.set_definition_files(T, ["models/t1.sql"])
    doc = ks.read(T)
    assert doc["definitions"] == [{"path": "models/t1.sql"}]
    assert doc["definition_files"] == ["models/t1.sql"]
    text = (workspace.knowledge_dir / "DB" / "S" / "T1.md").read_text()
    assert "definitions:" in text and "definition_files:" in text  # both names written


def test_legacy_doc_with_only_definition_files_reads_the_same(ks, workspace):
    path = workspace.knowledge_dir / "DB" / "S" / "T1.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntable: DB.S.T1\ndefinition_files:\n- models/old.sql\nfacts: []\n---\n")
    doc = ks.read(T)
    assert doc["definitions"] == [{"path": "models/old.sql"}]
    assert "definition_files" not in completeness(doc)["missing"]


def test_set_profile_definitions_structured_and_validated(ks):
    doc = ks.set_profile(
        T, {"definitions": [{"path": "models/t1.sql", "kind": "dbt_model", "repo": "org/dbt"}]}
    )
    assert doc["definitions"][0]["kind"] == "dbt_model"
    assert doc["definition_files"] == ["models/t1.sql"]
    with pytest.raises(ValueError, match="unknown definition kind"):
        ks.set_profile(T, {"definitions": [{"path": "x.sql", "kind": "spreadsheet"}]})
    with pytest.raises(ValueError, match="unknown profile fields"):
        ks.set_profile(T, {"ddl": "create table"})


def test_setting_paths_keeps_captured_snapshots(ks):
    snap = ks.write_snapshot(T, "ddl", "create or replace TABLE DB.S.T1 (ID NUMBER);")
    ks.upsert_definition(T, {"kind": "ddl", **snap})
    ks.set_definition_files(T, ["models/t1.sql"])
    kinds = {d.get("kind") for d in ks.read(T)["definitions"]}
    assert kinds == {"ddl", None}
    # the same path upserted again replaces, never duplicates
    ks.upsert_definition(T, {"path": "models/t1.sql", "kind": "dbt_model"})
    paths = [d.get("path") for d in ks.read(T)["definitions"]]
    assert paths.count("models/t1.sql") == 1


def test_snapshot_confined_to_doc_directory_and_linted(ks, workspace):
    snap = ks.write_snapshot(T, "ddl", "create table t (id number);", header="captured by test")
    assert snap["snapshot"] == "T1.ddl.sql" and snap["hash"].startswith("sha256:")
    assert ks.read_snapshot(T, "T1.ddl.sql").startswith("-- captured by test")
    assert ks.read_snapshot(T, "../../../etc/passwd") is None
    ks.upsert_definition(T, {"kind": "ddl", **snap})
    assert ks.lint()["warnings"] == []
    (workspace.knowledge_dir / "DB" / "S" / "T1.ddl.sql").unlink()
    assert "missing beside the doc" in ks.lint()["warnings"][0]["problem"]
    with pytest.raises(ValueError, match="unknown snapshot kind"):
        ks.snapshot_path(T, "csv")


# -- structure: the warehouse owns names and types --------------------------------


def test_columns_from_describe_parses_nullability_and_skips_non_columns():
    rows = [
        {"name": "ID", "type": "NUMBER(38,0)", "kind": "COLUMN", "null?": "N"},
        {"name": "VAL", "type": "VARCHAR", "kind": "COLUMN", "null?": "Y"},
        {"name": "ID", "type": "", "kind": "CLUSTERING KEY"},
        {"name": "", "type": "X"},
    ]
    assert columns_from_describe(rows) == [
        {"name": "ID", "type": "NUMBER(38,0)", "nullable": False},
        {"name": "VAL", "type": "VARCHAR", "nullable": True},
    ]
    assert columns_from_describe([{"name": "A", "type": "T"}])[0]["nullable"] is None


def test_sync_columns_keeps_human_fields_and_flags_dropped(ks):
    ks.set_profile(
        T,
        {
            "columns": [
                {"name": "id", "type": "INT", "description": "surrogate key"},
                {"name": "OLD", "description": "retired flag"},
                {"name": "JUNK"},
            ]
        },
    )
    out = ks.sync_columns(T, LIVE, evidence=["q_0001"])
    assert out["added"] == ["VAL"] and out["dropped"] == ["OLD", "JUNK"]
    assert out["type_changed"] == [{"name": "id", "recorded": "INT", "live": "NUMBER"}]
    assert out["removed"] == ["JUNK"]  # nothing human on it, so it simply goes
    doc = ks.read(T)
    by_name = {c["name"]: c for c in doc["columns"]}
    assert by_name["ID"]["description"] == "surrogate key"  # kept; name follows the warehouse
    assert by_name["ID"]["type"] == "NUMBER" and by_name["ID"]["nullable"] is False
    assert by_name["OLD"] == {"name": "OLD", "description": "retired flag", "dropped": True}
    assert "JUNK" not in by_name
    assert doc["structure"]["evidence"] == ["q_0001"] and doc["structure"]["observed_at"]
    # a second sync against the same warehouse is quiet, and OLD is not re-dropped
    again = ks.sync_columns(T, LIVE)
    assert again["added"] == [] and again["dropped"] == [] and again["type_changed"] == []


def test_column_drift_states(ks):
    assert column_drift(ks.read(T), LIVE)["status"] == "unrecorded"
    ks.set_profile(T, {"columns": [{"name": "ID", "type": "NUMBER"}, {"name": "VAL"}]})
    assert column_drift(ks.read(T), LIVE)["status"] == "in_sync"
    ks.set_profile(
        T,
        {
            "columns": [
                {"name": "ID", "type": "number (38, 0)"},
                {"name": "GONE", "type": "DATE"},
                {"name": "OLD", "dropped": True},
            ]
        },
    )
    drift = column_drift(ks.read(T), LIVE)
    assert drift["status"] == "drifted"
    assert drift["added"] == ["VAL"] and drift["dropped"] == ["GONE"]  # OLD already flagged
    assert drift["type_changed"][0]["name"] == "ID"
    assert drift_report(ks, {T: LIVE, "DB.S.NOBODY": LIVE}) == {T: drift}


# -- knowledge sync: system query or audited session statement -----------------------


def test_sync_table_without_session_is_a_system_observation(workspace, ks):
    ex = FakeExecutor()
    out = sync_table(workspace, T, executor=ex)
    assert out["first_observation"] and out["columns_total"] == 2
    doc = ks.read(T)
    assert doc["structure"]["source"] == "describe" and "evidence" not in doc["structure"]
    assert ex.calls[0][0].startswith('DESCRIBE TABLE "DB"."S"."T1"')


def test_sync_table_through_session_is_audited_evidence(workspace, ks, session):
    out = sync_table(workspace, T, session=session, executor=FakeExecutor())
    qid = out["structure"]["evidence"][0]
    assert qid in session.executed_qids()
    row = session.query_row(qid)
    assert row["label"] == f"knowledge sync: describe {T}"
    assert f"session {session.id}" in ks.read(T)["structure"]["source"]


def test_sync_ddl_captures_a_dated_snapshot(workspace, ks, session):
    ddl = "create or replace TABLE DB.S.T1 (\n\tID NUMBER,\n\tVAL VARCHAR\n);"
    ex = FakeExecutor(rows=[{"GET_DDL('TABLE', 'DB.S.T1')": ddl}])
    out = sync_table(workspace, T, session=session, executor=ex, ddl=True)
    assert out["ddl"]["first_capture"] and out["ddl"]["snapshot"] == "T1.ddl.sql"
    entry = next(d for d in ks.read(T)["definitions"] if d.get("kind") == "ddl")
    assert entry["hash"] == out["ddl"]["hash"]
    assert entry["evidence"][0] in session.executed_qids()  # the GET_DDL was audited
    text = ks.read_snapshot(T, "T1.ddl.sql")
    assert "the warehouse is the authority" in text and text.endswith(ddl + "\n")
    # unchanged DDL: same hash, nothing to report; changed DDL: says so
    assert sync_table(workspace, T, executor=ex, ddl=True)["ddl"]["changed_since_last"] is False
    ex2 = FakeExecutor(rows=[{"GET_DDL('TABLE', 'DB.S.T1')": ddl.replace("VARCHAR", "TEXT")}])
    assert sync_table(workspace, T, executor=ex2, ddl=True)["ddl"]["changed_since_last"] is True
    assert ks.lint()["ok"]


def test_sync_reports_failures_instead_of_writing(workspace, ks):
    with pytest.raises(SyncError, match="failed"):
        sync_table(workspace, T, executor=FakeExecutor(status="error", error="no such table"))
    assert ks.read(T)["columns"] == [] and ks.read(T)["structure"] == {}
    with pytest.raises(ValueError):
        sync_table(workspace, "not_qualified", executor=FakeExecutor())


# -- session start: the drift line ---------------------------------------------


def test_snapshot_describes_only_targets_with_recorded_columns(workspace, ks, session):
    ex = FakeExecutor()
    snap = snapshot_metadata(session, executor=ex)
    assert snap["columns"] == {}
    assert not any(sql.startswith("DESCRIBE") for sql, _ in ex.calls)
    ks.set_profile(T, {"columns": [{"name": "ID", "type": "NUMBER"}]})
    snap = snapshot_metadata(session, executor=ex)
    assert [c["name"] for c in snap["columns"][T]] == ["ID", "VAL"]
    assert json.loads(session.get_meta("columns_snapshot"))[T]


def test_describe_failure_never_fails_session_start(workspace, ks, session):
    ks.set_profile(T, {"columns": [{"name": "ID"}]})

    class Flaky(FakeExecutor):
        def execute(self, sql, timeout_seconds=0):
            if sql.startswith("DESCRIBE"):
                raise RuntimeError("warehouse hiccup")
            return super().execute(sql, timeout_seconds)

    snap = snapshot_metadata(session, executor=Flaky())
    assert snap["status"] == "ok" and snap["columns"] == {}


def _mcp_call(server, name: str, args: dict):
    result = asyncio.run(server.call_tool(name, args))
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    content = getattr(result, "content", None) or []
    return json.loads(content[0].text) if content else structured


def test_mcp_session_start_reports_drift_then_sync_settles_it(workspace, ks, fake_snow_env):
    from grayson.mcp.server import build_server

    server = build_server(workspace)
    ks.set_profile(
        T, {"columns": [{"name": "ID", "type": "NUMBER", "description": "pk"}, {"name": "OLD"}]}
    )
    started = _mcp_call(server, "session_start", {"workflow": "table-health", "tables": [T]})
    drift = started["knowledge_drift"][T]
    assert drift["status"] == "drifted"
    assert drift["added"] == ["VAL"] and drift["dropped"] == ["OLD"]
    assert "behind the warehouse" in started["hint"]
    assert "1 dropped (OLD)" in started["hint"]
    sid = started["session"]["id"]
    synced = _mcp_call(server, "knowledge_sync", {"table": T, "session_id": sid})
    assert synced["added"] == ["VAL"] and synced["structure"]["evidence"]
    again = _mcp_call(
        server, "session_start", {"workflow": "table-health", "tables": [T], "new": True}
    )
    assert again["knowledge_drift"][T]["status"] == "in_sync"
    assert "behind the warehouse" not in again.get("hint", "")
    shown = _mcp_call(server, "knowledge_show", {"table": T})
    assert shown["structure"]["evidence"] and shown["definition_snapshots"] == {}


def test_mcp_session_start_unrecorded_columns_are_a_gap_not_drift(workspace, fake_snow_env):
    from grayson.mcp.server import build_server

    started = _mcp_call(
        build_server(workspace), "session_start", {"workflow": "table-health", "tables": [T]}
    )
    assert started["knowledge_drift"] == {} and started["knowledge_gaps"] == [T]


# -- dbt: the transformation behind the table ------------------------------------

MANIFEST = {
    "metadata": {"dbt_version": "1.8.0"},
    "nodes": {
        "model.shop.orders": {
            "resource_type": "model",
            "database": "analytics",
            "schema": "shop",
            "name": "orders",
            "package_name": "shop",
            "original_file_path": "models/marts/orders.sql",
            "raw_code": "select * from {{ ref('stg_orders') }}",
            "compiled_code": "select * from ANALYTICS.STAGING.STG_ORDERS",
            "description": "One row per order, deduplicated.",
            "config": {"materialized": "table"},
            "columns": {
                "ORDER_ID": {"name": "ORDER_ID", "description": "Order key", "data_type": "number"},
                "EMAIL": {"name": "EMAIL", "description": "Customer email at order time"},
                "NOTHING": {"name": "NOTHING", "description": ""},
            },
        },
        "seed.shop.countries": {
            "resource_type": "seed",
            "database": "analytics",
            "schema": "shop",
            "name": "countries",
            "original_file_path": "seeds/countries.csv",
        },
        "test.shop.not_null_orders_email.abc": {
            "resource_type": "test",
            "database": "analytics",
            "schema": "shop",
            "name": "not_null_orders_email",
        },
        "model.shop.partial": {"resource_type": "model", "name": "partial"},
    },
    "sources": {},
}
ORDERS = "ANALYTICS.SHOP.ORDERS"


def test_manifest_detection():
    assert looks_like_dbt_manifest(MANIFEST)
    assert not looks_like_dbt_manifest({"results": []})


def test_ingest_defaults_to_documented_tables(ks):
    out = ingest_dbt_definitions(ks, MANIFEST)
    assert out["updated"] == [] and out["models_in_manifest"] == 2  # tests, partial fqn skipped
    ks.add_fact(ORDERS, "exists", fact_id="e")
    out = ingest_dbt_definitions(ks, MANIFEST, tables=["analytics.shop.missing"])
    assert out["updated"] == [ORDERS] and out["not_in_manifest"] == ["analytics.shop.missing"]
    assert ingest_dbt_definitions(ks, MANIFEST, everything=True)["updated"] == [
        "ANALYTICS.SHOP.COUNTRIES",
        ORDERS,
    ]


def test_ingest_records_pointer_snapshot_and_descriptions(ks):
    ks.set_profile(ORDERS, {"columns": [{"name": "ORDER_ID", "description": "mine already"}]})
    out = ingest_dbt_definitions(ks, MANIFEST, tables=[ORDERS], repo="org/dbt-shop")
    assert out["snapshots"] == 1 and out["descriptions_filled"] == 1
    doc = ks.read(ORDERS)
    entry = doc["definitions"][0]
    assert entry["path"] == "models/marts/orders.sql" and entry["kind"] == "dbt_model"
    assert entry["repo"] == "org/dbt-shop" and entry["materialized"] == "table"
    assert entry["snapshot"] == "ORDERS.dbt.sql" and entry["snapshot_of"] == "compiled"
    assert entry["hash"].startswith("sha256:") and entry["captured_at"]
    assert entry["description"] == "One row per order, deduplicated."
    text = ks.read_snapshot(ORDERS, "ORDERS.dbt.sql")
    assert "the dbt repo is the authority" in text and "ANALYTICS.STAGING.STG_ORDERS" in text
    by_name = {c["name"]: c for c in doc["columns"]}
    assert by_name["ORDER_ID"]["description"] == "mine already"  # grayson's word stands
    assert by_name["EMAIL"] == {
        "name": "EMAIL",
        "description": "Customer email at order time",
        "description_source": "dbt",
    }
    assert "NOTHING" not in by_name
    assert doc["definition_files"] == ["models/marts/orders.sql"]
    assert "definition_files" not in completeness(doc)["missing"]


def test_ingest_reports_changed_definition_and_no_snapshot_mode(ks):
    ingest_dbt_definitions(ks, MANIFEST, tables=[ORDERS], snapshot=False)
    doc = ks.read(ORDERS)
    assert "snapshot" not in doc["definitions"][0] and doc["definitions"][0]["hash"]
    assert ks.read_snapshot(ORDERS, "ORDERS.dbt.sql") is None
    edited = json.loads(json.dumps(MANIFEST))
    edited["nodes"]["model.shop.orders"]["compiled_code"] += " where deleted_at is null"
    out = ingest_dbt_definitions(ks, edited, tables=[ORDERS])
    assert out["changed_since_last"] == [ORDERS]
    assert len(ks.read(ORDERS)["definitions"]) == 1  # refreshed in place


# -- a verified fix compounds into the table's briefing ------------------------------


def _verified(session, verdict="pass"):
    before = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    f = engine.record_finding(
        session,
        {
            "title": "Dup rows",
            "severity": "high",
            "confidence": "high",
            "affected_objects": [T],
            "reproduction": "re-run the cited query",
            "summary": "Duplicate rows appear in the output table.",
            "evidence": [before],
            "extra": {
                "resolution": "root_caused",
                "root_cause": "join fan-out",
                "blast_radius": "1000 rows",
                "alternatives_tested": "two ruled out",
            },
        },
    )
    p = proposals.record_proposal(
        session,
        "file_diff",
        "Dedupe the join",
        {"target_file": "models/t1.sql", "diff": "- x\n+ y", "rationale": "fan-out on promo"},
        f["fid"],
    )
    proposals.decide(session, p["pid"], True)
    proposals.mark_applied(session, p["pid"])
    after = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=[]))["qid"]
    return proposals.verify(session, p["pid"], before, after, verdict, "anomaly gone"), p["pid"]


def test_verified_fix_becomes_a_data_inferred_fact(ks, session):
    out, pid = _verified(session)
    assert out["knowledge_facts"] == [{"table": T, "fact_id": out["knowledge_facts"][0]["fact_id"]}]
    assert "knowledge sync" in out["hint"]
    facts = ks.read(T)["facts"]
    assert len(facts) == 1
    fact = facts[0]
    assert fact["status"] == "data_inferred" and fact["created_by"] == "agent"
    assert fact["fact"].startswith("Verified fix: Dedupe the join — models/t1.sql changed")
    assert pid in fact["fact"] and "fan-out on promo" in fact["fact"]
    assert all(e.startswith(f"session {session.id} q_") for e in fact["evidence"])


def test_failed_verification_adds_no_fact_until_it_passes(ks, session):
    out, pid = _verified(session, verdict="fail")
    assert "knowledge_facts" not in out and ks.read(T)["facts"] == []
    before = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    after = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=[]))["qid"]
    out = proposals.verify(session, pid, before, after, "pass")
    assert len(ks.read(T)["facts"]) == 1 and out["knowledge_facts"][0]["table"] == T


# -- surfaces: knowledge-only server and console -------------------------------------


def test_knowledge_only_server_serves_snapshots(tmp_path):
    from grayson.library import init_library
    from grayson.mcp.knowledge_server import build_knowledge_server

    lib = init_library(tmp_path / "qa-library")
    ks = KnowledgeStore(lib / "knowledge")
    ingest_dbt_definitions(ks, MANIFEST, tables=[ORDERS])
    shown = _mcp_call(build_knowledge_server(lib), "knowledge_show", {"table": ORDERS})
    assert shown["definitions"][0]["kind"] == "dbt_model"
    assert "ANALYTICS.STAGING.STG_ORDERS" in shown["definition_snapshots"]["ORDERS.dbt.sql"]


def test_console_table_page_shows_definitions_and_dropped_columns(workspace, ks):
    from fastapi.testclient import TestClient

    from grayson.ui.server import build_app

    ingest_dbt_definitions(ks, MANIFEST, tables=[ORDERS])
    ks.sync_columns(ORDERS, [{"name": "ORDER_ID", "type": "NUMBER", "nullable": False}])
    client = TestClient(build_app(workspace, token="tok"), base_url="http://127.0.0.1")
    page = client.get(f"/knowledge/{ORDERS}?t=tok").text
    assert "models/marts/orders.sql" in page and "dbt model" in page
    assert "Captured copy" in page and "ANALYTICS.STAGING.STG_ORDERS" in page
    assert "dropped" in page and "not null" in page  # EMAIL dropped, ORDER_ID not null
    assert "Columns observed" in page


# -- CLI: ingest is a user command -----------------------------------------------


def test_cli_knowledge_ingest_and_sync(workspace, tmp_path, fake_snow_env):
    from typer.testing import CliRunner

    from grayson.cli import app

    runner = CliRunner()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(MANIFEST))
    bad = tmp_path / "run_results.json"
    bad.write_text(json.dumps({"metadata": {"dbt_version": "1.8.0"}, "results": []}))
    refused = runner.invoke(app, ["knowledge", "ingest", "--manifest", str(bad)])
    assert refused.exit_code != 0 and "not a dbt manifest" in (refused.stderr or refused.output)
    ok = runner.invoke(
        app,
        ["knowledge", "ingest", "--manifest", str(manifest), "--table", ORDERS, "--no-snapshot"],
    )
    assert ok.exit_code == 0, ok.output
    out = json.loads(ok.output)
    assert out["updated"] == [ORDERS] and out["snapshots"] == 0
    synced = runner.invoke(app, ["knowledge", "sync", T])
    assert synced.exit_code == 0, synced.output
    assert json.loads(synced.output)["columns_total"] == 2
    shown = json.loads(runner.invoke(app, ["knowledge", "show", T]).output)
    assert shown["structure"]["source"] == "describe" and shown["definition_snapshots"] == {}
