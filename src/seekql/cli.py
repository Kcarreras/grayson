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
from seekql.core import engine
from seekql.core import proposals as proposals_engine
from seekql.core.engine import EnforcementError
from seekql.core.proposals import ProposalError
from seekql.core.run import cache_find, check_statement, run_statement, snapshot_metadata
from seekql.core.session import STAGES, Session
from seekql.history import suggest_guard_profile
from seekql.interventions import build_request, validate_response
from seekql.interventions.types import InterventionError
from seekql.knowledge import KnowledgeStore
from seekql.util import write_json
from seekql.views import ViewEntry, ViewRegistry
from seekql.workflows import WorkflowNotFound, get_workflow, list_workflows
from seekql.workspace import Workspace

app = typer.Typer(
    name="seekql", help="Agentic QA infrastructure for SQL tables.", no_args_is_help=True
)
session_app = typer.Typer(help="Session lifecycle.", no_args_is_help=True)
query_app = typer.Typer(help="Guarded query execution.", no_args_is_help=True)
cache_app = typer.Typer(help="Cached results: find, preview, analyze.", no_args_is_help=True)
worker_app = typer.Typer(help="Parallel worker registration.", no_args_is_help=True)
guard_app = typer.Typer(help="Statement validation.", no_args_is_help=True)
workflow_app = typer.Typer(help="Workflow templates.", no_args_is_help=True)
checkpoint_app = typer.Typer(help="Checkpoints (evidence-gated).", no_args_is_help=True)
finding_app = typer.Typer(help="Findings (schema + evidence validated).", no_args_is_help=True)
intervention_app = typer.Typer(help="Human-in-the-loop tasks.", no_args_is_help=True)
proposal_app = typer.Typer(help="Fix proposals and verification.", no_args_is_help=True)
knowledge_app = typer.Typer(help="Team knowledge library.", no_args_is_help=True)
views_app = typer.Typer(help="QA view library.", no_args_is_help=True)
library_app = typer.Typer(help="Team library repo linking.", no_args_is_help=True)
harness_app = typer.Typer(help="Agent harness integration.", no_args_is_help=True)
mcp_app = typer.Typer(help="MCP server.", no_args_is_help=True)
ui_app = typer.Typer(help="Local web console.", no_args_is_help=True)
sandbox_app = typer.Typer(help="Local demo warehouse (no Snowflake needed).", no_args_is_help=True)
app.add_typer(session_app, name="session")
app.add_typer(query_app, name="query")
app.add_typer(cache_app, name="cache")
app.add_typer(worker_app, name="worker")
app.add_typer(guard_app, name="guard")
app.add_typer(workflow_app, name="workflow")
app.add_typer(checkpoint_app, name="checkpoint")
app.add_typer(finding_app, name="finding")
app.add_typer(intervention_app, name="intervention")
app.add_typer(proposal_app, name="proposal")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(views_app, name="views")
app.add_typer(library_app, name="library")
app.add_typer(harness_app, name="harness")
app.add_typer(mcp_app, name="mcp")
app.add_typer(ui_app, name="ui")
app.add_typer(sandbox_app, name="sandbox")


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

    sandbox = ws is not None and ws.config.connection == "sandbox"
    if sandbox:
        from seekql.sandbox.executor import sandbox_db_path

        db = sandbox_db_path(ws.root)
        checks.append(
            {
                "check": "sandbox_warehouse",
                "ok": db.is_file(),
                "detail": str(db) if db.is_file() else "missing — run `seekql sandbox init`",
            }
        )
    snow = shutil.which("snow")
    if not sandbox:
        checks.append(
            {
                "check": "snow_cli",
                "ok": snow is not None,
                "detail": snow or "snow not found on PATH — install Snowflake CLI",
            }
        )
    if snow and ws and not sandbox:
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
        tpl = get_workflow(workflow, ws.workflows_dir)
    except WorkflowNotFound as e:
        fail(str(e))
        return
    # Precedence for the base profile: explicit flag > last-used on these tables
    # > workflow suggestion. Per-setting overrides then apply on top.
    last_used = None if guard_profile else suggest_guard_profile(ws, tables)
    chosen_profile = guard_profile or last_used or tpl.suggested_guard_profile
    if chosen_profile not in ws.config.guard_profiles:
        chosen_profile = tpl.suggested_guard_profile
    try:
        settings = ws.config.resolve_profile(chosen_profile)
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
        guard_profile=chosen_profile,
        title=title,
        workers=workers,
        strict_scope=strict_scope,
    )
    engine.seed_from_workflow(session, ws.workflows_dir)
    result = {
        "session": session.summary(),
        "guard_profile_source": (
            "flag" if guard_profile else "last_used" if last_used else "workflow_default"
        ),
        "workflow": {
            "name": tpl.name,
            "title": tpl.title,
            "setup_inputs": [i.model_dump() for i in tpl.setup_inputs],
            "required_checks": [c.model_dump() for c in tpl.required_checks],
            "findings_schema": tpl.findings_schema,
        },
    }
    if not skip_snapshot:
        snap = snapshot_metadata(session)
        result["metadata_snapshot"] = snap
        current = {
            fq: info.get("last_altered")
            for fq, info in (snap.get("tables") or {}).items()
            if isinstance(info, dict) and info.get("last_altered")
        }
    else:
        current = {}
    # front-load view coverage and relevant knowledge so analysis isn't interrupted
    result["view_coverage"] = ViewRegistry(ws.views_dir).coverage_check(tables, current)
    knowledge = KnowledgeStore(ws.knowledge_dir)
    result["knowledge"] = {t: knowledge.read(t)["facts"] for t in tables}
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
    force: bool = typer.Option(False, "--force", help="Override evidence gates (audited)."),
) -> None:
    """Advance the stage. Evidence gates block review/fixes unless satisfied or forced."""
    s = _session(session_id)
    try:
        result = engine.advance_stage(s, stage, actor, force, _workspace().workflows_dir)
    except EnforcementError as e:
        fail(str(e))
        return
    emit({"id": session_id, "stage": s.stage, "readiness": result})


@session_app.command("readiness")
def session_readiness(session_id: str) -> None:
    """What still blocks the next gated transition?"""
    emit(engine.readiness(_session(session_id), _workspace().workflows_dir))


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


@session_app.command("report")
def session_report(
    session_id: str,
    out: Path = typer.Option(None, "--out", "-o", help="Also write a markdown report here."),
) -> None:
    """Build a full session report: checkpoints, findings, proposals, query stats."""
    from seekql.report import build_report, render_markdown

    report = build_report(_session(session_id), _workspace().workflows_dir)
    if out is not None:
        out.write_text(render_markdown(report), encoding="utf-8")
        emit({"written": str(out), "report": report})
        return
    emit(report)


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


@query_app.command("rerun")
def query_rerun(
    session_id: str,
    qid: str,
    worker: str = typer.Option(None, "--worker"),
    label: str = typer.Option("", "--label"),
) -> None:
    """Re-run a prior query's SQL as a fresh guarded execution (freshness re-check)."""
    s = _session(session_id)
    row = s.query_row(qid)
    if row is None:
        fail(f"no query '{qid}' in this session")
        return
    emit(run_statement(s, row["sql_raw"], worker=worker, label=label or f"rerun of {qid}"))


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


@cache_app.command("export")
def cache_export(
    session_id: str,
    qid: str,
    out: Path = typer.Option(..., "--out", "-o", help="Destination file (.csv or .json)."),
    fmt: str = typer.Option(None, "--format", help="csv|json (default: from extension)."),
) -> None:
    """Export a cached artifact's full rows to CSV or JSON."""
    s = _session(session_id)
    sidecar = s.cache.get(qid)
    if sidecar is None:
        fail(f"no cached artifact '{qid}'")
        return
    fmt = (fmt or ("json" if out.suffix.lower() == ".json" else "csv")).lower()
    if fmt not in {"csv", "json"}:
        fail("format must be csv or json")
        return
    columns, rows = s.cache.rows(qid)
    if not columns:
        columns = sidecar.get("columns", [])
    if fmt == "csv":
        import csv

        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            writer.writerows(rows)
    else:
        write_json(out, [dict(zip(columns, r, strict=True)) for r in rows])
    emit(
        {
            "written": str(out),
            "qid": qid,
            "format": fmt,
            "row_count": len(rows),
            "truncated": bool(sidecar.get("truncated")),
        }
    )


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


# -- workflow ------------------------------------------------------------


@workflow_app.command("list")
def workflow_list() -> None:
    """List available workflow templates (built-in + workspace overrides)."""
    try:
        ws = Workspace.find()
        overrides = ws.workflows_dir
    except FileNotFoundError:
        overrides = None
    emit(
        [
            {
                "name": t.name,
                "title": t.title,
                "description": t.description.strip(),
                "suggested_guard_profile": t.suggested_guard_profile,
                "required_checks": t.required_check_keys(),
                "findings_schema": t.findings_schema,
            }
            for t in list_workflows(overrides)
        ]
    )


@workflow_app.command("show")
def workflow_show(name: str) -> None:
    """Show a workflow template's full definition (setup inputs, checks, schema)."""
    try:
        ws = Workspace.find()
        overrides = ws.workflows_dir
    except FileNotFoundError:
        overrides = None
    try:
        emit(get_workflow(name, overrides).model_dump())
    except WorkflowNotFound as e:
        fail(str(e))


# -- checkpoint ----------------------------------------------------------


@checkpoint_app.command("list")
def checkpoint_list(session_id: str) -> None:
    emit(_session(session_id).checkpoints())


@checkpoint_app.command("complete")
def checkpoint_complete(
    session_id: str,
    key: str,
    evidence: list[str] = typer.Option([], "--evidence", "-e", help="Executed query ids."),
    note: str = typer.Option("", "--note"),
    actor: str = typer.Option("agent", "--actor"),
) -> None:
    """Close a checkpoint — requires evidence (executed query ids) that exist."""
    s = _session(session_id)
    try:
        cp = engine.complete_checkpoint(s, key, evidence, note, actor, _workspace().workflows_dir)
    except EnforcementError as e:
        fail(str(e))
        return
    emit(cp)


@checkpoint_app.command("reopen")
def checkpoint_reopen(session_id: str, key: str) -> None:
    s = _session(session_id)
    s.reopen_checkpoint(key)
    emit(s.checkpoint(key))


# -- findings ------------------------------------------------------------


@finding_app.command("add")
def finding_add(
    session_id: str,
    file: Path = typer.Option(None, "--file", "-f", help="JSON finding payload."),
    json_str: str = typer.Option(None, "--json", help="Inline JSON finding payload."),
    worker: str = typer.Option(None, "--worker"),
) -> None:
    """Record a finding — validated against the workflow schema and evidence."""
    if file and json_str:
        fail("pass --file or --json, not both")
    if file:
        if not file.is_file():
            fail(f"file not found: {file}")
        raw = file.read_text(encoding="utf-8")
    elif json_str:
        raw = json_str
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        fail("no finding payload: use --file, --json, or stdin")
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"invalid JSON: {e}")
        return
    s = _session(session_id)
    try:
        finding = engine.record_finding(s, payload, worker, _workspace().workflows_dir)
    except EnforcementError as e:
        fail(str(e))
        return
    emit(finding)


@finding_app.command("list")
def finding_list(session_id: str) -> None:
    emit(_session(session_id).findings())


@finding_app.command("show")
def finding_show(session_id: str, fid: str) -> None:
    f = _session(session_id).finding(fid)
    if f is None:
        fail(f"no finding '{fid}'")
        return
    emit(f)


@finding_app.command("accept")
def finding_accept(session_id: str, fid: str) -> None:
    """Accept a finding (a user action in the review stage)."""
    s = _session(session_id)
    try:
        s.accept_finding(fid)
    except KeyError as e:
        fail(str(e.args[0]))
        return
    emit(s.finding(fid))


# -- interventions -------------------------------------------------------


@intervention_app.command("request")
def intervention_request(
    session_id: str,
    kind: str = typer.Option(
        ..., "--kind", "-k", help="label_sample|confirm_semantics|choose|free_response"
    ),
    title: str = typer.Option(..., "--title"),
    prompt: str = typer.Option("", "--prompt", help="What you'll do with the answer."),
    file: Path = typer.Option(None, "--file", "-f", help="JSON request payload."),
    json_str: str = typer.Option(None, "--json", help="Inline JSON request payload."),
    worker: str = typer.Option(None, "--worker"),
) -> None:
    """File a human-input task. Returns the intervention id to await."""
    if file and json_str:
        fail("pass --file or --json, not both")
    if file:
        if not file.is_file():
            fail(f"file not found: {file}")
        raw = file.read_text(encoding="utf-8")
    elif json_str:
        raw = json_str
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        fail("no request payload: use --file, --json, or stdin")
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"invalid JSON: {e}")
        return
    s = _session(session_id)
    try:
        request = build_request(kind, payload)
    except InterventionError as e:
        fail(str(e))
        return
    iid = s.add_intervention(kind, title, prompt, request, worker)
    emit(s.intervention(iid))


@intervention_app.command("list")
def intervention_list(
    session_id: str,
    status: str = typer.Option(None, "--status", help="open|answered|cancelled"),
) -> None:
    emit(_session(session_id).interventions(status))


@intervention_app.command("show")
def intervention_show(session_id: str, iid: str) -> None:
    item = _session(session_id).intervention(iid)
    if item is None:
        fail(f"no intervention '{iid}'")
        return
    emit(item)


@intervention_app.command("await")
def intervention_await(
    session_id: str,
    iid: str,
    timeout: int = typer.Option(0, "--timeout", help="Max seconds to wait (0 = poll once)."),
    interval: float = typer.Option(2.0, "--interval"),
) -> None:
    """Block until the user answers the intervention (or timeout). Agents call this."""
    import time

    s = _session(session_id)
    deadline = time.monotonic() + timeout
    while True:
        item = s.intervention(iid)
        if item is None:
            fail(f"no intervention '{iid}'")
            return
        if item["status"] != "open":
            emit(item)
            return
        if timeout <= 0 or time.monotonic() >= deadline:
            emit({"iid": iid, "status": "open", "waiting": True})
            return
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


@intervention_app.command("respond")
def intervention_respond(
    session_id: str,
    iid: str,
    file: Path = typer.Option(None, "--file", "-f"),
    json_str: str = typer.Option(None, "--json"),
) -> None:
    """Submit a response (normally done via the UI; provided for scripting/tests)."""
    if file:
        raw = file.read_text(encoding="utf-8") if file.is_file() else fail(f"no file: {file}")
    elif json_str:
        raw = json_str
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        fail("no response payload: use --file, --json, or stdin")
        return
    s = _session(session_id)
    item = s.intervention(iid)
    if item is None:
        fail(f"no intervention '{iid}'")
        return
    try:
        response = validate_response(item["kind"], item["request"], json.loads(raw))
        s.respond_intervention(iid, response)
    except (InterventionError, ValueError, KeyError, json.JSONDecodeError) as e:
        fail(str(e.args[0] if e.args else e))
        return
    emit(s.intervention(iid))


# -- proposals -----------------------------------------------------------


@proposal_app.command("add")
def proposal_add(
    session_id: str,
    kind: str = typer.Option(..., "--kind", "-k", help="file_diff|ddl_snippet"),
    title: str = typer.Option(..., "--title"),
    finding: str = typer.Option(None, "--finding", help="Finding id this fixes."),
    file: Path = typer.Option(None, "--file", "-f", help="JSON proposal payload."),
    json_str: str = typer.Option(None, "--json"),
    worker: str = typer.Option(None, "--worker"),
) -> None:
    """Draft a fix proposal (file diff or DDL snippet) linked to a finding."""
    if file and json_str:
        fail("pass --file or --json, not both")
    if file:
        if not file.is_file():
            fail(f"file not found: {file}")
        raw = file.read_text(encoding="utf-8")
    elif json_str:
        raw = json_str
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        fail("no payload: use --file, --json, or stdin")
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"invalid JSON: {e}")
        return
    s = _session(session_id)
    try:
        proposal = proposals_engine.record_proposal(s, kind, title, payload, finding, worker)
    except ProposalError as e:
        fail(str(e))
        return
    emit(proposal)


@proposal_app.command("list")
def proposal_list(session_id: str, status: str = typer.Option(None, "--status")) -> None:
    emit(_session(session_id).proposals(status))


@proposal_app.command("show")
def proposal_show(session_id: str, pid: str) -> None:
    p = _session(session_id).proposal(pid)
    if p is None:
        fail(f"no proposal '{pid}'")
        return
    emit(p)


@proposal_app.command("approve")
def proposal_approve(session_id: str, pid: str) -> None:
    """Approve a proposal (a user action). The harness agent then applies it."""
    try:
        emit(proposals_engine.decide(_session(session_id), pid, approve=True))
    except ProposalError as e:
        fail(str(e))


@proposal_app.command("reject")
def proposal_reject(session_id: str, pid: str) -> None:
    try:
        emit(proposals_engine.decide(_session(session_id), pid, approve=False))
    except ProposalError as e:
        fail(str(e))


@proposal_app.command("applied")
def proposal_applied(session_id: str, pid: str) -> None:
    """Mark an approved proposal as applied (agent records this after editing files)."""
    try:
        emit(proposals_engine.mark_applied(_session(session_id), pid))
    except ProposalError as e:
        fail(str(e))


@proposal_app.command("verify")
def proposal_verify(
    session_id: str,
    pid: str,
    before: str = typer.Option(..., "--before", help="Pre-fix evidence query id."),
    after: str = typer.Option(..., "--after", help="Post-fix (re-run) query id."),
    verdict: str = typer.Option(..., "--verdict", help="pass|fail"),
    note: str = typer.Option("", "--note"),
) -> None:
    """Record before/after verification for a proposal, citing executed queries."""
    try:
        emit(proposals_engine.verify(_session(session_id), pid, before, after, verdict, note))
    except ProposalError as e:
        fail(str(e))


# -- knowledge -----------------------------------------------------------


@knowledge_app.command("show")
def knowledge_show(table: str) -> None:
    """Show the knowledge library entry for a table."""
    ws = _workspace()
    try:
        emit(KnowledgeStore(ws.knowledge_dir).read(table))
    except ValueError as e:
        fail(str(e))


@knowledge_app.command("add")
def knowledge_add(
    table: str,
    fact: str = typer.Option(..., "--fact", help="The fact text."),
    status: str = typer.Option(
        "proposed", "--status", help="proposed|data_inferred|user_confirmed"
    ),
    fact_id: str = typer.Option(None, "--id"),
    by: str = typer.Option("agent", "--by"),
    evidence: list[str] = typer.Option([], "--evidence", "-e"),
) -> None:
    """Add a fact about a table. Agents propose; users confirm."""
    ws = _workspace()
    try:
        emit(
            KnowledgeStore(ws.knowledge_dir).add_fact(
                table,
                fact,
                fact_id=fact_id,
                status=status,
                created_by=by,
                evidence=list(evidence),
            )
        )
    except ValueError as e:
        fail(str(e))


@knowledge_app.command("confirm")
def knowledge_confirm(table: str, fact_id: str, by: str = typer.Option("user", "--by")) -> None:
    """Confirm a proposed/inferred fact (a user action)."""
    ws = _workspace()
    try:
        emit(KnowledgeStore(ws.knowledge_dir).confirm_fact(table, fact_id, by))
    except (ValueError, KeyError) as e:
        fail(str(e.args[0] if e.args else e))


@knowledge_app.command("set-files")
def knowledge_set_files(table: str, files: list[str] = typer.Option(..., "--file", "-f")) -> None:
    """Point future agents at the work-repo files that define this table."""
    ws = _workspace()
    try:
        emit(KnowledgeStore(ws.knowledge_dir).set_definition_files(table, list(files)))
    except ValueError as e:
        fail(str(e))


@knowledge_app.command("search")
def knowledge_search(term: str) -> None:
    emit(KnowledgeStore(_workspace().knowledge_dir).search(term))


# -- views ---------------------------------------------------------------


@views_app.command("list")
def views_list() -> None:
    emit([v.model_dump() for v in ViewRegistry(_workspace().views_dir).list()])


@views_app.command("show")
def views_show(name: str) -> None:
    v = ViewRegistry(_workspace().views_dir).get(name)
    if v is None:
        fail(f"no view '{name}' in the registry")
        return
    emit(v.model_dump())


@views_app.command("check")
def views_check(
    tables: list[str] = typer.Option(..., "--table", "-t", help="Target tables."),
) -> None:
    """Coverage check: which library views to reuse, refresh, or build."""
    emit(ViewRegistry(_workspace().views_dir).coverage_check(list(tables)))


@views_app.command("register")
def views_register(
    name: str,
    purpose: str = typer.Option("", "--purpose"),
    source_tables: list[str] = typer.Option([], "--source", "-s"),
    base_files: list[str] = typer.Option([], "--base-file", "-b"),
    ddl_file: Path = typer.Option(None, "--ddl-file", help="File with the view DDL."),
) -> None:
    """Register a QA view (after the user has created it) so the library compounds."""
    ws = _workspace()
    ddl = None
    if ddl_file is not None:
        if not ddl_file.is_file():
            fail(f"ddl file not found: {ddl_file}")
        ddl = ddl_file.read_text(encoding="utf-8")
    entry = ViewEntry(
        name=name,
        purpose=purpose,
        source_tables=list(source_tables),
        base_files=list(base_files),
    )
    emit(ViewRegistry(ws.views_dir).register(entry, ddl).model_dump())


# -- library -------------------------------------------------------------


@library_app.command("init")
def library_init(
    path: Path = typer.Argument(..., help="Directory for the new library repo."),
) -> None:
    """Scaffold a fresh team library repo (knowledge/, views/, workflows/)."""
    from seekql.library import init_library

    created = init_library(path)
    emit(
        {
            "library": str(created),
            "next": "git init & push this dir, then set [library] path in your seekql.toml",
        }
    )


@library_app.command("status")
def library_status_cmd() -> None:
    """Check whether the linked team library clone is behind its remote."""
    from seekql.library import library_status

    emit(library_status(_workspace()))


@library_app.command("pull")
def library_pull_cmd() -> None:
    from seekql.library import library_pull

    emit(library_pull(_workspace()))


@library_app.command("extract")
def library_extract_cmd(
    dest: Path = typer.Argument(..., help="Destination for the extracted library repo."),
) -> None:
    """Split this workspace's assets out into a new shareable library repo."""
    from seekql.library import extract_library

    emit(extract_library(_workspace(), dest))


# -- harness -------------------------------------------------------------


@harness_app.command("init")
def harness_init(
    harness: str = typer.Argument(..., help="cursor | claude-code | codex"),
    path: Path = typer.Option(Path("."), "--path", help="Repo root to write into."),
    no_mcp: bool = typer.Option(False, "--no-mcp", help="Omit the MCP note."),
) -> None:
    """Generate the skill/instruction file that teaches a harness the seekql protocol."""
    from seekql.harness import generate_harness

    try:
        emit(generate_harness(path.resolve(), harness, with_mcp=not no_mcp))
    except ValueError as e:
        fail(str(e))


# -- sandbox -------------------------------------------------------------

SANDBOX_CONFIG = """\
# seekql sandbox workspace — mock warehouse, no Snowflake needed

[connection]
name = "sandbox"          # routes execution to the local sandbox warehouse

[defaults]
guard_profile = "moderate"

[scopes]
allowed = ["SANDBOX.*"]
strict = false
"""


@sandbox_app.command("init")
def sandbox_init(
    path: Path = typer.Argument(Path("seekql-sandbox"), help="Directory for the demo workspace."),
) -> None:
    """Create a demo workspace with a local mock warehouse and planted QA problems.

    Seeds SANDBOX.SHOP.* tables (customers, orders, promos, payments) containing
    problems matched to the built-in workflows, and writes an answer key for the
    human. Point your agent at the workspace WITHOUT showing it the answer key.
    """
    from seekql.sandbox.executor import sandbox_db_path
    from seekql.sandbox.seed import render_answer_key, seed_sandbox

    try:
        ws = Workspace.init(path)
    except FileExistsError as e:
        fail(str(e))
        return
    (ws.root / "seekql.toml").write_text(SANDBOX_CONFIG, encoding="utf-8")
    truth = seed_sandbox(sandbox_db_path(ws.root))
    key_path = ws.root / "SANDBOX_ANSWER_KEY.md"
    key_path.write_text(render_answer_key(truth), encoding="utf-8")
    emit(
        {
            "workspace": str(ws.root),
            "warehouse": str(sandbox_db_path(ws.root)),
            "tables": [
                "SANDBOX.SHOP.CUSTOMERS",
                "SANDBOX.SHOP.ORDERS",
                "SANDBOX.SHOP.PROMOS",
                "SANDBOX.SHOP.ORDERS_ENRICHED",
                "SANDBOX.SHOP.PAYMENTS",
                "SANDBOX.SHOP.PAYMENTS_V2",
            ],
            "answer_key": str(key_path),
            "next": [
                f"cd {path}",
                "seekql harness init claude-code   # or cursor | codex",
                "ask your agent to run a workflow (see the answer key for targets)",
                "keep SANDBOX_ANSWER_KEY.md away from the agent",
            ],
        }
    )


@sandbox_app.command("reset")
def sandbox_reset() -> None:
    """Re-seed the sandbox warehouse (fresh data, same planted problems)."""
    from seekql.sandbox.executor import sandbox_db_path
    from seekql.sandbox.seed import seed_sandbox

    ws = _workspace()
    if ws.config.connection != "sandbox":
        fail("this workspace is not a sandbox (connection name is not 'sandbox')")
        return
    seed_sandbox(sandbox_db_path(ws.root))
    emit({"reseeded": str(sandbox_db_path(ws.root))})


# -- mcp -----------------------------------------------------------------


@mcp_app.command("serve")
def mcp_serve() -> None:
    """Run the MCP server over stdio (for Cursor/Claude Code/Codex MCP configs)."""
    from seekql.mcp import serve_stdio

    serve_stdio(_workspace())


# -- ui ------------------------------------------------------------------


@ui_app.command("serve")
def ui_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    no_token: bool = typer.Option(False, "--no-token", help="Disable the URL access token."),
) -> None:
    """Launch the local web console (loopback only, token-gated)."""
    from seekql.ui.server import serve

    serve(_workspace(), host=host, port=port, use_token=not no_token)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
