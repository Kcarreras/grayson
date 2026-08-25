"""Local web console: FastAPI + server-rendered Jinja2, loopback only.

The console is the user's window into sessions — interventions, checkpoints,
findings, query log, proposals. Agents never touch it. It binds to 127.0.0.1
and gates every request on a per-launch token, carries no external assets, and
only ever mutates state through the same core APIs the CLI uses.
"""

from __future__ import annotations

import json
import secrets
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
from grayson.records import get_record, search_records
from grayson.ui.format import GLOSSARY, relationship_graph, split_sections
from grayson.ui.sqlhl import highlight_sql
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
                "graph": relationship_graph(
                    {**_library_docs(store), doc["table"]: doc}, focus=doc["table"]
                ),
                "table_checks": ChecksStore(workspace.checks_dir).summary([doc["table"]]),
            },
        )

    # -- checks -----------------------------------------------------------

    @app.get("/checks", response_class=HTMLResponse)
    def checks_page(request: Request) -> Any:
        _check(request)
        return templates.TemplateResponse(
            request,
            "checks.html",
            {"nav": "checks", "summary": ChecksStore(workspace.checks_dir).summary()},
        )

    # -- settings ---------------------------------------------------------

    def _settings_context(error: str | None = None) -> dict:
        from grayson.config_edit import config_summary
        from grayson.library import library_status

        workspace.reload_config()
        return {
            "nav": "settings",
            "cfg": config_summary(workspace.root),
            "lib": library_status(workspace),
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

    @app.get("/records/{sid}/{kind}/{rid}", response_class=HTMLResponse)
    def record_view(request: Request, sid: str, kind: str, rid: str) -> Any:
        _check(request)
        try:
            item = get_record(workspace, sid, kind, rid)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        if item is None:
            raise HTTPException(status_code=404, detail=f"no {kind} '{rid}' in session '{sid}'")
        return templates.TemplateResponse(
            request,
            "record.html",
            {
                "nav": "records",
                "session_id": sid,
                "session_title": _session(sid).get_meta("title", ""),
                "kind": kind,
                "record": item["record"],
            },
        )

    def _charts_context(s: Session, limit: int = 24) -> list[dict]:
        """Rendered charts, newest first — the visual trail of the analysis."""
        from grayson.charts import chart_data, list_charts, render_svg

        out = []
        for spec in reversed(list_charts(s)[-limit:]):
            try:
                data = chart_data(s, spec)
                svg = render_svg(spec, data)
            except (OSError, ValueError, KeyError):
                continue
            out.append({"spec": spec, "data": data, "svg": Markup(svg)})
        return out

    def _session_context(s: Session, error: str | None = None) -> dict:
        queries = s.query_log(100)
        return {
            "nav": "sessions",
            "s": s.summary(),
            "readiness": engine.readiness(s, workspace.workflows_dir),
            "checkpoints": s.checkpoints(),
            "findings": s.findings(),
            "interventions": s.interventions(),
            "proposals": s.proposals(),
            "queries": queries,
            "qsql": {q["qid"]: q.get("sql_raw") or "" for q in queries},
            "events": s.events(40),
            "charts": _charts_context(s),
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
            request, "intervention.html", {"nav": "sessions", "sid": sid, "item": item}
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
