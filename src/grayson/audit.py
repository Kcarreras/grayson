"""Audit reconciliation: warehouse query history vs grayson's own audit trail.

grayson's audit only records what went through grayson; Snowflake's history
records everything the connection's user ran. Diffing the two surfaces
bypass — an agent (or anyone) querying the warehouse around the guard.

This is a HUMAN command by design. It reads QUERY_HISTORY, which carries the
full text of past statements (sensitive literals included), so it is exposed
only through the CLI under the user's own connection — never as an MCP tool —
and the guard denies the history functions/views to agents outright
(guard/rules.py DENIED_FUNCTIONS / DENIED_TABLES). Agents may see the
*verdict* (via an ingested check result), never the history rows.
"""

from __future__ import annotations

import re

from grayson.core.session import Session
from grayson.executor.snow import Executor, get_executor
from grayson.workspace import Workspace

#: statements grayson runs outside the per-session audit (or prepends to
#: agent statements) — expected in warehouse history, not evidence of bypass.
_INTERNAL_PATTERNS = [
    re.compile(r"^ALTER\s+SESSION\s+SET\s+STATEMENT_TIMEOUT_IN_SECONDS", re.IGNORECASE),
    # metadata snapshot / freshness probes (core/run.py metadata_query)
    re.compile(
        r"^SELECT\s+TABLE_CATALOG,\s*TABLE_SCHEMA,\s*TABLE_NAME,\s*ROW_COUNT", re.IGNORECASE
    ),
    # this reconciliation's own history read
    re.compile(r"INFORMATION_SCHEMA\.QUERY_HISTORY", re.IGNORECASE),
]


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";").strip())


def _history_sql(hours: int, limit: int) -> str:
    return (
        "SELECT QUERY_ID, QUERY_TEXT, USER_NAME, TO_VARCHAR(START_TIME) AS START_TIME, "
        "EXECUTION_STATUS FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY("
        f"END_TIME_RANGE_START => DATEADD('hour', -{int(hours)}, CURRENT_TIMESTAMP()), "
        f"RESULT_LIMIT => {int(limit)})) ORDER BY START_TIME"
    )


def grayson_executed(workspace: Workspace) -> set[str]:
    """Normalized text of every statement grayson's audit trail executed."""
    out: set[str] = set()
    for sid in workspace.list_session_ids():
        try:
            rows = Session(workspace, sid).executed_statements()
        except (OSError, ValueError):
            continue
        out.update(_normalize(r["sql"]) for r in rows if r.get("sql"))
    return out


def reconcile(
    workspace: Workspace,
    hours: int = 24,
    limit: int = 10000,
    executor: Executor | None = None,
) -> dict:
    """Classify the last `hours` of warehouse history for this connection.

    Every statement is either grayson-run (matches the audit trail),
    grayson-internal (timeout prepends, metadata probes, this query), or
    UNMATCHED — run around grayson by the same user. Unmatched is a review
    list for a human, not an accusation: the warehouse cannot tell the agent
    apart from you at the keyboard.
    """
    if workspace.config.connection == "sandbox":
        return {"error": "the sandbox has no warehouse history — reconcile applies to Snowflake"}
    executor = executor or get_executor(workspace.config.connection, workspace.root)
    result = executor.execute(_history_sql(hours, limit), timeout_seconds=120)
    if not result.ok:
        return {"error": f"history query failed ({result.status}): {result.error}"}
    known = grayson_executed(workspace)
    matched = internal = 0
    unmatched: list[dict] = []
    for row in result.rows:
        text = str(row.get("QUERY_TEXT") or "")
        norm = _normalize(text)
        if not norm:
            continue
        if norm in known:
            matched += 1
        elif any(p.search(norm) for p in _INTERNAL_PATTERNS):
            internal += 1
        else:
            unmatched.append(
                {
                    "query_id": row.get("QUERY_ID"),
                    "start_time": row.get("START_TIME"),
                    "user": row.get("USER_NAME"),
                    "status": row.get("EXECUTION_STATUS"),
                    "text": text[:300],
                }
            )
    return {
        "connection": workspace.config.connection,
        "window_hours": hours,
        "history_statements": len(result.rows),
        "matched_grayson": matched,
        "grayson_internal": internal,
        "unmatched": unmatched,
        "ok": not unmatched,
        "note": (
            "unmatched statements ran on this connection without passing through "
            "grayson — review them; your own ad-hoc queries land here too"
            if unmatched
            else "every historical statement is accounted for by grayson's audit trail"
        ),
    }


def reconcile_check_result(report: dict) -> dict:
    """Shape a reconcile report as an external-check result (verdict only —
    no history text — so agents can see pass/warn without the raw statements)."""
    from grayson.util import utcnow

    unmatched = report.get("unmatched") or []
    return {
        "check_id": "grayson-audit-reconcile",
        "name": "Warehouse history reconciles with grayson audit",
        "status": "pass" if not unmatched else "warn",
        "tables": [],
        "run_at": utcnow(),
        "source": "grayson",
        "details": (
            f"{len(unmatched)} statement(s) in the last {report.get('window_hours')}h ran "
            "on this connection outside grayson — review with `grayson audit reconcile`"
            if unmatched
            else f"all {report.get('history_statements', 0)} statements in the last "
            f"{report.get('window_hours')}h are accounted for"
        ),
        "metrics": {
            "matched": report.get("matched_grayson", 0),
            "internal": report.get("grayson_internal", 0),
            "unmatched": len(unmatched),
        },
    }
