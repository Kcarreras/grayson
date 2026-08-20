"""Orchestration: guard -> execute -> cache -> audit for one agent statement."""

from __future__ import annotations

import json

from seekql.cache.store import staleness
from seekql.core.session import Session
from seekql.executor.snow import Executor, get_executor, metadata_query
from seekql.guard.rules import GuardContext, validate_statement
from seekql.util import utcnow

PREVIEW_ROWS = 10


def guard_context(session: Session) -> GuardContext:
    return GuardContext(
        scope_tables=session.scope_tables,
        allowed_globs=session.workspace.config.scopes.allowed,
        strict_scope=session.strict_scope,
        executed_count=session.executed_count(),
    )


def check_statement(session: Session, sql: str) -> dict:
    """Guard-only dry run (agents can pre-validate cheaply)."""
    verdict = validate_statement(sql, session.guard_settings, guard_context(session))
    return verdict.to_dict()


def run_statement(
    session: Session,
    sql: str,
    worker: str | None = None,
    label: str = "",
    executor: Executor | None = None,
) -> dict:
    """Full path: allocate audit row, guard, execute, cache, report."""
    qid = session.allocate_qid(worker, sql, label)
    verdict = validate_statement(sql, session.guard_settings, guard_context(session))

    if not verdict.allowed:
        session.update_query(
            qid,
            status="rejected",
            guard_rule=verdict.rule,
            reason=verdict.reason,
            warnings=json.dumps(verdict.warnings),
        )
        session.log_event(worker or "agent", "query_rejected", {"qid": qid, "rule": verdict.rule})
        return {
            "qid": qid,
            "status": "rejected",
            "rule": verdict.rule,
            "reason": verdict.reason,
            "suggestion": verdict.suggestion,
            "warnings": verdict.warnings,
        }

    executor = executor or get_executor(session.connection)
    settings = session.guard_settings
    result = executor.execute(verdict.executed_sql or sql, settings.timeout_seconds)

    if not result.ok:
        session.update_query(
            qid,
            status=result.status,
            sql_executed=verdict.executed_sql,
            duration_ms=result.duration_ms,
            error=result.error,
            warnings=json.dumps(verdict.warnings),
            tables_json=json.dumps(verdict.tables),
        )
        session.log_event(worker or "agent", "query_failed", {"qid": qid, "status": result.status})
        out = {
            "qid": qid,
            "status": result.status,
            "error": result.error,
            "warnings": verdict.warnings,
        }
        if result.status == "auth_required":
            out["action_needed"] = (
                "Snowflake authentication has expired. Pause and ask the user to re-authenticate "
                "(e.g. `snow connection test`), then retry."
            )
        return out

    snapshot = _last_altered_snapshot(session)
    captured = {t: snapshot[t] for t in verdict.tables if t in snapshot}
    sidecar = session.cache.save(
        qid,
        result.rows,
        sql=verdict.executed_sql or sql,
        source_tables=verdict.tables,
        truncated=bool(verdict.injected_limit and len(result.rows) >= verdict.injected_limit),
        source_last_altered=captured,
        worker=worker,
    )
    session.update_query(
        qid,
        status="executed",
        sql_executed=verdict.executed_sql,
        duration_ms=result.duration_ms,
        row_count=len(result.rows),
        truncated=int(sidecar["truncated"]),
        warnings=json.dumps(verdict.warnings),
        tables_json=json.dumps(verdict.tables),
    )
    session.log_event(
        worker or "agent",
        "query_executed",
        {"qid": qid, "rows": len(result.rows), "duration_ms": result.duration_ms},
    )
    return {
        "qid": qid,
        "status": "executed",
        "row_count": len(result.rows),
        "truncated": sidecar["truncated"],
        "injected_limit": verdict.injected_limit,
        "duration_ms": result.duration_ms,
        "columns": result.columns,
        "preview": result.rows[:PREVIEW_ROWS],
        "artifact": sidecar.get("artifact"),
        "warnings": verdict.warnings,
        "tables": verdict.tables,
    }


def snapshot_metadata(session: Session, executor: Executor | None = None) -> dict:
    """Fetch ROW_COUNT/LAST_ALTERED for session targets; store in session meta."""
    sql = metadata_query(session.targets)
    if sql is None:
        return {"status": "skipped", "reason": "no fully-qualified targets"}
    executor = executor or get_executor(session.connection)
    result = executor.execute(sql, timeout_seconds=60)
    if not result.ok:
        return {"status": result.status, "error": result.error}
    snapshot = {}
    for row in result.rows:
        upper = {k.upper(): v for k, v in row.items()}
        fq = ".".join(
            str(upper.get(k, "")) for k in ("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME")
        ).upper()
        snapshot[fq] = {
            "row_count": upper.get("ROW_COUNT"),
            "last_altered": upper.get("LAST_ALTERED"),
        }
    session.set_meta("metadata_snapshot", json.dumps(snapshot))
    session.set_meta("metadata_snapshot_at", utcnow())
    session.log_event("system", "metadata_snapshot", {"tables": list(snapshot)})
    return {"status": "ok", "tables": snapshot}


def _last_altered_snapshot(session: Session) -> dict[str, str]:
    raw = session.get_meta("metadata_snapshot")
    if not raw:
        return {}
    data = json.loads(raw)
    return {
        fq.upper(): info.get("last_altered")
        for fq, info in data.items()
        if info.get("last_altered")
    }


def cache_find(
    session: Session,
    tables: list[str] | None = None,
    check_freshness: bool = False,
    executor: Executor | None = None,
) -> list[dict]:
    """Find cached artifacts; optionally re-check source freshness now."""
    matches = session.cache.find(tables=tables)
    current: dict[str, str] = {}
    if check_freshness and matches:
        source_tables = sorted({t for m in matches for t in m.get("source_tables", [])})
        sql = metadata_query(source_tables)
        if sql:
            executor = executor or get_executor(session.connection)
            result = executor.execute(sql, timeout_seconds=60)
            if result.ok:
                for row in result.rows:
                    upper = {k.upper(): v for k, v in row.items()}
                    fq = ".".join(
                        str(upper.get(k, ""))
                        for k in ("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME")
                    ).upper()
                    if upper.get("LAST_ALTERED"):
                        current[fq] = str(upper["LAST_ALTERED"])
    out = []
    for m in matches:
        entry = dict(m)
        entry.pop("sql", None)
        entry["freshness"] = staleness(m, current) if check_freshness else "unchecked"
        out.append(entry)
    return out
