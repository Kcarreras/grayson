"""dbt run_results adapter: detection, mapping, manifest table resolution."""

from __future__ import annotations

import json

from grayson.checks import ChecksStore
from grayson.checks.adapters import dbt_run_results_to_checks, looks_like_dbt_run_results

RUN_RESULTS = {
    "metadata": {"dbt_version": "1.8.0", "generated_at": "2026-08-25T06:00:00Z"},
    "results": [
        {
            "unique_id": "test.shop.not_null_orders_email.abc123",
            "status": "fail",
            "failures": 812,
            "execution_time": 1.23456,
            "message": "Got 812 results, configured to fail if != 0",
        },
        {
            "unique_id": "test.shop.unique_orders_order_id.def456",
            "status": "pass",
            "failures": 0,
            "execution_time": 0.5,
            "message": None,
        },
        {"unique_id": "model.shop.orders", "status": "success"},
    ],
}

MANIFEST = {
    "nodes": {
        "test.shop.not_null_orders_email.abc123": {
            "name": "not_null_orders_email",
            "compiled_code": "select * from ANALYTICS.SHOP.ORDERS where EMAIL is null",
            "depends_on": {"nodes": ["model.shop.orders"]},
        },
        "test.shop.unique_orders_order_id.def456": {
            "name": "unique_orders_order_id",
            "depends_on": {"nodes": ["model.shop.orders"]},
        },
        "model.shop.orders": {
            "resource_type": "model",
            "database": "analytics",
            "schema": "shop",
            "name": "orders",
        },
    },
    "sources": {},
}


def test_detection():
    assert looks_like_dbt_run_results(RUN_RESULTS)
    assert not looks_like_dbt_run_results({"results": [{"check_id": "x"}]})
    assert not looks_like_dbt_run_results([{"check_id": "x"}])


def test_mapping_with_manifest():
    checks = dbt_run_results_to_checks(RUN_RESULTS, MANIFEST, ttl_hours=26)
    assert len(checks) == 2  # the model node is ignored
    fail = next(c for c in checks if c["status"] == "fail")
    assert fail["check_id"] == "shop.not_null_orders_email.abc123"
    assert fail["name"] == "not_null_orders_email"
    assert fail["tables"] == ["ANALYTICS.SHOP.ORDERS"]
    assert fail["metrics"]["failures"] == 812
    assert "EMAIL is null" in fail["sql"]
    assert fail["run_at"] == "2026-08-25T06:00:00Z"
    assert fail["source"] == "dbt"
    assert fail["ttl_hours"] == 26


def test_mapping_without_manifest_has_no_tables():
    checks = dbt_run_results_to_checks(RUN_RESULTS, None)
    assert all(c["tables"] == [] for c in checks)


def test_ingest_autodetects_dbt(tmp_path):
    results_file = tmp_path / "run_results.json"
    results_file.write_text(json.dumps(RUN_RESULTS))
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(MANIFEST))
    store = ChecksStore(tmp_path / "checks")

    out = store.ingest(results_file, manifest_path=manifest_file, ttl_hours=26)
    assert out["ingested"] == 2 and not out["errors"]

    # results are now native checks: latest() sees them with resolved tables
    latest = store.latest(["ANALYTICS.SHOP.ORDERS"])
    assert {c.check_id for c in latest} == {
        "shop.not_null_orders_email.abc123",
        "shop.unique_orders_order_id.def456",
    }

    # idempotent per (check_id, run_at)
    again = store.ingest(results_file, manifest_path=manifest_file)
    assert again["ingested"] == 0 and again["skipped_duplicates"] == 2
