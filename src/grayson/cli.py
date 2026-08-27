"""grayson CLI — the primary agent-facing interface. All output is JSON."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from grayson.cache.local import LocalQueryError, query_artifacts
from grayson.config import GuardSettings
from grayson.core import engine
from grayson.core import proposals as proposals_engine
from grayson.core.engine import EnforcementError
from grayson.core.proposals import ProposalError
from grayson.core.run import cache_find, check_statement, run_statement, snapshot_metadata
from grayson.core.session import STAGES, Session, find_recent_duplicate, resolve_session_id
from grayson.history import suggest_guard_profile
from grayson.interventions import build_request, validate_response
from grayson.interventions.types import InterventionError
from grayson.knowledge import KnowledgeStore, completeness
from grayson.util import write_json
from grayson.views import ViewEntry, ViewRegistry, enter_session_scope
from grayson.workflows import WorkflowNotFound, get_workflow, list_workflows
from grayson.workspace import Workspace

app = typer.Typer(
    name="grayson",
    help="Agentic QA infrastructure for SQL tables. "
    "Tip: 'latest' works anywhere a session id is expected.",
    no_args_is_help=True,
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
config_app = typer.Typer(
    help="Workspace settings (a user surface — agents get read-only access via MCP).",
    no_args_is_help=True,
)
knowledge_app = typer.Typer(help="Team knowledge library.", no_args_is_help=True)
checks_app = typer.Typer(
    help="External deterministic checks (Airflow, dbt, ...) dropped into the library.",
    no_args_is_help=True,
)
chart_app = typer.Typer(
    help="Charts built from cached artifacts, rendered live in the console.",
    no_args_is_help=True,
)
views_app = typer.Typer(help="QA view library.", no_args_is_help=True)
library_app = typer.Typer(help="Team library repo linking.", no_args_is_help=True)
user_app = typer.Typer(
    help="Your per-user id, stamped on knowledge writes and library commits.",
    no_args_is_help=True,
)
audit_app = typer.Typer(
    help="Audit-trail tools (human-side; reads warehouse history).", no_args_is_help=True
)
harness_app = typer.Typer(help="Agent harness integration.", no_args_is_help=True)
mcp_app = typer.Typer(help="MCP server.", no_args_is_help=True)
ui_app = typer.Typer(help="Local web console.", no_args_is_help=True)
sandbox_app = typer.Typer(help="Local demo warehouse (no Snowflake needed).", no_args_is_help=True)
records_app = typer.Typer(help="Cross-session archive of findings and fixes.", no_args_is_help=True)
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
app.add_typer(config_app, name="config")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(checks_app, name="checks")
app.add_typer(chart_app, name="chart")
app.add_typer(views_app, name="views")
app.add_typer(library_app, name="library")
app.add_typer(user_app, name="user")
app.add_typer(audit_app, name="audit")
app.add_typer(harness_app, name="harness")
app.add_typer(mcp_app, name="mcp")
app.add_typer(ui_app, name="ui")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(records_app, name="records")


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
        ws = _workspace()
        return Session(ws, resolve_session_id(ws, session_id))
    except (FileNotFoundError, ValueError) as e:
        fail(str(e))
        raise  # unreachable


def _refuse_nested_workspace(path: Path) -> None:
    """Workspaces must not nest — sessions/config would silently split by cwd."""
    try:
        existing = Workspace.find(path.resolve().parent)
    except FileNotFoundError:
        return
    fail(
        f"'{path}' is inside the existing workspace at {existing.root} — workspaces "
        "must not nest (commands would target one or the other depending on cwd). "
        "cd outside it and pick a separate directory."
    )


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
    """Initialize a grayson workspace (grayson.toml, library dirs, .grayson/)."""
    _refuse_nested_workspace(path)
    try:
        ws = Workspace.init(path)
    except FileExistsError as e:
        fail(str(e))
        return
    except OSError as e:
        fail(f"cannot create a workspace at '{path.resolve()}': {e}. cd to a writable directory")
        return
    emit({"initialized": str(ws.root), "next": "edit grayson.toml, then `grayson doctor`"})


@app.command()
def doctor() -> None:
    """Check the environment: workspace, snow CLI, connection."""
    emit(_doctor_report())


@app.command()
def setup() -> None:
    """Interactive onboarding: workspace, user id, connection, team library,
    harness, guard permissions, MCP config — one guided pass over the same steps that exist
    as individual commands (init, doctor, user set, library link, harness init),
    which all remain the scriptable path."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        fail(
            "grayson setup is interactive — run it in a terminal, or use the "
            "individual commands: init, doctor, user set, library link, harness init"
        )
        return
    from grayson.harness import generate_harness
    from grayson.harness.mcp import apply_mcp, mcp_status
    from grayson.harness.permissions import apply_guard, guard_rules_display, guard_status
    from grayson.identity import get_user_id, set_user_id

    done: dict = {}
    say = typer.echo

    # -- workspace -------------------------------------------------------
    try:
        ws = Workspace.find()
        say(f"Workspace: {ws.root}")
    except FileNotFoundError:
        say("No workspace here. (For a no-Snowflake demo, use `grayson sandbox init` instead.)")
        if not typer.confirm(f"Initialize a grayson workspace in {Path.cwd()}?", default=True):
            fail("setup needs a workspace — cd to your data repo and run `grayson setup` again")
            return
        _refuse_nested_workspace(Path.cwd())
        try:
            ws = Workspace.init(Path.cwd())
        except OSError as e:
            fail(f"cannot create a workspace here: {e}")
            return
        say(f"Initialized workspace at {ws.root}")
    done["workspace"] = str(ws.root)

    # -- user id ---------------------------------------------------------
    current_id = get_user_id()
    say("\nYour user id stamps facts, records, and library commits for the team.")
    answer = typer.prompt(
        "User id (letters/digits/-/_; blank to skip)",
        default=current_id or "",
        show_default=bool(current_id),
    ).strip()
    if answer and answer != current_id:
        try:
            done["user_id"] = set_user_id(answer)
        except ValueError as e:
            say(f"  skipped: {e}")
    elif current_id:
        done["user_id"] = current_id

    # -- connection ------------------------------------------------------
    from grayson.config_edit import ConfigError, set_values

    say("\ngrayson runs all warehouse access through the `snow` CLI's named connection.")
    connection = typer.prompt("Snowflake connection name", default=ws.config.connection).strip()
    if connection and connection != ws.config.connection:
        try:
            set_values(ws.root, {"connection.name": connection})
            ws.reload_config()
            done["connection"] = connection
        except ConfigError as e:
            say(f"  skipped: {e}")

    # -- doctor ----------------------------------------------------------
    if typer.confirm("Run environment checks now?", default=True):
        report = _doctor_report()
        for check in report["checks"]:
            say(f"  {'ok  ' if check['ok'] else 'FAIL'} {check['check']}: {check['detail']}")
        done["doctor_ok"] = report["ok"]

    # -- team library ----------------------------------------------------
    say("\nA team library (an ordinary git repo) shares knowledge, workflows, and records.")
    source = typer.prompt(
        "Team library git URL or local path (blank to skip)", default="", show_default=False
    ).strip()
    if source:
        from grayson.library import link_library

        auto_push = typer.confirm("Auto commit+push library writes?", default=False)
        try:
            done["library"] = link_library(ws, source, auto_push=auto_push)
            ws.reload_config()
        except (FileExistsError, FileNotFoundError, RuntimeError, OSError) as e:
            say(f"  library link failed: {e} — retry later with `grayson library link`")

    # -- harness ---------------------------------------------------------
    say("\nThe protocol file teaches your agent how to drive grayson.")
    harness = typer.prompt(
        "Harness (claude-code | cursor | codex | copilot | skip)", default="claude-code"
    ).strip()
    if harness != "skip":
        try:
            done["harness"] = generate_harness(ws.root, harness)
        except ValueError as e:
            say(f"  skipped: {e}")
            harness = "skip"
    if harness != "skip":
        status = guard_status(ws.root, harness)
        if status.get("supported"):
            say(
                "\nGuard permissions add deny rules so the agent calling `snow` "
                "directly, or reading .grayson/ state, hits a permission prompt:\n  "
                + "\n  ".join(guard_rules_display(harness))
            )
            if typer.confirm("Apply guard permissions?", default=False):
                try:
                    done["guard_permissions"] = apply_guard(ws.root, harness)
                except ValueError as e:
                    say(f"  skipped: {e}")
        else:
            say("\nHarness guard setup for this harness (human-configured):")
            say(status["guidance"])

        mstat = mcp_status(ws.root, harness)
        if mstat.get("supported"):
            say(
                "\ngrayson's MCP server mirrors the CLI one-to-one; its stdio "
                f"entry can be written to {mstat['file']} (only the `grayson` "
                "entry — other servers untouched)."
            )
            if typer.confirm("Register grayson's MCP server there?", default=False):
                try:
                    done["mcp_config"] = apply_mcp(ws.root, harness)
                except ValueError as e:
                    say(f"  skipped: {e}")
        else:
            say("\nMCP setup for this harness (human-configured):")
            say(mstat["guidance"])

    done["next"] = [
        "try the sandbox: grayson sandbox init my-demo (planted bugs + answer key)",
        "start the console: grayson ui serve",
        "then ask your agent to run a workflow — grayson enforces the rails",
    ]
    say("")
    emit(done)


def _doctor_report() -> dict:
    checks: list[dict] = []
    ws: Workspace | None = None
    try:
        ws = Workspace.find()
        checks.append({"check": "workspace", "ok": True, "detail": str(ws.root)})
    except FileNotFoundError as e:
        checks.append({"check": "workspace", "ok": False, "detail": str(e)})

    sandbox = ws is not None and ws.config.connection == "sandbox"
    if sandbox:
        from grayson.sandbox.executor import locate_warehouse

        db = locate_warehouse(ws.root)
        checks.append(
            {
                "check": "sandbox_warehouse",
                "ok": db.is_file(),
                "detail": str(db) if db.is_file() else "missing — run `grayson sandbox reset`",
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
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


@app.command()
def upgrade() -> None:
    """Upgrade grayson in place (uv tool installs; dev checkouts get instructions)."""
    uv = shutil.which("uv")
    if uv is None:
        fail("uv not found on PATH — install uv, or upgrade however grayson was installed")
        return
    listed = subprocess.run([uv, "tool", "list"], capture_output=True, text=True, timeout=60)
    if "grayson-sql" not in listed.stdout:
        emit(
            {
                "upgraded": False,
                "detail": "not a `uv tool` install — for a development checkout run "
                "`git pull && uv sync` in the repo",
            }
        )
        return
    proc = subprocess.run(
        [uv, "tool", "upgrade", "grayson-sql"], capture_output=True, text=True, timeout=600
    )
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        fail(f"uv tool upgrade failed: {out[-1000:]}")
        return
    emit({"upgraded": True, "detail": out[-1000:]})


@app.command()
def status() -> None:
    """Where am I? Workspace, latest session, and what needs attention next."""
    ws = _workspace()
    ids = ws.list_session_ids()
    out: dict = {
        "workspace": str(ws.root),
        "connection": ws.config.connection,
        "sessions": len(ids),
    }
    hints: list[str] = []
    if not ids:
        out["latest_session"] = None
        hints.append(
            "start a session: grayson session start --workflow <name> "
            "--table DB.SCHEMA.TABLE (workflows: grayson workflow list)"
        )
        if ws.config.connection == "sandbox":
            hints.append(
                "sandbox targets: SANDBOX.SHOP.CUSTOMERS (table-health), "
                "SANDBOX.SHOP.ORDERS_ENRICHED (bug-hunter), "
                "SANDBOX.SHOP.PAYMENTS + PAYMENTS_V2 (migration-parity)"
            )
    else:
        s = Session(ws, ids[-1])
        summary = s.summary()
        ready = engine.readiness(s, ws.workflows_dir)
        open_iv = s.interventions("open")
        pending = s.proposals("proposed")
        out["latest_session"] = {
            "id": s.id,
            "title": summary["title"],
            "workflow": summary["workflow"],
            "stage": summary["stage"],
            "queries_executed": summary["queries_executed"],
            "open_checks": ready["open_checks"],
            "findings_total": ready["findings_total"],
            "findings_unaccepted": ready["findings_unaccepted"],
            "open_interventions": [i["iid"] for i in open_iv],
            "proposals_pending": [p["pid"] for p in pending],
        }
        if open_iv:
            hints.append(
                f"{len(open_iv)} intervention(s) await your answer — "
                "grayson ui serve (opens the console)"
            )
        if ready["findings_unaccepted"]:
            hints.append(
                f"{len(ready['findings_unaccepted'])} finding(s) to review — "
                "accept in the console or: grayson finding accept latest <fid>"
            )
        if pending:
            hints.append(
                f"{len(pending)} proposal(s) awaiting decision — approve/reject in the console"
            )
        if ready["open_checks"]:
            hints.append(f"checkpoints still open for the agent: {', '.join(ready['open_checks'])}")
        if not hints:
            hints.append("nothing waiting on you — full detail: grayson session report latest")
        hints.append("tip: 'latest' works anywhere a session id is expected")
    out["hints"] = hints
    emit(out)


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
    new: bool = typer.Option(
        False, "--new", help="Start even if an identical just-created session exists."
    ),
    inputs: list[str] = typer.Option(
        [],
        "--input",
        "-I",
        help='Workflow setup input as key="value" (repeatable) — recorded on the '
        "session so it says why it was started, not just the chat transcript.",
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        help="Walk the workflow's setup inputs as prompts (terminal only — for a "
        "human driving the session by hand; agents pass --input instead).",
    ),
) -> None:
    """Start a QA session. Guard profile is resolved then per-setting overrides apply."""
    ws = _workspace()
    try:
        tpl = get_workflow(workflow, ws.workflows_dir)
    except WorkflowNotFound as e:
        fail(str(e.args[0] if e.args else e))
        return
    provided: dict[str, str] = {}
    for item in inputs:
        if "=" not in item:
            fail(f"--input expects key=value, got {item!r}")
            return
        key, value = item.split("=", 1)
        provided[key.strip()] = value.strip()
    unknown = tpl.unknown_input_keys(provided)
    if unknown:
        fail(
            f"unknown setup input(s) {unknown} for workflow '{workflow}' "
            f"(defined: {tpl.input_keys() or 'none'})"
        )
        return
    if interactive:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            fail("--interactive needs a terminal — pass --input key=value instead")
            return
        for setup_input in tpl.setup_inputs:
            if str(provided.get(setup_input.key) or "").strip():
                continue
            prompt = f"{setup_input.key} — {setup_input.prompt.strip()}"
            if setup_input.required:
                provided[setup_input.key] = typer.prompt(prompt)
            else:
                answer = typer.prompt(f"{prompt} (blank to skip)", default="", show_default=False)
                if answer.strip():
                    provided[setup_input.key] = answer
    # Re-running session start moments later (lost output, shell retry) reuses
    # the fresh session instead of littering the workspace with twins.
    if not new:
        dup = find_recent_duplicate(ws, workflow, tables)
        if dup:
            s = Session(ws, dup)
            if provided:
                s.set_setup_inputs(provided)
            emit(
                {
                    "reused_existing": True,
                    "session": s.summary(),
                    "checkpoints": s.checkpoints(),
                    "setup_inputs": s.setup_inputs(),
                    "note": f"an identical session '{dup}' was created moments ago and has "
                    "no work yet — continuing with it. Pass --new to force a separate one.",
                }
            )
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
    if provided:
        session.set_setup_inputs(provided)
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
        "setup_inputs": provided,
    }
    missing_inputs = tpl.missing_required_inputs(provided)
    if missing_inputs:
        result["setup_inputs_missing"] = missing_inputs
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
    registry = ViewRegistry(ws.views_dir)
    result["view_coverage"] = registry.coverage_check(tables, current)
    # matching library views enter the query scope now — querying them must not
    # trip the guard, and evidence touching them must count
    result["views_in_scope"] = enter_session_scope(registry, session, tables)
    knowledge = KnowledgeStore(ws.knowledge_dir)
    result["knowledge"] = {t: knowledge.read(t)["facts"] for t in tables}
    result["knowledge_gaps"] = sorted(t for t, facts in result["knowledge"].items() if not facts)
    from grayson.checks import ChecksStore

    result["external_checks"] = ChecksStore(ws.checks_dir).summary(tables or None)
    result["hints"] = [
        "human console (interventions, reviews, approvals): grayson ui serve",
        f'run a guarded query: grayson query run {session.id} -q "SELECT ..."',
        "'latest' works in place of the session id in any command",
    ]
    if result["knowledge_gaps"]:
        result["hints"].insert(
            0,
            f"no recorded knowledge for {', '.join(result['knowledge_gaps'])} — confirm "
            "grain/semantics with the user early (intervention), record durable answers "
            "with `grayson knowledge add`, or run the table-onboarding workflow first",
        )
    failing = result["external_checks"]["failing"]
    if failing:
        ids = ", ".join(f["check_id"] for f in failing)
        result["hints"].insert(
            0,
            f"{len(failing)} external deterministic check(s) are FAILING on the target "
            f"tables ({ids}) — these are pre-vetted leads: replicate each with a guarded "
            "query first (their `sql`/`details` are in external_checks.failing), then "
            "widen the investigation",
        )
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


@session_app.command("delete")
def session_delete(
    session_id: str,
    yes: bool = typer.Option(False, "--yes", help="Confirm permanent deletion."),
) -> None:
    """Permanently delete a session — audit trail and cached data included."""
    s = _session(session_id)
    if not yes:
        fail(
            f"this permanently deletes session '{s.id}' (audit trail and cached data) — "
            "re-run with --yes to confirm"
        )
        return
    s.delete()
    emit({"deleted": s.id})


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
    from grayson.report import build_report, render_markdown

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


# -- charts --------------------------------------------------------------


@chart_app.command("add")
def chart_add(
    session_id: str,
    artifact: str = typer.Option(..., "--artifact", "-a", help="Cached artifact id (q_XXXX)."),
    kind: str = typer.Option(..., "--kind", "-k", help="bar | line | scatter"),
    x: str = typer.Option(..., "--x", "-x", help="Column for the x axis."),
    y: list[str] = typer.Option(..., "--y", "-y", help="Measure column(s); up to 3."),
    title: str = typer.Option(..., "--title", help="What the chart shows."),
    note: str = typer.Option("", "--note", help="One-line read: what should the viewer see?"),
    worker: str = typer.Option(None, "--worker"),
) -> None:
    """Build a chart from a cached artifact — it appears live in the console.

    Aggregate/order the data with SQL first (query run or cache query), then
    chart the resulting artifact. Every chart is traceable to its query id.
    The response includes `text`, a terminal rendering — paste it into your
    chat reply so the user sees the shape without leaving the conversation."""
    from grayson.charts import ChartError, add_chart, chart_data, render_text

    s = _session(session_id)
    try:
        spec = add_chart(s, artifact, kind, x, list(y), title, note, worker)
    except ChartError as e:
        fail(str(e))
        return
    emit(
        {
            **spec,
            "text": render_text(spec, chart_data(s, spec)),
            "hint": "paste `text` into your chat reply (inside a code block) so the "
            "user sees the shape now; the full chart is live in the console",
        }
    )


@chart_app.command("list")
def chart_list(session_id: str) -> None:
    from grayson.charts import list_charts

    emit(list_charts(_session(session_id)))


@chart_app.command("show")
def chart_show(session_id: str, chart_id: str) -> None:
    """A chart's spec, the exact points it plots, and its terminal rendering."""
    from grayson.charts import chart_data, get_chart, render_text

    s = _session(session_id)
    spec = get_chart(s, chart_id)
    if spec is None:
        fail(f"no chart '{chart_id}' in this session")
        return
    data = chart_data(s, spec)
    emit({**spec, "data": data, "text": render_text(spec, data)})


@chart_app.command("render")
def chart_render(
    session_id: str,
    chart_id: str,
    out: Path = typer.Option(..., "--out", "-o", help="Destination .svg file."),
) -> None:
    """Export a chart as a standalone SVG file."""
    from grayson.charts import brand_export, chart_data, get_chart, render_svg

    s = _session(session_id)
    spec = get_chart(s, chart_id)
    if spec is None:
        fail(f"no chart '{chart_id}' in this session")
        return
    data = chart_data(s, spec)
    out.write_text(brand_export(render_svg(spec, data)), encoding="utf-8")
    emit({"written": str(out), "chart_id": chart_id, "points": len(data["points"])})


# -- workflow ------------------------------------------------------------


@workflow_app.command("list")
def workflow_list() -> None:
    """List available workflow templates (built-in + library extensions)."""
    from grayson.workflows import override_problems

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
    problems = override_problems(overrides)
    if problems:
        typer.echo(
            json.dumps(
                {
                    "warning": f"{len(problems)} library workflow file(s) are not "
                    "loadable — run `grayson workflow lint`",
                    "problems": problems,
                },
                indent=2,
            ),
            err=True,
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
        fail(str(e.args[0] if e.args else e))


@workflow_app.command("new")
def workflow_new(
    name: str = typer.Argument(..., help="Workflow name (lowercase, hyphens), e.g. orders-health."),
    fork: str = typer.Option(
        None, "--fork", help="Start from a copy of this existing workflow (lineage recorded)."
    ),
    title: str = typer.Option("", "--title"),
) -> None:
    """Scaffold a new workflow in the team library — blank, or forked from an
    existing one. Core names are refused (core templates are canonical). The
    file is stamped with your `grayson user` id; edit it, lint it, push it."""
    from grayson.identity import get_user_id
    from grayson.workflows import lint_workflows
    from grayson.workflows.authoring import WorkflowAuthoringError, create_workflow

    ws = _workspace()
    try:
        path = create_workflow(
            ws.workflows_dir, name, fork_of=fork, title=title, user_id=get_user_id()
        )
    except (WorkflowAuthoringError, WorkflowNotFound) as e:
        fail(str(e.args[0] if e.args else e))
        return
    emit(
        {
            "created": str(path),
            **({"forked_from": fork} if fork else {}),
            "lint": lint_workflows(ws.workflows_dir),
            "next": "edit the YAML (or use the console's Workflows tab), then "
            "`grayson workflow lint` and `grayson library push`",
        }
    )


@workflow_app.command("lint")
def workflow_lint() -> None:
    """Validate the library's workflow YAML: parse/shape errors, core-name
    shadowing, duplicate names or checkpoint keys, unknown findings schemas
    (errors), plus quality warnings. Exits non-zero on errors."""
    from grayson.workflows import lint_workflows

    try:
        ws = Workspace.find()
        overrides = ws.workflows_dir
    except FileNotFoundError:
        overrides = None
    report = lint_workflows(overrides)
    emit(report)
    if not report["ok"]:
        raise typer.Exit(1)


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


# -- config --------------------------------------------------------------


@config_app.command("show")
def config_show() -> None:
    """Current workspace configuration, resolved (profiles, scopes, library)."""
    from grayson.config_edit import config_summary

    emit(config_summary(_workspace().root))


@config_app.command("set")
def config_set(
    assignments: list[str] = typer.Argument(
        ...,
        help="key=value pairs, e.g. defaults.guard_profile=strict scopes.strict=true "
        "library.auto_push=true connection.name=prod",
    ),
) -> None:
    """Change workspace settings (validated; only the touched sections are rewritten).

    This is a user command: it edits the rails agents run inside, so agents must
    not run it — the MCP surface exposes configuration read-only."""
    from grayson.config_edit import ConfigError, config_summary, set_values

    changes: dict[str, str] = {}
    for item in assignments:
        if "=" not in item:
            fail(f"expected key=value, got {item!r}")
            return
        key, value = item.split("=", 1)
        changes[key.strip()] = value.strip()
    ws = _workspace()
    try:
        result = set_values(ws.root, changes)
    except ConfigError as e:
        fail(str(e))
        return
    emit({**result, "config": config_summary(ws.root)})


@config_app.command("profile")
def config_profile(
    name: str = typer.Argument(..., help="Guard profile to create or edit."),
    auto_limit: int = typer.Option(None, "--auto-limit", min=0, help="Row cap (0 = off)."),
    timeout_seconds: int = typer.Option(None, "--timeout", min=0, help="Seconds (0 = off)."),
    budget_warn: int = typer.Option(None, "--budget-warn", min=0, help="Warn at N queries."),
    budget_cap: int = typer.Option(None, "--budget-cap", min=0, help="Hard cap (0 = off)."),
) -> None:
    """Create or edit a named guard profile (unset flags keep current values)."""
    from grayson.config_edit import ConfigError, set_guard_profile

    try:
        emit(
            set_guard_profile(
                _workspace().root,
                name,
                {
                    "auto_limit": auto_limit,
                    "timeout_seconds": timeout_seconds,
                    "budget_warn": budget_warn,
                    "budget_cap": budget_cap,
                },
            )
        )
    except ConfigError as e:
        fail(str(e))


# -- knowledge -----------------------------------------------------------


def _attach_library_sync(out: dict, ws: Workspace, message: str) -> None:
    """Auto commit+push the library after a write, when configured."""
    from grayson.library import maybe_auto_push

    sync = maybe_auto_push(ws, message)
    if sync is not None:
        out["library_sync"] = sync


@knowledge_app.command("show")
def knowledge_show(table: str) -> None:
    """Show a table's knowledge entry, with a base-descriptor completeness report."""
    ws = _workspace()
    try:
        doc = KnowledgeStore(ws.knowledge_dir).read(table)
    except ValueError as e:
        fail(str(e))
        return
    emit({**doc, "completeness": completeness(doc)})


@knowledge_app.command("set")
def knowledge_set(
    table: str,
    json_str: str = typer.Option(None, "--json", help="Inline JSON profile fields."),
    file: Path = typer.Option(None, "--file", "-f", help="JSON file with profile fields."),
) -> None:
    """Set structured base-descriptor fields: grain, columns, relationships,
    freshness, owners, open_questions (merged per-field)."""
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
        fail("no profile payload: use --file, --json, or stdin")
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"invalid JSON: {e}")
        return
    ws = _workspace()
    try:
        doc = KnowledgeStore(ws.knowledge_dir).set_profile(table, payload)
    except ValueError as e:
        fail(str(e))
        return
    out = {**doc, "completeness": completeness(doc)}
    _attach_library_sync(out, ws, f"grayson knowledge: profile {table.upper()}")
    emit(out)


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
        result = KnowledgeStore(ws.knowledge_dir).add_fact(
            table,
            fact,
            fact_id=fact_id,
            status=status,
            created_by=by,
            evidence=list(evidence),
        )
    except ValueError as e:
        fail(str(e))
        return
    out = dict(result)
    _attach_library_sync(out, ws, f"grayson knowledge: fact for {table.upper()}")
    emit(out)


@knowledge_app.command("confirm")
def knowledge_confirm(table: str, fact_id: str, by: str = typer.Option("user", "--by")) -> None:
    """Confirm a proposed/inferred fact (a user action)."""
    ws = _workspace()
    try:
        result = KnowledgeStore(ws.knowledge_dir).confirm_fact(table, fact_id, by)
    except (ValueError, KeyError) as e:
        fail(str(e.args[0] if e.args else e))
        return
    out = dict(result)
    _attach_library_sync(out, ws, f"grayson knowledge: confirm {fact_id} on {table.upper()}")
    emit(out)


@knowledge_app.command("set-files")
def knowledge_set_files(table: str, files: list[str] = typer.Option(..., "--file", "-f")) -> None:
    """Point future agents at the work-repo files that define this table."""
    ws = _workspace()
    try:
        result = KnowledgeStore(ws.knowledge_dir).set_definition_files(table, list(files))
    except ValueError as e:
        fail(str(e))
        return
    out = dict(result)
    _attach_library_sync(out, ws, f"grayson knowledge: definition files for {table.upper()}")
    emit(out)


@knowledge_app.command("search")
def knowledge_search(term: str) -> None:
    emit(KnowledgeStore(_workspace().knowledge_dir).search(term))


# -- checks --------------------------------------------------------------


@checks_app.command("status")
def checks_status(
    tables: list[str] = typer.Option(
        [], "--table", "-t", help="Only checks touching these tables."
    ),
) -> None:
    """Latest result per external check, failures and overdue runs called out."""
    from grayson.checks import ChecksStore

    emit(ChecksStore(_workspace().checks_dir).summary(list(tables) or None))


@checks_app.command("list")
def checks_list(
    tables: list[str] = typer.Option(
        [], "--table", "-t", help="Only checks touching these tables."
    ),
) -> None:
    """Latest run of every external check (full result payloads)."""
    from grayson.checks import ChecksStore

    emit(
        [r.model_dump() for r in ChecksStore(_workspace().checks_dir).latest(list(tables) or None)]
    )


@checks_app.command("show")
def checks_show(check_id: str) -> None:
    """One check's run history, newest first."""
    from grayson.checks import ChecksStore

    runs = ChecksStore(_workspace().checks_dir).history(check_id)
    if not runs:
        fail(f"no runs on file for check '{check_id}'")
        return
    emit([r.model_dump() for r in runs])


@checks_app.command("ingest")
def checks_ingest(
    path: Path = typer.Argument(..., help="A results JSON file, or a directory of them."),
    source: str = typer.Option(None, "--source", help="Fill in 'source' where results omit it."),
    manifest: Path = typer.Option(
        None,
        "--manifest",
        help="dbt manifest.json — resolves each test to its tables and compiled SQL.",
    ),
    ttl_hours: float = typer.Option(
        None,
        "--ttl-hours",
        help="Cadence expectation stamped on adapter-converted results (older = overdue).",
    ),
) -> None:
    """Validate external check results and fold them into the library
    (checks/ingested/, bounded history per check). Idempotent per (check, run_at).
    A dbt run_results.json is detected and converted automatically."""
    from grayson.checks import ChecksStore, scaffold_checks_dir

    ws = _workspace()
    if not path.exists():
        fail(f"path not found: {path}")
        return
    if manifest is not None and not manifest.is_file():
        fail(f"manifest not found: {manifest}")
        return
    scaffold_checks_dir(ws.checks_dir)
    out = ChecksStore(ws.checks_dir).ingest(
        path, source, manifest_path=manifest, ttl_hours=ttl_hours
    )
    if out["ingested"]:
        _attach_library_sync(out, ws, f"grayson checks: ingest {out['ingested']} result(s)")
    emit(out)


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
    check_freshness: bool = typer.Option(
        False,
        "--check-freshness",
        help="Fetch current LAST_ALTERED so stale views land in 'refresh'.",
    ),
) -> None:
    """Coverage check: which library views to reuse, refresh, or build."""
    ws = _workspace()
    registry = ViewRegistry(ws.views_dir)
    current = None
    if check_freshness:
        from grayson.core.run import fetch_last_altered

        sources = sorted(
            {t.upper() for t in tables}
            | {s for v in registry.matching(list(tables)) for s in v.normalized_sources()}
        )
        current = fetch_last_altered(ws.config.connection, ws.root, sources)
    emit(registry.coverage_check(list(tables), current))


@views_app.command("use")
def views_use(
    session_id: str,
    names: list[str] = typer.Argument(..., help="Registered view name(s) to bring into scope."),
) -> None:
    """Bring registered library views into a session's query scope mid-session.

    Only views already in the registry qualify — this widens scope to
    user-curated surfaces, never to arbitrary tables."""
    ws = _workspace()
    registry = ViewRegistry(ws.views_dir)
    resolved = []
    for name in names:
        entry = registry.get(name)
        if entry is None:
            fail(f"'{name}' is not in the view registry — only registered views can enter scope")
            return
        resolved.append(entry.name.upper())
    s = _session(session_id)
    s.add_scope(resolved)
    s.log_event("agent", "views_in_scope", {"views": resolved})
    emit({"views_in_scope": resolved, "scope": sorted(s.scope_tables)})


@views_app.command("register")
def views_register(
    name: str,
    purpose: str = typer.Option("", "--purpose"),
    source_tables: list[str] = typer.Option([], "--source", "-s"),
    base_files: list[str] = typer.Option([], "--base-file", "-b"),
    ddl_file: Path = typer.Option(None, "--ddl-file", help="File with the view DDL."),
    snapshot: bool = typer.Option(
        True,
        "--snapshot/--no-snapshot",
        help="Capture the sources' current LAST_ALTERED as the staleness baseline.",
    ),
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
    baseline = {}
    if snapshot and source_tables:
        from grayson.core.run import fetch_last_altered

        baseline = fetch_last_altered(ws.config.connection, ws.root, list(source_tables))
    out = ViewRegistry(ws.views_dir).register(entry, ddl, baseline).model_dump()
    out["staleness_baseline_captured"] = bool(baseline)
    if snapshot and source_tables and not baseline:
        out["note"] = (
            "could not fetch LAST_ALTERED for the sources (auth/connection?) — "
            "staleness detection will report this view as never-stale until re-registered"
        )
    _attach_library_sync(out, ws, f"grayson views: register {name}")
    emit(out)


# -- library -------------------------------------------------------------


@library_app.command("init")
def library_init(
    path: Path = typer.Argument(..., help="Directory for the new library repo."),
) -> None:
    """Scaffold a fresh team library repo (knowledge/, views/, workflows/)."""
    from grayson.library import init_library

    try:
        created = init_library(path)
    except OSError as e:
        fail(f"cannot create a library at '{path.resolve()}': {e}. cd to a writable directory")
        return
    emit(
        {
            "library": str(created),
            "next": "git init & push this dir, then set [library] path in your grayson.toml",
        }
    )


@library_app.command("link")
def library_link_cmd(
    source: str = typer.Argument(..., help="Git URL of the team library, or a local path."),
    dest: Path = typer.Option(
        None, "--dest", help="Where to clone (default: ~/.grayson/libraries/<repo-name>)."
    ),
    auto_push: bool = typer.Option(
        False,
        "--auto-push/--no-auto-push",
        help="Auto commit+push knowledge/view changes to the library remote.",
    ),
) -> None:
    """Connect this workspace to a team library. Clones the repo if given a git URL —
    the one-command path for a teammate joining an existing knowledge store."""
    from grayson.library import link_library

    try:
        emit(link_library(_workspace(), source, dest, auto_push))
    except (FileExistsError, FileNotFoundError, RuntimeError, OSError) as e:
        fail(str(e))


@library_app.command("push")
def library_push_cmd(
    message: str = typer.Option("grayson: library update", "--message", "-m"),
) -> None:
    """Commit and push the linked library repo (knowledge, views, workflows)."""
    from grayson.library import push_library

    emit(push_library(_workspace(), message))


@library_app.command("status")
def library_status_cmd() -> None:
    """Check whether the linked team library clone is behind its remote."""
    from grayson.library import library_status

    emit(library_status(_workspace()))


@library_app.command("pull")
def library_pull_cmd() -> None:
    from grayson.library import library_pull

    emit(library_pull(_workspace()))


@library_app.command("extract")
def library_extract_cmd(
    dest: Path = typer.Argument(..., help="Destination for the extracted library repo."),
) -> None:
    """Split this workspace's assets out into a new shareable library repo."""
    from grayson.library import extract_library

    emit(extract_library(_workspace(), dest))


# -- user ----------------------------------------------------------------


@user_app.command("set")
def user_set(
    user_id: str = typer.Argument(..., help="Alphanumeric id, e.g. your initials."),
) -> None:
    """Set your user id (stored per-user in ~/.grayson/config.toml, never in the
    workspace). It stamps knowledge facts and library commit messages so shared
    history stays attributable. Set it once after install."""
    from grayson.identity import set_user_id, user_config_path

    try:
        stored = set_user_id(user_id)
    except ValueError as e:
        fail(str(e))
        return
    emit({"user_id": stored, "stored_in": str(user_config_path())})


@user_app.command("show")
def user_show() -> None:
    """Show the configured user id (GRAYSON_USER_ID overrides the file)."""
    from grayson.identity import get_user_id, user_config_path

    uid = get_user_id()
    emit(
        {
            "user_id": uid,
            "stored_in": str(user_config_path()),
            **({} if uid else {"hint": "set one with `grayson user set <id>`"}),
        }
    )


# -- audit ---------------------------------------------------------------


@audit_app.command("reconcile")
def audit_reconcile(
    hours: int = typer.Option(24, "--hours", min=1, max=168, help="History window to check."),
    limit: int = typer.Option(10000, "--limit", min=1, help="Max history rows to fetch."),
    ingest: bool = typer.Option(
        False,
        "--ingest",
        help="Also record the verdict (counts only, no statement text) as an "
        "external check, so it shows on the Checks tab and at session start.",
    ),
) -> None:
    """Diff Snowflake's query history for this connection against grayson's own
    audit trail. Unmatched statements ran around grayson — a bypass review list.

    A human command by design: it reads QUERY_HISTORY (full statement text),
    which agents are denied both here (no MCP twin) and in the guard."""
    from grayson.audit import reconcile, reconcile_check_result

    ws = _workspace()
    report = reconcile(ws, hours=hours, limit=limit)
    if report.get("error"):
        fail(report["error"])
        return
    if ingest:
        import tempfile

        from grayson.checks import ChecksStore

        payload = json.dumps([reconcile_check_result(report)])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(payload)
            tmp = Path(f.name)
        try:
            report["ingested"] = ChecksStore(ws.checks_dir).ingest(tmp, source="grayson")
        finally:
            tmp.unlink(missing_ok=True)
        _attach_library_sync(report, ws, "grayson checks: audit reconcile")
    emit(report)


# -- records -------------------------------------------------------------


@records_app.command("search")
def records_search_cmd(
    term: str = typer.Argument("", help="Search term (empty lists everything)."),
    kind: str = typer.Option(None, "--kind", "-k", help="finding|proposal"),
    limit: int = typer.Option(20, "--limit"),
) -> None:
    """Search past findings and fix proposals across ALL sessions — how past
    problems were diagnosed and what fixed them."""
    from grayson.records import search_records

    try:
        emit(search_records(_workspace(), term, kind, limit))
    except ValueError as e:
        fail(str(e))


@records_app.command("show")
def records_show_cmd(
    session_id: str,
    kind: str = typer.Argument(..., help="finding|proposal"),
    record_id: str = typer.Argument(..., help="e.g. f_001 or p_001"),
) -> None:
    """Show one past record in full (payload, and verification for proposals)."""
    from grayson.records import get_record

    ws = _workspace()
    try:
        item = get_record(ws, resolve_session_id(ws, session_id), kind, record_id)
    except ValueError as e:
        fail(str(e))
        return
    if item is None:
        fail(f"no {kind} '{record_id}' in session '{session_id}'")
        return
    emit(item)


# -- harness -------------------------------------------------------------


@harness_app.command("init")
def harness_init(
    harness: str = typer.Argument(..., help="cursor | claude-code | codex | copilot"),
    path: Path = typer.Option(Path("."), "--path", help="Repo root to write into."),
    no_mcp: bool = typer.Option(False, "--no-mcp", help="Omit the MCP note."),
    guard_permissions: bool = typer.Option(
        None,
        "--guard-permissions/--no-guard-permissions",
        help="Also write harness deny rules blocking direct `snow` use and "
        "`.grayson/` state access (asked interactively when unset).",
    ),
    mcp_config: bool = typer.Option(
        None,
        "--mcp-config/--no-mcp-config",
        help="Also register `grayson mcp serve` in the harness's project MCP "
        "config file (asked interactively when unset).",
    ),
) -> None:
    """Generate the skill/instruction file that teaches a harness the grayson protocol.

    Optionally also writes permission deny rules so the agent's bypass paths
    (direct snow CLI, .grayson state files) hit a human-visible permission
    prompt instead of a paragraph of prose, and/or the harness's project MCP
    config registering grayson's server. Both are consent-based: never written
    without an explicit yes (a flag or an interactive confirm); `grayson
    harness guard` and `grayson harness mcp` inspect and reverse them later."""
    from grayson.harness import generate_harness
    from grayson.harness.mcp import apply_mcp, mcp_status
    from grayson.harness.permissions import apply_guard, guard_rules_display, guard_status

    root = path.resolve()
    try:
        out = generate_harness(root, harness, with_mcp=not no_mcp)
    except ValueError as e:
        fail(str(e))
        return
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    # -- guard permissions (consent-gated) --------------------------------
    status = guard_status(root, harness)
    if not status.get("supported"):
        # no machine-writable config for this harness — hand over the concrete
        # per-harness setup (denylist/hooks for Cursor, sandbox for Codex)
        out["guard_guidance"] = status["guidance"]
    else:
        wants_guard = guard_permissions
        if wants_guard is None and interactive:
            typer.echo(
                "\nAlso block the agent's bypass paths via harness permissions?\n"
                "This writes these deny rules to "
                + status["file"]
                + ":\n  "
                + "\n  ".join(guard_rules_display(harness))
                + "\n(friction + visibility, not containment — pair with a read-only "
                "Snowflake role; reverse anytime with `grayson harness guard remove`)",
                err=True,
            )
            wants_guard = typer.confirm("Apply guard permissions?", default=False)
        if wants_guard:
            try:
                out["guard_permissions"] = apply_guard(root, harness)
            except ValueError as e:
                out["guard_permissions"] = {"error": str(e)}
        elif wants_guard is None:
            out["hint"] = (
                "consider `grayson harness guard apply` (or --guard-permissions): deny "
                "rules that stop the agent calling `snow` around the guard"
            )

    # -- MCP config (consent-gated) ---------------------------------------
    mstat = mcp_status(root, harness)
    if not mstat.get("supported"):
        # no project-level MCP file for this harness (Codex: user-global toml)
        out["mcp_guidance"] = mstat["guidance"]
    else:
        wants_mcp = mcp_config
        if wants_mcp is None and interactive:
            typer.echo(
                "\nAlso register grayson's MCP server in "
                + mstat["file"]
                + "?\n(writes only the `grayson` stdio entry — `grayson mcp serve` — "
                "other servers untouched; reverse anytime with "
                "`grayson harness mcp remove`)",
                err=True,
            )
            wants_mcp = typer.confirm("Write MCP config?", default=False)
        if wants_mcp:
            try:
                out["mcp_config"] = apply_mcp(root, harness)
            except ValueError as e:
                out["mcp_config"] = {"error": str(e)}
        elif wants_mcp is None:
            out["mcp_hint"] = (
                "consider `grayson harness mcp apply` (or --mcp-config): registers "
                "`grayson mcp serve` in the harness's project MCP config"
            )
    emit(out)


@harness_app.command("guard")
def harness_guard(
    action: str = typer.Argument(..., help="status | apply | remove"),
    harness: str = typer.Option(
        "claude-code", "--harness", help="claude-code | copilot (cursor/codex: prints the steps)"
    ),
    path: Path = typer.Option(Path("."), "--path", help="Repo root holding the harness config."),
) -> None:
    """Inspect, apply, or remove grayson's harness deny rules (the 'no way but
    the highway' setup: direct `snow` use and `.grayson/` state access get a
    permission prompt instead of silently succeeding)."""
    from grayson.harness.permissions import apply_guard, guard_status, remove_guard

    actions = {"status": guard_status, "apply": apply_guard, "remove": remove_guard}
    if action not in actions:
        fail("action must be status, apply, or remove")
        return
    try:
        emit(actions[action](path.resolve(), harness))
    except ValueError as e:
        fail(str(e))


@harness_app.command("mcp")
def harness_mcp(
    action: str = typer.Argument(..., help="status | apply | remove"),
    harness: str = typer.Option(
        "claude-code",
        "--harness",
        help="claude-code | cursor | copilot (codex: prints the steps)",
    ),
    path: Path = typer.Option(Path("."), "--path", help="Repo root holding the harness config."),
) -> None:
    """Inspect, write, or remove grayson's server entry in the harness's
    project MCP config (`grayson mcp serve`, stdio). Only the `grayson` entry
    is ever touched; user-authored servers in the same file are kept."""
    from grayson.harness.mcp import apply_mcp, mcp_status, remove_mcp

    actions = {"status": mcp_status, "apply": apply_mcp, "remove": remove_mcp}
    if action not in actions:
        fail("action must be status, apply, or remove")
        return
    try:
        emit(actions[action](path.resolve(), harness))
    except ValueError as e:
        fail(str(e))


# -- sandbox -------------------------------------------------------------

SANDBOX_CONFIG = """\
# grayson sandbox workspace — mock warehouse, no Snowflake needed

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
    path: Path = typer.Argument(Path("grayson-sandbox"), help="Directory for the demo workspace."),
) -> None:
    """Create a demo workspace with a local mock warehouse and planted QA problems.

    Seeds SANDBOX.SHOP.* tables (customers, orders, promos, payments) containing
    problems matched to the built-in workflows, and writes an answer key for the
    human. Point your agent at the workspace WITHOUT showing it the answer key.
    """
    from grayson.sandbox.executor import sandbox_db_path
    from grayson.sandbox.seed import render_answer_key, seed_sandbox

    _refuse_nested_workspace(path)
    try:
        ws = Workspace.init(path)
    except FileExistsError as e:
        fail(str(e))
        return
    except OSError as e:
        fail(f"cannot create a workspace at '{path.resolve()}': {e}. cd to a writable directory")
        return
    (ws.root / "grayson.toml").write_text(SANDBOX_CONFIG, encoding="utf-8")
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
                "grayson harness init claude-code   # or cursor | codex",
                "grayson status                     # what to do next, any time",
                "ask your agent to run a workflow (see the answer key for targets)",
                "keep SANDBOX_ANSWER_KEY.md away from the agent",
            ],
        }
    )


@sandbox_app.command("reset")
def sandbox_reset() -> None:
    """Re-seed the sandbox warehouse (fresh data, same planted problems)."""
    from grayson.sandbox.executor import locate_warehouse
    from grayson.sandbox.seed import seed_sandbox

    ws = _workspace()
    if ws.config.connection != "sandbox":
        fail("this workspace is not a sandbox (connection name is not 'sandbox')")
        return
    path = locate_warehouse(ws.root)  # migrates any legacy in-workspace file first
    seed_sandbox(path)
    emit({"reseeded": str(path)})


# -- mcp -----------------------------------------------------------------


@mcp_app.command("serve")
def mcp_serve(
    knowledge_only: bool = typer.Option(
        False,
        "--knowledge-only",
        help="Serve READ-ONLY knowledge-library tools (no sessions, no queries, "
        "no writes). Needs no workspace when --library is given.",
    ),
    library: str = typer.Option(
        None,
        "--library",
        help="Knowledge-only mode: the library to serve — a local path or a git URL "
        "(cloned under ~/.grayson/libraries and pulled on start). Defaults to the "
        "current workspace's library.",
    ),
    http: bool = typer.Option(
        False,
        "--http",
        help="Serve over streamable HTTP (bearer-token gated) instead of stdio. "
        "Run it where the Snowflake credentials live (a service account or "
        "container); point the agent at the URL from a machine that holds none — "
        "credential isolation by process boundary.",
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="HTTP bind address."),
    port: int = typer.Option(8850, "--port", help="HTTP port."),
    token: str = typer.Option(
        None,
        "--token",
        envvar="GRAYSON_MCP_TOKEN",
        help="Bearer token HTTP clients must present (generated and printed if unset).",
    ),
    no_token: bool = typer.Option(
        False,
        "--no-token",
        help="Serve WITHOUT the built-in bearer wall. Only for deployment behind "
        "a gateway that already authenticates every caller (and usually owns the "
        "Authorization header, e.g. platform SSO with an API token policy) — the "
        "port must not be reachable except through that gateway.",
    ),
) -> None:
    """Run the MCP server: stdio by default, --http for the credential-isolated
    deployment. --knowledge-only serves just the team library, read-only — for a
    teammate whose agent should be briefed by shared knowledge without running
    the harness end to end. The two flags compose."""
    if library and not knowledge_only:
        fail("--library only applies with --knowledge-only")
        return

    if knowledge_only:
        from grayson.library import library_pull_path, resolve_library_source
        from grayson.mcp.knowledge_server import build_knowledge_server

        if library:
            try:
                root, _action, _remote = resolve_library_source(library)
            except (FileExistsError, FileNotFoundError, RuntimeError, OSError) as e:
                fail(str(e))
                return
        else:
            ws = _workspace()
            root = ws.knowledge_dir.parent  # linked library clone, or the workspace itself
        library_pull_path(root)  # stale knowledge misleads; failure is non-fatal
        server = build_knowledge_server(root)
    else:
        from grayson.mcp.server import build_server

        server = build_server(_workspace())

    if not http:
        server.run(transport="stdio")
        return

    import secrets as _secrets

    from grayson.mcp.server import serve_http

    if no_token and token:
        fail("--no-token and --token are mutually exclusive")
        return
    resolved_token = None if no_token else (token or _secrets.token_urlsafe(24))
    typer.echo(f"grayson mcp (streamable HTTP): http://{host}:{port}/mcp", err=True)
    if no_token:
        typer.echo(
            "WARNING: no bearer wall — every peer that can reach this port has the "
            "full tool surface. Run this ONLY behind a gateway that authenticates "
            "every caller, on a port reachable solely through it.",
            err=True,
        )
    elif not token:
        typer.echo(f"bearer token (per-launch): {resolved_token}", err=True)
        typer.echo(
            "  pass a stable one via --token or GRAYSON_MCP_TOKEN for service deployments",
            err=True,
        )
    if resolved_token:
        typer.echo(
            "  client config: Authorization: Bearer <token>  (e.g. `claude mcp add "
            f"--transport http grayson http://{host}:{port}/mcp "
            '--header "Authorization: Bearer <token>"`)',
            err=True,
        )
    if host not in {"127.0.0.1", "localhost", "::1"}:
        typer.echo(
            "WARNING: binding beyond loopback serves plaintext HTTP — front it with "
            "TLS (reverse proxy) or an SSH tunnel before crossing machines",
            err=True,
        )
    serve_http(server, host, port, resolved_token)


# -- ui ------------------------------------------------------------------


@ui_app.command("serve")
def ui_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port"),
    no_token: bool = typer.Option(False, "--no-token", help="Disable the URL access token."),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the console in your browser."
    ),
) -> None:
    """Launch the local web console (loopback only, token-gated); opens your browser."""
    from grayson.ui.server import serve

    serve(_workspace(), host=host, port=port, use_token=not no_token, open_browser=open_browser)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
