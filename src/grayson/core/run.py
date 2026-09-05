"""Orchestration: guard -> execute -> cache -> audit for one agent statement."""

from __future__ import annotations

import json

from grayson.cache.store import staleness
from grayson.core.session import Session
from grayson.executor.snow import Executor, get_executor, metadata_query
from grayson.guard.rules import GuardContext, validate_statement
from grayson.util import utcnow

PREVIEW_ROWS = 10


def guard_context(session: Session, prior_count: int | None = None) -> GuardContext:
    return GuardContext(
        scope_tables=session.scope_tables,
        allowed_globs=session.workspace.config.scopes.allowed,
        strict_scope=session.strict_scope,
        executed_count=session.budget_consumed_count() if prior_count is None else prior_count,
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
    # The pending row for this query already exists; count everything else that
    # consumes budget (executed + other in-flight) so a hard cap holds under
    # concurrent workers rather than being a soft per-worker advisory.
    prior = max(0, session.budget_consumed_count() - 1)
    verdict = validate_statement(sql, session.guard_settings, guard_context(session, prior))

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

    settings = session.guard_settings
    # Any raise between here and the terminal update would otherwise strand the
    # audit row at 'pending'. Catch, record the failure, and re-report.
    try:
        executor = executor or get_executor(session.connection, session.workspace.root)
        result = executor.execute(verdict.executed_sql or sql, settings.timeout_seconds)
    except Exception as e:  # noqa: BLE001 — must not drop the audit row on any failure
        session.update_query(
            qid,
            status="error",
            sql_executed=verdict.executed_sql,
            error=f"executor raised: {type(e).__name__}: {e}",
            warnings=json.dumps(verdict.warnings),
            tables_json=json.dumps(verdict.tables),
        )
        session.log_event(worker or "agent", "query_failed", {"qid": qid, "status": "error"})
        return {"qid": qid, "status": "error", "error": f"{type(e).__name__}: {e}"}

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

    # The first executed statement is analysis by definition — move the stage
    # marker off "setup" here so the console tracks reality by code, not by
    # hoping the agent remembers to advance. Later stages stay agent-declared
    # (and review/fixes stay evidence-gated in advance_stage).
    if session.stage == "setup":
        session.set_stage("analysis", actor="system")

    snapshot = _last_altered_snapshot(session)
    captured = {t: snapshot[t] for t in verdict.tables if t in snapshot}
    # The warehouse statement has already run. If caching the result fails
    # (disk full, etc.), still record that it executed so the audit and budget
    # stay accurate — the cache miss is reported, not silently swallowed.
    try:
        sidecar = session.cache.save(
            qid,
            result.rows,
            sql=verdict.executed_sql or sql,
            source_tables=verdict.tables,
            truncated=bool(verdict.injected_limit and len(result.rows) >= verdict.injected_limit),
            source_last_altered=captured,
            worker=worker,
        )
    except Exception as e:  # noqa: BLE001 — statement ran; audit must reflect that
        session.update_query(
            qid,
            status="executed",
            sql_executed=verdict.executed_sql,
            duration_ms=result.duration_ms,
            row_count=len(result.rows),
            error=f"result cache failed: {type(e).__name__}: {e}",
            warnings=json.dumps(verdict.warnings),
            tables_json=json.dumps(verdict.tables),
        )
        session.log_event(worker or "agent", "cache_failed", {"qid": qid})
        return {
            "qid": qid,
            "status": "executed",
            "row_count": len(result.rows),
            "cache_error": f"{type(e).__name__}: {e}",
            "columns": result.columns,
            "preview": result.rows[:PREVIEW_ROWS],
            "warnings": verdict.warnings,
        }
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
    """Fetch ROW_COUNT/LAST_ALTERED for session targets; store in session meta.

    Also DESCRIBEs each target the knowledge library records columns for, so
    the briefing can say where the recorded descriptor has fallen behind the
    warehouse (`knowledge_drift`). Targets nobody has described yet are not
    described here — that is a knowledge gap, reported as one, and an
    unrequested statement per table is not free."""
    sql = metadata_query(session.targets)
    if sql is None:
        return {"status": "skipped", "reason": "no fully-qualified targets"}
    executor = executor or get_executor(session.connection, session.workspace.root)
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
    columns = _snapshot_columns(session, executor)
    session.set_meta("columns_snapshot", json.dumps(columns))
    session.log_event(
        "system",
        "metadata_snapshot",
        {"tables": list(snapshot), "columns_described": list(columns)},
    )
    return {"status": "ok", "tables": snapshot, "columns": columns}


def _snapshot_columns(session: Session, executor: Executor) -> dict[str, list[dict]]:
    """Live columns for each target with a recorded column list, best-effort:
    advisory data that must never fail a session start."""
    from grayson.knowledge import KnowledgeStore, columns_from_describe
    from grayson.profile.plan import ProfilePlanError, qualify

    store = KnowledgeStore(session.workspace.knowledge_dir)
    out: dict[str, list[dict]] = {}
    for target in session.targets:
        try:
            doc = store.read(target)
            quoted = qualify(doc["table"])
        except (ValueError, ProfilePlanError):  # not an FQN, or an unreadable doc
            continue
        if not any(not c.get("dropped") for c in doc["columns"]):
            continue
        try:
            result = executor.execute(f"DESCRIBE TABLE {quoted}", timeout_seconds=60)
        except Exception:  # noqa: BLE001 — advisory; degrade to 'drift unknown'
            continue
        if not result.ok:
            continue
        live = columns_from_describe(result.rows)
        if live:
            out[doc["table"]] = live
    return out


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


def fetch_last_altered(
    connection: str,
    workspace_root,
    tables: list[str],
    executor: Executor | None = None,
) -> dict[str, str]:
    """Current LAST_ALTERED per fully-qualified table, best-effort.

    One cheap metadata query; any failure (no auth, no executor, bad tables)
    returns {} so callers degrade to 'freshness unknown' instead of erroring —
    staleness detection must never block the operation it decorates.
    """
    sql = metadata_query(tables)
    if sql is None:
        return {}
    try:
        executor = executor or get_executor(connection, workspace_root)
        result = executor.execute(sql, timeout_seconds=60)
    except Exception:  # noqa: BLE001 — advisory data; degrade, don't raise
        return {}
    if not result.ok:
        return {}
    current: dict[str, str] = {}
    for row in result.rows:
        upper = {k.upper(): v for k, v in row.items()}
        fq = ".".join(
            str(upper.get(k, "")) for k in ("TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME")
        ).upper()
        if upper.get("LAST_ALTERED"):
            current[fq] = str(upper["LAST_ALTERED"])
    return current


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
        current = fetch_last_altered(
            session.connection, session.workspace.root, source_tables, executor
        )
    out = []
    for m in matches:
        entry = dict(m)
        entry.pop("sql", None)
        entry["freshness"] = staleness(m, current) if check_freshness else "unchecked"
        out.append(entry)
    return out
