"""Local web console: FastAPI + server-rendered Jinja2, loopback only.

The console is the user's window into sessions — interventions, checkpoints,
findings, query log, proposals. Agents never touch it. It binds to 127.0.0.1
and gates every request on a per-launch token, carries no external assets, and
only ever mutates state through the same core APIs the CLI uses.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from grayson import __version__
from grayson.checks import ChecksStore
from grayson.core import engine
from grayson.core.engine import EnforcementError
from grayson.core.session import STAGES, Session
from grayson.interventions import validate_response
from grayson.interventions.types import InterventionError
from grayson.knowledge import KnowledgeDocError, KnowledgeStore, completeness
from grayson.records import (
    delete_session_records,
    deletion_verdict,
    get_record,
    search_records,
    session_records,
)
from grayson.ui.format import GLOSSARY, paragraphs, relationship_graph, split_sections
from grayson.ui.sqlhl import highlight_sql
from grayson.util import parse_table_list
from grayson.workspace import Workspace

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
_COOKIE = "grayson_token"


def _help_widget(key: str) -> Markup:
    text = GLOSSARY.get(key, key)
    return Markup(
        '<span class="help" tabindex="0"><span class="h-i">i</span>'
        f'<span class="h-pop">{escape(text)}</span></span>'
    )


def build_app(workspace: Workspace, token: str | None = None) -> FastAPI:
    app = FastAPI(title="grayson console", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["token"] = token or ""
    templates.env.globals["help"] = _help_widget
    templates.env.globals["stages"] = STAGES
    templates.env.globals["asset_version"] = __version__
    templates.env.filters["sections"] = split_sections
    templates.env.filters["sqlhl"] = highlight_sql
    templates.env.filters["para"] = paragraphs

    def _valid(supplied: str | None) -> bool:
        return bool(supplied) and secrets.compare_digest(supplied, token)

    def _host_check(request: Request) -> None:
        # Host allowlist defeats DNS-rebinding (a malicious hostname resolving to
        # 127.0.0.1): the browser sends that hostname in Host, which we reject.
        host = (request.headers.get("host") or "").split(":")[0]
        if host and host not in {"127.0.0.1", "localhost", "::1", ""}:
            raise HTTPException(status_code=403, detail="unexpected Host header")

    def _check(request: Request) -> None:
        _host_check(request)
        # Accept the token from a cookie (set on first authenticated load) or the
        # query string, constant-time compared. The cookie keeps the secret out of
        # subsequent URLs (history/referrer). Loopback-bound; see docs/SECURITY.md.
        if not token:
            return
        if _valid(request.cookies.get(_COOKIE)) or _valid(request.query_params.get("t")):
            return
        raise HTTPException(status_code=403, detail="invalid or missing access token")

    def _set_cookie(response: Response) -> None:
        if token:
            response.set_cookie(_COOKIE, token, httponly=True, samesite="strict", path="/")

    def _session(sid: str) -> Session:
        try:
            return Session(workspace, sid)
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    def _removal(sid: str) -> dict:
        """Whether the person at the console may remove this session's
        published records — the author's or an admin's call, judged from the
        configured user id (docs/LIBRARY.md: a guard rail, not access control)."""
        from grayson.identity import get_user_id
        from grayson.library import library_admins, library_root

        return {
            "me": get_user_id(),
            **deletion_verdict(
                workspace.records_dir,
                sid,
                get_user_id(),
                library_admins(library_root(workspace)),
                solo=workspace.config.library_path is None,
            ),
        }

    def _redirect(path: str) -> RedirectResponse:
        sep = "&" if "?" in path else "?"
        target = f"{path}{sep}t={token}" if token else path
        return RedirectResponse(url=target, status_code=303)

    # Vendored JS/CSS. Deliberately not token-gated: these are public library
    # files carrying no workspace data, and a <script src> issued before the
    # session cookie exists would otherwise 403 and break the page. The Host
    # allowlist still applies. Paths are resolved and confined to STATIC_DIR.
    @app.get("/static/{path:path}")
    def static_asset(request: Request, path: str) -> Any:
        _host_check(request)
        target = (STATIC_DIR / path).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            raise HTTPException(status_code=404, detail="no such asset")
        # Cached hard, but only ever fetched through a ?v=<version> URL, so an
        # upgraded grayson asks for a different URL instead of being served a
        # stale bundle until the cache expires.
        immutable = request.query_params.get("v") == __version__
        cache = "public, max-age=31536000, immutable" if immutable else "no-cache"
        return FileResponse(target, headers={"Cache-Control": cache})

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Any:
        _check(request)
        sessions = []
        for sid in workspace.list_session_ids():
            try:
                s = Session(workspace, sid)
            except (OSError, ValueError):
                continue
            ready = engine.readiness(s, workspace.workflows_dir)
            sessions.append(
                {
                    "summary": s.summary(),
                    "open_interventions": len(s.interventions("open")),
                    "pending_proposals": len(s.proposals("proposed")),
                    "open_checks": len(ready["open_checks"]),
                    "findings": ready["findings_total"],
                }
            )
        sessions.sort(key=lambda x: x["summary"]["created_at"] or "", reverse=True)
        active = [r for r in sessions if r["summary"]["stage"] != "closed"]
        historical = [r for r in sessions if r["summary"]["stage"] == "closed"]
        response = templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "nav": "sessions",
                "active": active,
                "historical": historical,
                "needs_input": sum(r["open_interventions"] for r in active),
                "pending_proposals": sum(r["pending_proposals"] for r in active),
                "total_findings": sum(r["findings"] for r in sessions),
            },
        )
        _set_cookie(response)  # subsequent navigation authenticates via cookie, not URL
        return response

    def _library_docs(store: KnowledgeStore) -> dict[str, dict]:
        """Every table doc, keyed by FQN.

        The relationship canvas needs the whole library, not just the table being
        viewed: a relationship only the *other* side declared is invisible from
        here otherwise, and those one-sided declarations are exactly the gaps
        worth showing.
        """
        docs: dict[str, dict] = {}
        for fqn in store.all_tables():
            try:
                docs[fqn] = store.read(fqn)
            except (OSError, ValueError):
                continue
        return docs

    # -- knowledge --------------------------------------------------------

    @app.get("/knowledge", response_class=HTMLResponse)
    def knowledge_list(request: Request, q: str = "") -> Any:
        _check(request)
        store = KnowledgeStore(workspace.knowledge_dir)
        all_tables = store.all_tables()
        fact_hits = [h for h in store.search(q) if h.get("fact_id")] if q else []
        if q:
            ql = q.lower()
            hit_tables = {h["source"] for h in fact_hits}
            all_tables = [t for t in all_tables if ql in t.lower() or t in hit_tables]
        rows = []
        for fqn in all_tables:
            try:
                doc = store.read(fqn)
            except KnowledgeDocError as e:
                # a broken doc is a card with the parse error, never a 500 —
                # the whole point is telling the user which file to fix
                rows.append({"table": fqn, "grain": None, "completeness": None, "error": str(e)})
                continue
            rows.append(
                {"table": fqn, "grain": doc.get("grain"), "completeness": completeness(doc)}
            )
        # The map always shows the whole library, not the filtered subset: a
        # search narrows the list you are reading, not the schema you are in.
        graph = relationship_graph(_library_docs(store))
        return templates.TemplateResponse(
            request,
            "knowledge.html",
            {
                "nav": "knowledge",
                "tables": rows,
                "fact_hits": fact_hits,
                "q": q,
                "graph": graph,
            },
        )

    @app.get("/knowledge/{fqn}", response_class=HTMLResponse)
    def knowledge_table(request: Request, fqn: str) -> Any:
        _check(request)
        store = KnowledgeStore(workspace.knowledge_dir)
        try:
            doc = store.read(fqn)
        except KnowledgeDocError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return templates.TemplateResponse(
            request,
            "knowledge_table.html",
            {
                "nav": "knowledge",
                "doc": doc,
                "comp": completeness(doc),
                "snapshots": {
                    str(d["snapshot"]): store.read_snapshot(doc["table"], str(d["snapshot"]))
                    for d in doc["definitions"]
                    if d.get("snapshot")
                },
                "graph": relationship_graph(
                    {**_library_docs(store), doc["table"]: doc}, focus=doc["table"]
                ),
                "table_checks": ChecksStore(workspace.checks_dir).summary([doc["table"]]),
            },
        )

    def _knowledge_write(fqn: str, message: str, write) -> Any:
        """One console knowledge edit: perform it, auto-push, return to the page.

        The console is the surface where a human unambiguously is the human, so
        writes here carry user provenance — the counterpart of the CLI's
        interactive-terminal gate."""
        from grayson.library import maybe_auto_push

        store = KnowledgeStore(workspace.knowledge_dir)
        try:
            write(store)
        except (KnowledgeDocError, ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else e)) from e
        maybe_auto_push(workspace, message)
        return _redirect(f"/knowledge/{fqn}")

    @app.post("/knowledge/{fqn}/fact/{fact_id}/confirm")
    def knowledge_confirm_fact(request: Request, fqn: str, fact_id: str) -> Any:
        _check(request)
        return _knowledge_write(
            fqn,
            f"grayson knowledge: confirm {fact_id} on {fqn.upper()}",
            lambda store: store.confirm_fact(fqn, fact_id),
        )

    @app.post("/knowledge/{fqn}/fact")
    async def knowledge_add_fact(request: Request, fqn: str) -> Any:
        """A human writing a fact directly: added and confirmed in one action —
        the same two steps the provenance rail requires of everyone, just from
        the one caller who *is* the confirming authority."""
        _check(request)
        from grayson.identity import get_user_id

        form = await request.form()
        fact = str(form.get("fact", "")).strip()
        if not fact:
            raise HTTPException(status_code=400, detail="fact text is required")

        def write(store: KnowledgeStore) -> None:
            added = store.add_fact(fqn, fact, created_by=get_user_id() or "user")
            store.confirm_fact(fqn, added["id"])

        return _knowledge_write(fqn, f"grayson knowledge: fact for {fqn.upper()}", write)

    @app.post("/knowledge/{fqn}/question")
    async def knowledge_answer_question(request: Request, fqn: str) -> Any:
        _check(request)
        from grayson.identity import get_user_id

        form = await request.form()
        question = str(form.get("question", "")).strip()
        answer = str(form.get("answer", "")).strip()
        if not question or not answer:
            raise HTTPException(status_code=400, detail="question and answer are required")

        def write(store: KnowledgeStore) -> None:
            result = store.answer_open_question(
                fqn, question, answer, created_by=get_user_id() or "user"
            )
            store.confirm_fact(fqn, result["fact"]["id"])  # answered by the human directly

        return _knowledge_write(fqn, f"grayson knowledge: answer on {fqn.upper()}", write)

    @app.post("/knowledge/{fqn}/column")
    async def knowledge_describe_column(request: Request, fqn: str) -> Any:
        _check(request)
        form = await request.form()
        name = str(form.get("name", "")).strip()
        description = str(form.get("description", "")).strip()
        if not name:
            raise HTTPException(status_code=400, detail="column name is required")

        def write(store: KnowledgeStore) -> None:
            doc = store.read(fqn)
            columns = [dict(c) for c in doc.get("columns") or []]
            for col in columns:
                if str(col.get("name", "")).upper() == name.upper():
                    col["description"] = description
                    break
            else:
                columns.append({"name": name, "description": description})
            store.set_profile(fqn, {"columns": columns})

        return _knowledge_write(fqn, f"grayson knowledge: column {name} on {fqn.upper()}", write)

    @app.post("/knowledge/{fqn}/descriptor")
    async def knowledge_edit_descriptor(request: Request, fqn: str) -> Any:
        _check(request)
        form = await request.form()
        updates: dict[str, Any] = {}
        for key in ("grain", "freshness"):
            if key in form:
                updates[key] = str(form.get(key, "")).strip()
        if "owners" in form:
            updates["owners"] = [
                o.strip() for o in str(form.get("owners", "")).split(",") if o.strip()
            ]
        if not updates:
            raise HTTPException(status_code=400, detail="nothing to update")

        def write(store: KnowledgeStore) -> None:
            store.set_profile(fqn, updates)

        return _knowledge_write(fqn, f"grayson knowledge: profile {fqn.upper()}", write)

    # -- checks -----------------------------------------------------------

    @app.get("/checks", response_class=HTMLResponse)
    def checks_page(request: Request) -> Any:
        _check(request)
        return templates.TemplateResponse(
            request,
            "checks.html",
            {"nav": "checks", "summary": ChecksStore(workspace.checks_dir).summary()},
        )

    # -- workflows --------------------------------------------------------

    def _workflow_usage() -> dict[str, dict]:
        """Sessions per workflow: count and most recent start."""
        usage: dict[str, dict] = {}
        for sid in workspace.list_session_ids():
            try:
                meta = Session(workspace, sid).meta_all()
            except (OSError, ValueError):
                continue
            u = usage.setdefault(meta.get("workflow") or "", {"count": 0, "last": ""})
            u["count"] += 1
            u["last"] = max(u["last"], meta.get("created_at") or "")
        return usage

    def _workflows_context(error: str | None = None) -> dict:
        from grayson.identity import get_user_id
        from grayson.workflows import list_workflows, override_problems
        from grayson.workflows.registry import core_names

        user_id = get_user_id()
        usage = _workflow_usage()
        cores, team = [], []
        for tpl in list_workflows(workspace.workflows_dir):
            row = {
                "tpl": tpl,
                "usage": usage.get(tpl.name, {"count": 0, "last": ""}),
                "mine": bool(user_id) and tpl.created_by == user_id,
            }
            (cores if tpl.name in core_names() else team).append(row)
        return {
            "nav": "workflows",
            "cores": cores,
            "team": team,
            "problems": override_problems(workspace.workflows_dir),
            "fork_bases": sorted(r["tpl"].name for r in cores + team),
            "user_id": user_id,
            "error": error,
        }

    @app.get("/workflows", response_class=HTMLResponse)
    def workflows_page(request: Request) -> Any:
        _check(request)
        return templates.TemplateResponse(request, "workflows.html", _workflows_context())

    def _workflow_or_404(name: str):
        from grayson.workflows import WorkflowNotFound, get_workflow

        try:
            return get_workflow(name, workspace.workflows_dir)
        except WorkflowNotFound as e:
            raise HTTPException(status_code=404, detail=str(e.args[0] if e.args else e)) from e

    @app.get("/workflows/{name}", response_class=HTMLResponse)
    def workflow_detail(request: Request, name: str) -> Any:
        _check(request)
        from grayson.findings.schemas import FINDINGS_SCHEMAS
        from grayson.identity import get_user_id
        from grayson.workflows.authoring import can_edit
        from grayson.workflows.registry import core_names

        tpl = _workflow_or_404(name)
        user_id = get_user_id()
        schema = FINDINGS_SCHEMAS.get(tpl.findings_schema, {})
        return templates.TemplateResponse(
            request,
            "workflow_detail.html",
            {
                "nav": "workflows",
                "tpl": tpl,
                "is_core": tpl.name in core_names(),
                "editable": can_edit(tpl, user_id),
                "usage": _workflow_usage().get(tpl.name, {"count": 0, "last": ""}),
                "required_extra": schema.get("required_extra", []),
                "fork_name": f"{tpl.name}-{user_id}" if user_id else f"{tpl.name}-fork",
                "user_id": user_id,
            },
        )

    @app.get("/workflows/{name}/yaml")
    def workflow_yaml(request: Request, name: str) -> Any:
        _check(request)
        from grayson.workflows.authoring import _dump

        tpl = _workflow_or_404(name)
        path = workspace.workflows_dir / f"{name}.yaml"
        text = (
            path.read_text(encoding="utf-8") if path.is_file() else _dump(tpl)  # core: rendered
        )
        return Response(text, media_type="text/plain; charset=utf-8")

    def _workflow_edit_context(name: str, text: str, error: str | None = None) -> dict:
        return {"nav": "workflows", "name": name, "text": text, "error": error}

    @app.get("/workflows/{name}/edit", response_class=HTMLResponse)
    def workflow_edit(request: Request, name: str) -> Any:
        _check(request)
        from grayson.identity import get_user_id
        from grayson.workflows.authoring import can_edit
        from grayson.workflows.registry import core_names

        if name in core_names():
            raise HTTPException(
                status_code=403, detail="core workflows are canonical — fork instead"
            )
        path = workspace.workflows_dir / f"{name}.yaml"
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no library file {name}.yaml")
        tpl = _workflow_or_404(name)
        if not can_edit(tpl, get_user_id()):
            raise HTTPException(
                status_code=403,
                detail=f"'{name}' was created by '{tpl.created_by}' — fork it instead",
            )
        return templates.TemplateResponse(
            request,
            "workflow_edit.html",
            _workflow_edit_context(name, path.read_text(encoding="utf-8")),
        )

    @app.post("/workflows/{name}/edit")
    async def workflow_save(request: Request, name: str) -> Any:
        _check(request)
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push
        from grayson.workflows.authoring import WorkflowAuthoringError, save_workflow_yaml

        form = await request.form()
        text = str(form.get("yaml", ""))
        try:
            save_workflow_yaml(workspace.workflows_dir, name, text, get_user_id())
        except WorkflowAuthoringError as e:
            return templates.TemplateResponse(
                request,
                "workflow_edit.html",
                _workflow_edit_context(name, text, error=str(e)),
                status_code=400,
            )
        maybe_auto_push(workspace, f"grayson workflows: edit {name}")
        return _redirect(f"/workflows/{name}")

    @app.post("/workflows/{name}/fork")
    async def workflow_fork(request: Request, name: str) -> Any:
        _check(request)
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push
        from grayson.workflows import WorkflowNotFound
        from grayson.workflows.authoring import WorkflowAuthoringError, create_workflow

        form = await request.form()
        new_name = str(form.get("new_name", "")).strip()
        _workflow_or_404(name)
        try:
            create_workflow(workspace.workflows_dir, new_name, fork_of=name, user_id=get_user_id())
        except (WorkflowAuthoringError, WorkflowNotFound) as e:
            return templates.TemplateResponse(
                request,
                "workflows.html",
                _workflows_context(error=str(e.args[0] if e.args else e)),
                status_code=400,
            )
        maybe_auto_push(workspace, f"grayson workflows: fork {name} -> {new_name}")
        return _redirect(f"/workflows/{new_name}/edit")

    @app.post("/workflows/new")
    async def workflow_create(request: Request) -> Any:
        _check(request)
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push
        from grayson.workflows import WorkflowNotFound
        from grayson.workflows.authoring import WorkflowAuthoringError, create_workflow

        form = await request.form()
        new_name = str(form.get("new_name", "")).strip()
        fork_of = str(form.get("fork_of", "")).strip() or None
        try:
            create_workflow(
                workspace.workflows_dir, new_name, fork_of=fork_of, user_id=get_user_id()
            )
        except (WorkflowAuthoringError, WorkflowNotFound) as e:
            return templates.TemplateResponse(
                request,
                "workflows.html",
                _workflows_context(error=str(e.args[0] if e.args else e)),
                status_code=400,
            )
        maybe_auto_push(
            workspace,
            f"grayson workflows: new {new_name}" + (f" (fork of {fork_of})" if fork_of else ""),
        )
        return _redirect(f"/workflows/{new_name}/edit")

    # -- settings ---------------------------------------------------------

    def _settings_context(error: str | None = None) -> dict:
        from grayson.config_edit import config_summary
        from grayson.library import (
            library_admins,
            library_root,
            library_status,
            settings_last_change,
        )
        from grayson.workflows import list_workflows

        workspace.reload_config()
        root = library_root(workspace)
        return {
            "nav": "settings",
            "cfg": config_summary(workspace.root),
            "workflow_names": sorted(t.name for t in list_workflows(workspace.workflows_dir)),
            "lib": library_status(workspace),
            "admins": library_admins(root),
            "admins_changed": settings_last_change(root),
            "error": error,
        }

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Any:
        _check(request)
        return templates.TemplateResponse(request, "settings.html", _settings_context())

    @app.post("/settings/general")
    async def settings_general(request: Request) -> Any:
        _check(request)
        from grayson.config_edit import ConfigError, set_values

        form = await request.form()
        # Checkbox semantics: an unchecked box submits nothing, so each form
        # names what it owns ('only') and absent checkboxes in scope mean false.
        only = form.get("only")
        changes: dict[str, object] = {}
        if only == "auto_push":
            changes["library.auto_push"] = form.get("auto_push") == "true"
        else:
            changes["connection.name"] = form.get("connection", "")
            changes["defaults.guard_profile"] = form.get("guard_profile", "")
            changes["scopes.strict"] = form.get("strict") == "true"
            changes["scopes.allowed"] = form.get("allowed", "")
        try:
            set_values(workspace.root, changes)
        except ConfigError as e:
            return templates.TemplateResponse(
                request, "settings.html", _settings_context(error=str(e)), status_code=400
            )
        return _redirect("/settings")

    @app.post("/settings/profile/{name}")
    async def settings_profile(request: Request, name: str) -> Any:
        _check(request)
        from grayson.config_edit import ConfigError, set_guard_profile

        form = await request.form()
        try:
            updates = {
                key: int(form.get(key))
                for key in ("auto_limit", "timeout_seconds", "budget_warn", "budget_cap")
                if form.get(key) not in (None, "")
            }
            set_guard_profile(workspace.root, name, updates)
        except (ConfigError, ValueError) as e:
            return templates.TemplateResponse(
                request, "settings.html", _settings_context(error=str(e)), status_code=400
            )
        return _redirect("/settings")

    @app.post("/settings/workflow/{name}")
    async def settings_workflow(request: Request, name: str) -> Any:
        """One workflow's session defaults, saved whole: 'inherit' clears a
        field back to the usual resolution (flag > this > last-used/template)."""
        _check(request)
        from grayson.config_edit import ConfigError, set_workflow_defaults
        from grayson.workflows import list_workflows

        form = await request.form()
        profile = str(form.get("guard_profile", "")).strip() or None
        strict = {"true": True, "false": False}.get(str(form.get("strict_scope", "")).strip())
        try:
            known = {t.name for t in list_workflows(workspace.workflows_dir)}
            if name not in known:
                raise ConfigError(f"unknown workflow '{name}'")
            set_workflow_defaults(workspace.root, name, guard_profile=profile, strict_scope=strict)
        except ConfigError as e:
            return templates.TemplateResponse(
                request, "settings.html", _settings_context(error=str(e)), status_code=400
            )
        return _redirect("/settings")

    @app.post("/settings/library/{action}")
    def settings_library(request: Request, action: str) -> Any:
        _check(request)
        from grayson.library import library_pull, push_library

        if action == "pull":
            result = library_pull(workspace)
        elif action == "push":
            result = push_library(workspace, "grayson: library update (console)")
        else:
            raise HTTPException(status_code=400, detail="action must be pull or push")
        if not result.get("ok"):
            return templates.TemplateResponse(
                request,
                "settings.html",
                _settings_context(
                    error=f"library {action} failed: {result.get('detail') or result.get('output')}"
                ),
                status_code=400,
            )
        return _redirect("/settings")

    # -- records ----------------------------------------------------------

    @app.get("/records", response_class=HTMLResponse)
    def records_list(request: Request, q: str = "", kind: str = "") -> Any:
        _check(request)
        try:
            records = search_records(workspace, q, kind or None, limit=200)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return templates.TemplateResponse(
            request,
            "records.html",
            {"nav": "records", "records": records, "q": q, "kind": kind},
        )

    def _finding_lineage(s: Session, fid: str) -> list[dict]:
        """The supersession chain around `fid`, oldest first.

        Executed supersessions (superseded_by pointers, set only inside a user
        accept) form the chain; a not-yet-accepted finding that proposes to
        replace the head is appended as 'proposed'. Empty when there is no
        history to show.
        """
        findings = {f["fid"]: f for f in s.findings()}
        if fid not in findings:
            return []
        newer_to_older = {
            f["superseded_by"]: f["fid"]
            for f in findings.values()
            if f.get("superseded_by") in findings
        }
        root = fid
        while root in newer_to_older:
            root = newer_to_older[root]
        chain = [root]
        while True:
            nxt = findings[chain[-1]].get("superseded_by")
            if nxt in findings and nxt not in chain:
                chain.append(nxt)
            else:
                break
        proposed = next(
            (
                f["fid"]
                for f in findings.values()
                if (f["payload"] or {}).get("supersedes") == chain[-1]
                and not f["accepted"]
                and f["fid"] not in chain
            ),
            None,
        )
        if proposed:
            chain.append(proposed)
        if len(chain) < 2:
            return []
        return [
            {
                "fid": c,
                "title": findings[c]["title"],
                "ts": findings[c]["ts"],
                "accepted": findings[c]["accepted"],
                "superseded": bool(findings[c].get("superseded_by")),
                "rejected": findings[c].get("rejected", False),
                "proposed": c == proposed,
                "current": c == fid,
            }
            for c in chain
        ]

    @app.get("/records/{sid}/{kind}/{rid}", response_class=HTMLResponse)
    def record_view(request: Request, sid: str, kind: str, rid: str) -> Any:
        _check(request)
        try:
            item = get_record(workspace, sid, kind, rid)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        if item is None:
            raise HTTPException(status_code=404, detail=f"no {kind} '{rid}' in session '{sid}'")
        # a library-published record from a teammate has no local session:
        # render it from the published copy, without lineage or a session link
        from_library = item.get("source") == "library"
        if from_library:
            title, lineage = item.get("session_title", ""), []
        else:
            s = _session(sid)
            title = s.get_meta("title", "")
            lineage = _finding_lineage(s, rid) if kind == "finding" else []
        return templates.TemplateResponse(
            request,
            "record.html",
            {
                "nav": "records",
                "session_id": sid,
                "session_title": title,
                "kind": kind,
                "record": item["record"],
                "lineage": lineage,
                "from_library": from_library,
                "author": item.get("author"),
                "evidence_queries": item.get("evidence_queries") or [],
                "removal": _removal(sid),
            },
        )

    @app.post("/records/{sid}/delete")
    async def records_delete_ui(request: Request, sid: str) -> Any:
        """Remove a session's published records from the library — the
        author's action or an admin's; one commit with the reason."""
        _check(request)
        try:
            workspace.session_dir(sid)  # shape only; the session need not be local
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        form = await request.form()
        reason = str(form.get("reason", "")).strip()
        try:
            delete_session_records(workspace, sid, reason)
        except PermissionError as e:
            records = search_records(workspace, "", None, limit=200)
            return templates.TemplateResponse(
                request,
                "records.html",
                {"nav": "records", "records": records, "q": "", "kind": "", "error": str(e)},
                status_code=400,
            )
        return _redirect("/records")

    # -- charts: one chart, full size, with its data and the query behind it --

    def _chart_page(
        sid: str, chart_id: str, detail: bool = False
    ) -> tuple[Session, dict, dict, str]:
        from grayson.charts import chart_data, get_chart, render_svg

        s = _session(sid)
        spec = get_chart(s, chart_id)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"no chart '{chart_id}'")
        try:
            data = chart_data(s, spec)
            svg = render_svg(spec, data, detail=detail)
        except (OSError, ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return s, spec, data, svg

    @app.get("/session/{sid}/chart/{chart_id}.svg")
    def chart_svg(request: Request, sid: str, chart_id: str, detail: bool = False) -> Any:
        """The chart as a file — for a slide, a ticket, a message. Carries the
        export mark, like `chart render --out`; `detail` is the chart page's
        size, so the download matches what was on screen."""
        _check(request)
        from grayson.charts import brand_export

        _s, _spec, _data, svg = _chart_page(sid, chart_id, detail)
        return Response(
            brand_export(svg),
            media_type="image/svg+xml",
            headers={"Content-Disposition": f'attachment; filename="{sid}-{chart_id}.svg"'},
        )

    @app.get("/session/{sid}/chart/{chart_id}/svg")
    def chart_svg_inline(request: Request, sid: str, chart_id: str, detail: bool = False) -> Any:
        """The chart's markup for the console itself (the lightbox swaps the
        tile's rendering for the detail one) — unbranded, not a download."""
        _check(request)
        _s, _spec, _data, svg = _chart_page(sid, chart_id, detail)
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.get("/session/{sid}/chart/{chart_id}", response_class=HTMLResponse)
    def chart_detail(request: Request, sid: str, chart_id: str) -> Any:
        _check(request)
        from grayson.charts import list_charts

        s, spec, data, svg = _chart_page(sid, chart_id, detail=True)
        specs = list_charts(s)
        ids = [c["chart_id"] for c in specs]
        pos = ids.index(chart_id) if chart_id in ids else -1
        q = s.query_row(spec["qid"])
        return templates.TemplateResponse(
            request,
            "chart.html",
            {
                "nav": "sessions",
                "s": s.summary(),
                "spec": spec,
                "data": data,
                "svg": Markup(svg),
                "labels_cut": 'class="tick-cut"' in svg,
                "q": q,
                "sql_html": highlight_sql(q["sql_raw"]) if q else None,
                "prev_id": ids[pos - 1] if pos > 0 else None,
                "next_id": ids[pos + 1] if 0 <= pos < len(ids) - 1 else None,
                "position": pos + 1,
                "count": len(ids),
            },
        )

    def _charts_context(s: Session, limit: int = 24) -> list[dict]:
        """Rendered charts, newest first — the visual trail of the analysis."""
        from grayson.charts import chart_data, list_charts, render_svg

        out = []
        now = datetime.now(UTC)
        for spec in reversed(list_charts(s)[-limit:]):
            try:
                data = chart_data(s, spec)
                svg = render_svg(spec, data)
            except (OSError, ValueError, KeyError):
                continue
            # charts younger than the live-refresh interval slide in animated;
            # by the next refresh they render as settled tiles
            try:
                created_raw = str(spec.get("created_at", ""))
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                is_new = (now - created).total_seconds() < 15
            except ValueError:
                is_new = False
            out.append({"spec": spec, "data": data, "svg": Markup(svg), "is_new": is_new})
        return out

    def _session_context(s: Session, error: str | None = None) -> dict:
        queries = s.query_log(100)
        return {
            "nav": "sessions",
            "guard_profiles": sorted(workspace.config.guard_profiles),
            "s": s.summary(),
            "setup_inputs": s.setup_inputs(),
            "readiness": engine.readiness(s, workspace.workflows_dir),
            "checkpoints": s.checkpoints(),
            "findings": s.findings(),
            "interventions": s.interventions(),
            "proposals": s.proposals(),
            "queries": queries,
            "qsql": {q["qid"]: q.get("sql_raw") or "" for q in queries},
            "events": s.events(40),
            "charts": _charts_context(s),
            "published": _removal(s.id),
            "error": error,
        }

    @app.get("/session/{sid}", response_class=HTMLResponse)
    def session_detail(request: Request, sid: str) -> Any:
        _check(request)
        return templates.TemplateResponse(request, "session.html", _session_context(_session(sid)))

    @app.get("/session/{sid}/query/{qid}", response_class=HTMLResponse)
    def query_detail(request: Request, sid: str, qid: str) -> Any:
        _check(request)
        s = _session(sid)
        row = s.query_row(qid)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no query '{qid}'")
        executed = row.get("sql_executed")
        return templates.TemplateResponse(
            request,
            "query.html",
            {
                "nav": "sessions",
                "s": s.summary(),
                "q": row,
                "sql_html": highlight_sql(row.get("sql_raw") or ""),
                # show the guard's rewrite (e.g. an injected LIMIT) only when
                # it differs from what the agent submitted
                "sql_exec_html": (
                    highlight_sql(executed) if executed and executed != row.get("sql_raw") else None
                ),
                "q_tables": json.loads(row["tables_json"]) if row.get("tables_json") else [],
                "q_warnings": json.loads(row["warnings"]) if row.get("warnings") else [],
            },
        )

    @app.post("/session/{sid}/guard")
    async def session_guard_update(request: Request, sid: str) -> Any:
        """Change a live session's guard snapshot — profile and/or strict scope.

        The console is the surface where a human unambiguously is the human, so
        this is the UI twin of `grayson session guard`: applied to the snapshot,
        effective from the next statement, logged as a user event."""
        _check(request)
        s = _session(sid)
        current = s.summary()
        if current["stage"] == "closed":
            return templates.TemplateResponse(
                request,
                "session.html",
                _session_context(s, "session is closed — its guard snapshot is part of the record"),
                status_code=400,
            )
        form = await request.form()
        workspace.reload_config()
        changes: dict[str, Any] = {}
        profile = str(form.get("guard_profile", "")).strip()
        if profile and profile != current["guard_profile"]:
            try:
                settings = workspace.config.resolve_profile(profile)
            except KeyError as e:
                return templates.TemplateResponse(
                    request, "session.html", _session_context(s, str(e.args[0])), status_code=400
                )
            s.set_meta("guard", settings.model_dump_json())
            s.set_meta("guard_profile", profile)
            changes["guard_profile"] = profile
            changes["guard"] = settings.model_dump()
        strict_raw = str(form.get("strict_scope", "")).strip()
        strict = {"true": True, "false": False}.get(strict_raw)
        if strict is not None and strict != current["strict_scope"]:
            s.set_meta("strict_scope", json.dumps(strict))
            changes["strict_scope"] = strict
        if changes:
            s.log_event("user", "guard_changed", changes)
        return _redirect(f"/session/{sid}")

    @app.post("/session/{sid}/scope")
    async def session_scope_widen(request: Request, sid: str) -> Any:
        """Bring tables into a live session's readable scope — the UI twin of
        `grayson session scope`. A user action, logged with who and how; the
        agent-facing surfaces have no equivalent, by design."""
        _check(request)
        s = _session(sid)
        if s.stage == "closed":
            return templates.TemplateResponse(
                request,
                "session.html",
                _session_context(s, "session is closed — its scope is part of the record"),
                status_code=400,
            )
        form = await request.form()
        try:
            s.widen_scope(parse_table_list(str(form.get("tables", ""))), "user", via="console")
        except ValueError as e:
            return templates.TemplateResponse(
                request, "session.html", _session_context(s, str(e)), status_code=400
            )
        return _redirect(f"/session/{sid}")

    @app.post("/session/{sid}/close")
    async def session_close_ui(request: Request, sid: str) -> Any:
        """Close from the console — the human boundary; the gates still decide
        the outcome (findings or clean) and refuse an unready session, with the
        refusal shown in place."""
        _check(request)
        s = _session(sid)
        form = await request.form()
        note = str(form.get("note", "")).strip()
        try:
            engine.close_session(s, "user", note, workspace.workflows_dir)
        except EnforcementError as e:
            return templates.TemplateResponse(
                request, "session.html", _session_context(s, str(e)), status_code=400
            )
        return _redirect(f"/session/{sid}")

    @app.post("/session/{sid}/abandon")
    async def session_abandon_ui(request: Request, sid: str) -> Any:
        """The third ending: close without a result, on purpose and with a
        reason. Open interventions are cancelled; nothing publishes."""
        _check(request)
        s = _session(sid)
        form = await request.form()
        reason = str(form.get("reason", "")).strip()
        try:
            engine.abandon_session(s, "user", reason, workspace.workflows_dir)
        except EnforcementError as e:
            return templates.TemplateResponse(
                request, "session.html", _session_context(s, str(e)), status_code=400
            )
        return _redirect(f"/session/{sid}")

    @app.post("/session/{sid}/delete")
    async def session_delete_ui(request: Request, sid: str) -> Any:
        """Remove the session from this workspace — audit trail and cache
        included — and, when asked and allowed, its published records from the
        library. The library goes first: a refused removal deletes nothing."""
        _check(request)
        s = _session(sid)
        form = await request.form()
        reason = str(form.get("reason", "")).strip()
        if form.get("library") == "true" and session_records(workspace.records_dir, sid):
            try:
                delete_session_records(workspace, sid, reason)
            except PermissionError as e:
                return templates.TemplateResponse(
                    request,
                    "session.html",
                    _session_context(s, f"nothing deleted — {e}"),
                    status_code=400,
                )
        s.delete()
        return _redirect("/")

    @app.post("/session/{sid}/title")
    async def session_rename(request: Request, sid: str) -> Any:
        _check(request)
        s = _session(sid)
        form = await request.form()
        title = str(form.get("title", "")).strip()
        s.set_meta("title", title)
        s.log_event("user", "title_changed", {"title": title})
        return _redirect(f"/session/{sid}")

    @app.get("/session/{sid}/intervention/{iid}", response_class=HTMLResponse)
    def intervention_detail(request: Request, sid: str, iid: str) -> Any:
        _check(request)
        s = _session(sid)
        item = s.intervention(iid)
        if item is None:
            raise HTTPException(status_code=404, detail=f"no intervention '{iid}'")
        return templates.TemplateResponse(
            request,
            "intervention.html",
            {"nav": "sessions", "sid": sid, "s": s.summary(), "item": item},
        )

    @app.post("/session/{sid}/intervention/{iid}/respond")
    async def respond(request: Request, sid: str, iid: str) -> Any:
        _check(request)
        s = _session(sid)
        item = s.intervention(iid)
        if item is None:
            raise HTTPException(status_code=404, detail=f"no intervention '{iid}'")
        form = await request.form()
        try:
            response = _form_to_response(item, form)
            validated = validate_response(item["kind"], item["request"], response)
            s.respond_intervention(iid, validated)
        except (InterventionError, ValueError, KeyError) as e:
            return templates.TemplateResponse(
                request,
                "intervention.html",
                {
                    "nav": "sessions",
                    "sid": sid,
                    "s": s.summary(),
                    "item": item,
                    "error": str(e.args[0] if e.args else e),
                },
                status_code=400,
            )
        return _redirect(f"/session/{sid}")

    @app.post("/session/{sid}/finding/{fid}/accept")
    def accept_finding(request: Request, sid: str, fid: str) -> Any:
        _check(request)
        s = _session(sid)
        try:
            s.accept_finding(fid)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e.args[0])) from e
        return _redirect(f"/session/{sid}")

    @app.post("/session/{sid}/finding/{fid}/reject")
    async def reject_finding(request: Request, sid: str, fid: str) -> Any:
        _check(request)
        s = _session(sid)
        form = await request.form()
        reason = str(form.get("reason", "")).strip()
        try:
            s.reject_finding(fid, reason)
        except ValueError:
            return templates.TemplateResponse(
                request,
                "session.html",
                _session_context(
                    s, error=f"rejecting {fid} requires a reason — it is what the agent works from"
                ),
                status_code=400,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e.args[0])) from e
        return _redirect(f"/session/{sid}")

    @app.post("/session/{sid}/advance")
    def advance(request: Request, sid: str, to: str = Form(...), force: bool = Form(False)) -> Any:
        _check(request)
        s = _session(sid)
        try:
            engine.advance_stage(s, to, "user", force, workspace.workflows_dir)
        except EnforcementError as e:
            return templates.TemplateResponse(
                request, "session.html", _session_context(s, str(e)), status_code=400
            )
        return _redirect(f"/session/{sid}")

    @app.post("/session/{sid}/checkpoint/{key}/waive")
    async def waive_checkpoint(request: Request, sid: str, key: str) -> Any:
        _check(request)
        s = _session(sid)
        form = await request.form()
        reason = str(form.get("reason", "")).strip()
        try:
            engine.waive_checkpoint(s, key, reason, "user", workspace.workflows_dir)
        except EnforcementError as e:
            return templates.TemplateResponse(
                request, "session.html", _session_context(s, str(e)), status_code=400
            )
        return _redirect(f"/session/{sid}")

    @app.post("/session/{sid}/close-clean")
    async def close_clean(request: Request, sid: str) -> Any:
        """The human boundary on a negative result: someone vouches that the run
        cleared its checks and genuinely found nothing, so 'we looked and it was
        fine' enters the record as a result rather than an abandoned session."""
        _check(request)
        s = _session(sid)
        form = await request.form()
        note = str(form.get("note", "")).strip()
        try:
            engine.close_session(s, "user", note, workspace.workflows_dir)
        except EnforcementError as e:
            return templates.TemplateResponse(
                request, "session.html", _session_context(s, str(e)), status_code=400
            )
        return _redirect(f"/session/{sid}")

    @app.post("/session/{sid}/proposal/{pid}/{decision}")
    def decide_proposal(request: Request, sid: str, pid: str, decision: str) -> Any:
        _check(request)
        s = _session(sid)
        if decision not in {"approve", "reject"}:
            raise HTTPException(status_code=400, detail="decision must be approve or reject")
        from grayson.core import proposals as proposals_engine
        from grayson.core.proposals import ProposalError

        try:
            proposals_engine.decide(s, pid, approve=(decision == "approve"))
        except ProposalError as e:
            return templates.TemplateResponse(
                request, "session.html", _session_context(s, str(e)), status_code=400
            )
        return _redirect(f"/session/{sid}")

    return app


def _form_to_response(item: dict, form: Any) -> dict:
    """Translate an HTML form submission into the intervention response shape."""
    kind = item["kind"]
    if kind == "label_sample":
        labels = []
        n = len(item["request"].get("rows", []))
        for idx in range(n):
            choice = form.get(f"label_{idx}")
            if choice:
                labels.append(
                    {"row_index": idx, "label": choice, "note": form.get(f"note_{idx}", "")}
                )
        return {"labels": labels}
    if kind == "confirm_semantics":
        return {"decision": form.get("decision"), "note": form.get("note", "")}
    if kind == "choose":
        if item["request"].get("multi"):
            return {"selected": form.getlist("selected"), "note": form.get("note", "")}
        return {"selected": form.get("selected"), "note": form.get("note", "")}
    if kind == "scope_request":
        # unticked everything and submitted = declined; the response records it
        return {"granted": form.getlist("granted"), "note": form.get("note", "")}
    return {"text": form.get("text", "")}


def serve(
    workspace: Workspace,
    host: str = "127.0.0.1",
    port: int = 8765,
    use_token: bool = True,
    open_browser: bool = True,
) -> None:
    import threading
    import webbrowser

    import uvicorn

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing to bind the console to a non-loopback host")
    token = secrets.token_urlsafe(16) if use_token else None
    app = build_app(workspace, token)
    url = f"http://{host}:{port}/"
    if token:
        url += f"?t={token}"
    print(f"grayson console: {url}")
    print("(loopback only; keep this token private — it grants access to this workspace)")
    if open_browser:
        # after uvicorn has had a moment to bind; daemon so it never blocks exit
        threading.Timer(0.8, webbrowser.open, [url]).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
