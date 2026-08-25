"""Adapters that convert familiar validation-tool artifacts into check results.

The native contract (see store.CHECKS_README) stays the one source of truth;
an adapter's whole job is to map another tool's result file onto it. dbt is
built in because `dbt test` + `run_results.json` is the most common shape a
data team already has. Anything else — Great Expectations, Soda, a bespoke
QA job — follows the same ~30-line pattern; docs/CHECKS.md walks through it.
"""

from __future__ import annotations

import re
from typing import Any

#: dbt statuses map 1:1 onto the native vocabulary except "warn"
_DBT_STATUS = {
    "pass": "pass",
    "fail": "fail",
    "warn": "warn",
    "error": "error",
    "skipped": "skipped",
}

_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def looks_like_dbt_run_results(data: object) -> bool:
    """A dbt run_results.json: metadata.dbt_version plus a results list."""
    return (
        isinstance(data, dict)
        and isinstance(data.get("metadata"), dict)
        and "dbt_version" in data["metadata"]
        and isinstance(data.get("results"), list)
    )


def _node_fqn(node: dict) -> str:
    parts = [node.get("database"), node.get("schema"), node.get("alias") or node.get("name")]
    return ".".join(str(p) for p in parts if p).upper()


def _tables_for_test(unique_id: str, manifest: dict | None) -> list[str]:
    """Resolve the models/sources a dbt test depends on to fully-qualified tables."""
    if not manifest:
        return []
    node = (manifest.get("nodes") or {}).get(unique_id) or {}
    deps = (node.get("depends_on") or {}).get("nodes") or []
    tables = []
    for dep_id in deps:
        dep = (manifest.get("nodes") or {}).get(dep_id) or (manifest.get("sources") or {}).get(
            dep_id
        )
        if dep and dep.get("resource_type") in {"model", "source", "seed", "snapshot"}:
            fqn = _node_fqn(dep)
            if fqn:
                tables.append(fqn)
    return sorted(set(tables))


def dbt_run_results_to_checks(
    data: dict, manifest: dict | None = None, ttl_hours: float | None = None
) -> list[dict[str, Any]]:
    """Map a dbt run_results.json onto native check results.

    Only test nodes are taken (models/seeds in the same file are ignored).
    With a manifest.json alongside, each check gets the fully-qualified tables
    the test depends on — that is what lets grayson surface it at session
    start — plus the test's compiled SQL when dbt recorded it.
    """
    run_at = str((data.get("metadata") or {}).get("generated_at") or "")
    checks: list[dict[str, Any]] = []
    for r in data.get("results") or []:
        unique_id = str(r.get("unique_id") or "")
        if not unique_id.startswith("test."):
            continue
        status = _DBT_STATUS.get(str(r.get("status")))
        if status is None:
            continue
        node = ((manifest or {}).get("nodes") or {}).get(unique_id) or {}
        check_id = _ID_SAFE.sub("-", unique_id.removeprefix("test."))[:100]
        metrics: dict[str, Any] = {}
        if r.get("failures") is not None:
            metrics["failures"] = r["failures"]
        if r.get("execution_time") is not None:
            metrics["execution_time_s"] = round(float(r["execution_time"]), 3)
        checks.append(
            {
                "check_id": check_id,
                "name": node.get("name") or check_id,
                "status": status,
                "run_at": run_at,
                "source": "dbt",
                "tables": _tables_for_test(unique_id, manifest),
                "metrics": metrics,
                "details": str(r.get("message") or "")[:500],
                "sql": str(node.get("compiled_code") or "")[:4000],
                "ttl_hours": ttl_hours,
            }
        )
    return checks
