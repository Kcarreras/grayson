import json

import pytest
from fastapi.testclient import TestClient

from conftest import FakeExecutor
from grayson.checks.regression import decide_check, propose_check
from grayson.config import GuardSettings
from grayson.core import engine
from grayson.core.brief import build_brief, render_brief
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.knowledge import KnowledgeStore
from grayson.knowledge.dbt import ingest_dbt_definitions
from grayson.knowledge.impact import build_plan, ingest_manifest, launch, run_plan_checks
from grayson.ui.server import build_app


@pytest.fixture
def s(workspace):
    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.ORDERS"],
        guard=GuardSettings(),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    return s


def manifest(code="select 1"):
    return {
        "metadata": {
            "dbt_version": "1",
            "project_name": "shop",
            "generated_at": "2026-01-01T00:00:00Z",
        },
        "nodes": {
            "model.orders": {
                "resource_type": "model",
                "database": "DB",
                "schema": "S",
                "name": "orders",
                "raw_code": code,
                "original_file_path": "models/orders.sql",
            },
            "model.daily": {
                "resource_type": "model",
                "database": "DB",
                "schema": "S",
                "name": "daily",
                "depends_on": {"nodes": ["model.orders"]},
            },
            "model.report": {
                "resource_type": "model",
                "database": "DB",
                "schema": "S",
                "name": "report",
                "depends_on": {"nodes": ["model.daily"]},
            },
        },
    }


def test_change_to_downstream_assumptions_checks_launch_and_replay(s):
    store = KnowledgeStore(s.workspace.knowledge_dir)
    ingest_dbt_definitions(store, manifest(), everything=True)
    ingest_dbt_definitions(store, manifest("select 2"), everything=True)
    store.add_fact("DB.S.DAILY", "Revenue totals reconcile", fact_id="revenue")
    check_session = Session.create(
        s.workspace,
        workflow="table-health",
        targets=["DB.S.DAILY"],
        guard=GuardSettings(),
        guard_profile="moderate",
    )
    qid = run_statement(
        check_session, "SELECT COUNT(*) N FROM DB.S.DAILY", executor=FakeExecutor(rows=[{"N": 0}])
    )["qid"]
    check = propose_check(
        check_session,
        qid,
        "daily_clean",
        "Daily stays clean",
        "Revenue QA",
        {"kind": "scalar", "column": "N", "value": 0},
    )["check"]
    decide_check(s.workspace, check["id"], "activate", check["digest"], actor="user")
    plan = build_plan(s)
    assert plan["changed_tables"] == ["DB.S.ORDERS"]
    assert plan["affected_tables"] == ["DB.S.DAILY", "DB.S.ORDERS", "DB.S.REPORT"]
    assert plan["assumptions"][0]["id"] == "revenue"
    assert all(e["freshness"] == "stale" for e in plan["dependencies"])
    assert plan["checks"][0]["id"] == "daily_clean"
    assert sum(c["status"] == "unknown" for c in plan["coverage"]) == 2
    launched = launch(s, plan["digest"])
    child = Session(s.workspace, launched["session_id"])
    assert child.strict_scope and child.targets == plan["affected_tables"]
    assert s.targets == ["DB.S.ORDERS"]
    assert "Investigation plan" in render_brief(build_brief(child))
    assert run_plan_checks(child, executor=FakeExecutor(rows=[{"N": 0}]))["ok"]
    client = TestClient(build_app(s.workspace), base_url="http://localhost")
    page = client.get(f"/session/{s.id}/impact")
    assert page.status_code == 200 and "Launch investigation over 3 tables" in page.text
    assert client.get(f"/session/{child.id}").status_code == 200


def test_relationships_do_not_invent_dependencies_and_cycles_terminate(s):
    store = KnowledgeStore(s.workspace.knowledge_dir)
    store.set_profile("DB.S.ORDERS", {"relationships": [{"to": "DB.S.CUSTOMERS", "on": "ID"}]})
    plan = build_plan(s, ["DB.S.ORDERS"])
    assert plan["affected_tables"] == ["DB.S.ORDERS"]
    assert plan["relationships"] and plan["coverage"][0]["status"] == "unknown"
    m = manifest()
    m["nodes"]["model.orders"]["depends_on"] = {"nodes": ["model.report", "model.missing"]}
    ingest_manifest(store, m)
    assert len(build_plan(s, ["DB.S.ORDERS"])["affected_tables"]) == 3


def test_unknown_future_and_outdated_manifest_preserved(s):
    store = KnowledgeStore(s.workspace.knowledge_dir)
    ingest_manifest(store, manifest())
    path = next((store.dir / "_dependencies").glob("*.json"))
    previous = path.read_bytes()
    m = manifest()
    m["metadata"]["generated_at"] = "2025-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="older"):
        ingest_manifest(store, m)
    assert path.read_bytes() == previous
    path.write_text(json.dumps({"format": 99}), encoding="utf-8")
    plan = build_plan(s, ["DB.S.ORDERS"])
    assert plan["errors"] and plan["coverage"][0]["status"] == "unknown"
    with pytest.raises(ValueError, match="unsupported"):
        ingest_manifest(store, manifest())
    assert json.loads(path.read_text())["format"] == 99


def test_plan_launch_refuses_changed_inputs(s):
    store = KnowledgeStore(s.workspace.knowledge_dir)
    ingest_manifest(store, manifest())
    plan = build_plan(s, ["DB.S.ORDERS"])
    store.add_fact("DB.S.ORDERS", "An assumption added since the plan was shown")
    with pytest.raises(ValueError, match="changed; review"):
        launch(s, plan["digest"])


@pytest.mark.parametrize("change", ["rename", "remove", "remove_chain"])
def test_removed_manifest_relations_retain_prior_downstream_evidence(s, change):
    store = KnowledgeStore(s.workspace.knowledge_dir)
    ingest_manifest(store, manifest())
    updated = manifest()
    updated["metadata"]["generated_at"] = "2026-01-02T00:00:00Z"
    if change == "rename":
        updated["nodes"]["model.orders"]["name"] = "orders_v2"
    else:
        del updated["nodes"]["model.orders"]
        if change == "remove_chain":
            del updated["nodes"]["model.daily"]
    ingest_manifest(store, updated)
    assert build_plan(s)["affected_tables"] == ["DB.S.DAILY", "DB.S.ORDERS", "DB.S.REPORT"]
    # A routine refresh must preserve both the removed relation and its old topology.
    ingest_manifest(store, updated)
    plan = build_plan(s)
    assert plan["changed_tables"] == ["DB.S.ORDERS"]
    assert plan["affected_tables"] == ["DB.S.DAILY", "DB.S.ORDERS", "DB.S.REPORT"]
    prior = next(e for e in plan["dependencies"] if e["upstream"] == "DB.S.ORDERS")
    assert prior["state"] == "prior"
    assert prior["observed_at"] == "2026-01-01T00:00:00Z"
    assert prior["removed_at"] == "2026-01-02T00:00:00Z"
    child = Session(s.workspace, launch(s, plan["digest"])["session_id"])
    assert child.targets == plan["affected_tables"]
    client = TestClient(build_app(s.workspace), base_url="http://localhost")
    page = client.get(f"/session/{s.id}/impact")
    assert "Prior manifest" in page.text


def test_restored_manifest_links_use_current_provenance_without_duplicates(s):
    store = KnowledgeStore(s.workspace.knowledge_dir)
    ingest_manifest(store, manifest())
    renamed = manifest()
    renamed["nodes"]["model.orders"]["name"] = "orders_v2"
    renamed["metadata"]["generated_at"] = "2026-01-02T00:00:00Z"
    ingest_manifest(store, renamed)
    restored = manifest()
    restored["metadata"]["generated_at"] = "2026-01-03T00:00:00Z"
    ingest_manifest(store, restored)
    ingest_manifest(store, restored)
    plan = build_plan(s)
    assert len(plan["dependencies"]) == 2
    assert all(e["state"] == "current" for e in plan["dependencies"])
    assert all(
        e["observed_at"] == restored["metadata"]["generated_at"] for e in plan["dependencies"]
    )


def test_manifest_refresh_preserves_outstanding_changes(s):
    store = KnowledgeStore(s.workspace.knowledge_dir)
    ingest_manifest(store, manifest())
    updated = manifest("select 2")
    updated["metadata"]["generated_at"] = "2026-01-02T00:00:00Z"
    original_changes = ingest_manifest(store, updated)["changes"]
    updated["metadata"]["generated_at"] = "2026-01-03T00:00:00Z"
    assert ingest_manifest(store, updated)["changes"] == original_changes
    # A later change to a different model must not acknowledge the first one.
    updated["nodes"]["model.daily"]["raw_code"] = "select 3"
    updated["metadata"]["generated_at"] = "2026-01-04T00:00:00Z"
    ingest_manifest(store, updated)
    ingest_manifest(store, updated)
    plan = build_plan(s)
    assert plan["changed_tables"] == ["DB.S.ORDERS"]
    assert plan["changes"] == original_changes
    all_changes = build_plan(s, ["DB.S.ORDERS", "DB.S.DAILY"])["changes"]
    assert len(all_changes) == 2
