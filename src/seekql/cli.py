"""seekql CLI — the primary agent-facing interface. All output is JSON."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from seekql.cache.local import LocalQueryError, query_artifacts
from seekql.config import GuardSettings
from seekql.core.run import cache_find, check_statement, run_statement, snapshot_metadata
from seekql.core.session import STAGES, Session
from seekql.workspace import Workspace

app = typer.Typer(
    name="seekql", help="Agentic QA infrastructure for SQL tables.", no_args_is_help=True
)
session_app = typer.Typer(help="Session lifecycle.", no_args_is_help=True)
query_app = typer.Typer(help="Guarded query execution.", no_args_is_help=True)
cache_app = typer.Typer(help="Cached results: find, preview, analyze.", no_args_is_help=True)
worker_app = typer.Typer(help="Parallel worker registration.", no_args_is_help=True)
guard_app = typer.Typer(help="Statement validation.", no_args_is_help=True)
app.add_typer(session_app, name="session")
app.add_typer(query_app, name="query")
app.add_typer(cache_app, name="cache")
app.add_typer(worker_app, name="worker")
app.add_typer(guard_app, name="guard")


def emit(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def fail(message: str, code: int = 1) -> None:
    typer.echo(json.dumps({"error": message}, indent=2), err=True)
    raise typer.Exit(code)


def _workspace() -> Workspace:
    try:
        return Workspace.find()
    except FileNotFoundError as e:
        fail(str(e))
        raise  # unreachable


def _session(session_id: str) -> Session:
    try:
        return Session(_workspace(), session_id)
    except (FileNotFoundError, ValueError) as e:
        fail(str(e))
        raise  # unreachable


def _read_sql(sql: str | None, file: Path | None) -> str:
    if sql and file:
        fail("pass --sql or --file, not both")
    if sql:
        return sql
    if file:
        if not file.is_file():
            fail(f"file not found: {file}")
        return file.read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    fail("no SQL given: use --sql, --file, or pipe via stdin")
    raise RuntimeError  # unreachable


# -- top level -----------------------------------------------------------


@app.command()
def init(path: Path = typer.Argument(Path("."), help="Directory to initialize.")) -> None:
    """Initialize a seekql workspace (seekql.toml, library dirs, .seekql/)."""
    try:
        ws = Workspace.init(path)
    except FileExistsError as e:
        fail(str(e))
        return
    emit({"initialized": str(ws.root), "next": "edit seekql.toml, then `seekql doctor`"})


@app.command()
def doctor() -> None:
    """Check the environment: workspace, snow CLI, connection."""
    checks: list[dict] = []
    ws: Workspace | None = None
    try:
        ws = Workspace.find()
        checks.append({"check": "workspace", "ok": True, "detail": str(ws.root)})
    except FileNotFoundError as e:
        checks.append({"check": "workspace", "ok": False, "detail": str(e)})

    snow = shutil.which("snow")
    checks.append(
        {
            "check": "snow_cli",
            "ok": snow is not None,
            "detail": snow or "snow not found on PATH — install Snowflake CLI",
        }
    )
    if snow and ws:
        try:
            proc = subprocess.run(  # noqa: S603
                [snow, "connection", "list", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            names = []
            if proc.returncode == 0:
                try:
                    names = [c.get("connection_name") for c in json.loads(proc.stdout)]
                except (json.JSONDecodeError, AttributeError):
                    names = []
            target = ws.config.connection
            checks.append(
                {
                    "check": "connection",
                    "ok": target in names,
                    "detail": f"'{target}' in {names}" if names else proc.stderr.strip()[:500],
                }
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            checks.append({"check": "connection", "ok": False, "detail": str(e)})
    if ws and ws.config.library_path:
        checks.append(
            {
                "check": "library",
                "ok": ws.config.library_path.is_dir(),
                "detail": str(ws.config.library_path),
            }
        )
    emit({"ok": all(c["ok"] for c in checks), "checks": checks})


# -- session -------------------------------------------------------------


@session_app.command("start")
def session_start(
    workflow: str = typer.Option(..., "--workflow", "-w"),
    tables: list[str] = typer.Option([], "--table", "-t", help="Fully-qualified target tables."),
    title: str = typer.Option("", "--title"),
    guard_profile: str = typer.Option(None, "--guard-profile"),
    auto_limit: int = typer.Option(None, "--auto-limit", min=0),
    timeout_seconds: int = typer.Option(None, "--timeout", min=0),
    budget_warn: int = typer.Option(None, "--budget-warn", min=0),
    budget_cap: int = typer.Option(None, "--budget-cap", min=0),
    workers: int = typer.Option(1, "--workers", min=1, max=16),
    strict_scope: bool = typer.Option(None, "--strict-scope/--no-strict-scope"),
    skip_snapshot: bool = typer.Option(False, "--skip-snapshot", help="Skip metadata snapshot."),
) -> None:
    """Start a QA session. Guard profile is resolved then per-setting overrides apply."""
    ws = _workspace()
    try:
        settings = ws.config.resolve_profile(guard_profile)
    except KeyError as e:
        fail(str(e.args[0]))
        return
    overrides = {
        "auto_limit": auto_limit,
        "timeout_seconds": timeout_seconds,
        "budget_warn": budget_warn,
        "budget_cap": budget_cap,
    }
    settings = GuardSettings(
        **{k: (v if v is not None else getattr(settings, k)) for k, v in overrides.items()}
    )
    session = Session.create(
        ws,
        workflow=workflow,
        targets=tables,
        guard=settings,
        guard_profile=guard_profile or ws.config.default_guard_profile,
        title=title,
        workers=workers,
        strict_scope=strict_scope,
    )
    result = {"session": session.summary()}
    if not skip_snapshot:
        result["metadata_snapshot"] = snapshot_metadata(session)
    emit(result)


@session_app.command("list")
def session_list() -> None:
    ws = _workspace()
    out = []
    for sid in ws.list_session_ids():
        try:
            s = Session(ws, sid)
            out.append(
                {
                    "id": sid,
                    "title": s.get_meta("title", ""),
                    "workflow": s.workflow,
                    "stage": s.stage,
                    "created_at": s.get_meta("created_at"),
                }
            )
        except (OSError, ValueError):
            continue
    emit(out)


@session_app.command("status")
def session_status(session_id: str) -> None:
    emit(_session(session_id).summary())


@session_app.command("advance")
def session_advance(
    session_id: str,
    stage: str = typer.Option(..., "--to", help=f"One of: {', '.join(STAGES)}"),
    actor: str = typer.Option("user", "--actor"),
) -> None:
    s = _session(session_id)
    try:
        s.set_stage(stage, actor)
    except ValueError as e:
        fail(str(e))
        return
    emit({"id": session_id, "stage": s.stage})


@session_app.command("budget")
def session_budget(
    session_id: str, cap: int = typer.Option(..., "--cap", min=0, help="New budget cap (0=off).")
) -> None:
    """Extend/change the session query budget (a user action)."""
    s = _session(session_id)
    settings = s.guard_settings.model_copy(update={"budget_cap": cap})
    s.set_meta("guard", settings.model_dump_json())
    s.log_event("user", "budget_changed", {"budget_cap": cap})
    emit({"id": session_id, "guard": settings.model_dump()})


@session_app.command("events")
def session_events(session_id: str, limit: int = typer.Option(50, "--limit")) -> None:
    emit(_session(session_id).events(limit))


@session_app.command("scrub")
def session_scrub(session_id: str) -> None:
    """Delete cached warehouse data for a session (audit trail is kept)."""
    emit({"id": session_id, "artifacts_deleted": _session(session_id).scrub_data()})


@session_app.command("close")
def session_close(session_id: str) -> None:
    s = _session(session_id)
    s.set_stage("closed")
    emit({"id": session_id, "stage": "closed"})


# -- worker --------------------------------------------------------------


@worker_app.command("join")
def worker_join(session_id: str, label: str = typer.Option("", "--label")) -> None:
    emit({"worker": _session(session_id).worker_join(label)})


@worker_app.command("list")
def worker_list(session_id: str) -> None:
    emit(_session(session_id).workers())


# -- guard / query -------------------------------------------------------


@guard_app.command("check")
def guard_check(
    session_id: str,
    sql: str = typer.Option(None, "--sql", "-q"),
    file: Path = typer.Option(None, "--file", "-f"),
) -> None:
    """Dry-run validation: what would the guard say? Nothing executes."""
    emit(check_statement(_session(session_id), _read_sql(sql, file)))


@query_app.command("run")
def query_run(
    session_id: str,
    sql: str = typer.Option(None, "--sql", "-q"),
    file: Path = typer.Option(None, "--file", "-f"),
    worker: str = typer.Option(None, "--worker"),
    label: str = typer.Option("", "--label", help="Short purpose note for the audit log."),
) -> None:
    """Guard, execute against Snowflake, cache results, return summary + preview."""
    emit(run_statement(_session(session_id), _read_sql(sql, file), worker=worker, label=label))


@query_app.command("log")
def query_log(session_id: str, limit: int = typer.Option(50, "--limit")) -> None:
    emit(_session(session_id).query_log(limit))


# -- cache ---------------------------------------------------------------


@cache_app.command("find")
def cache_find_cmd(
    session_id: str,
    tables: list[str] = typer.Option([], "--table", "-t"),
    check_freshness: bool = typer.Option(False, "--check-freshness"),
) -> None:
    """List cached artifacts matching tables; optionally re-check source freshness."""
    emit(cache_find(_session(session_id), tables or None, check_freshness))


@cache_app.command("show")
def cache_show(session_id: str, qid: str, rows: int = typer.Option(10, "--rows")) -> None:
    s = _session(session_id)
    sidecar = s.cache.get(qid)
    if sidecar is None:
        fail(f"no cached artifact '{qid}'")
        return
    emit({**sidecar, "preview": s.cache.preview(qid, rows)})


@cache_app.command("query")
def cache_query(
    session_id: str,
    sql: str = typer.Option(None, "--sql", "-q"),
    file: Path = typer.Option(None, "--file", "-f"),
    max_rows: int = typer.Option(1000, "--max-rows"),
) -> None:
    """Local read-only SELECT over cached artifacts (table names are qids, e.g. q_0003)."""
    s = _session(session_id)
    try:
        columns, data = query_artifacts(s.dir / "data", _read_sql(sql, file), max_rows)
    except LocalQueryError as e:
        fail(str(e))
        return
    emit(
        {
            "columns": columns,
            "row_count": len(data),
            "rows": [dict(zip(columns, r, strict=True)) for r in data],
        }
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
