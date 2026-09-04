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
from grayson.ui.format import (
    GLOSSARY,
    gap_label,
    paragraphs,
    relationship_graph,
    split_sections,
)
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
    templates.env.globals["gap_label"] = gap_label
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
        from grayson.knowledge import StandingContext, annotate_doc
        from grayson.library import effective_policy

        policy = effective_policy(workspace)
        ctx = StandingContext.build(workspace.records_dir, policy)
        rows = []
        for fqn in all_tables:
            try:
                doc = store.read(fqn)
            except KnowledgeDocError as e:
                # a broken doc is a card with the parse error, never a 500 —
                # the whole point is telling the user which file to fix
                rows.append({"table": fqn, "grain": None, "completeness": None, "error": str(e)})
                continue
            annotated = annotate_doc(doc, ctx)
            rows.append(
                {
                    "table": fqn,
                    "grain": doc.get("grain"),
                    "completeness": completeness(doc),
                    "standing": annotated["standing_counts"],
                    "contested": len(annotated["contested"]),
                    "agent_actions": len(annotated["agent_actions"]),
                }
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
        from grayson.knowledge import actions as knowledge_actions

        store = KnowledgeStore(workspace.knowledge_dir)
        try:
            doc = knowledge_actions.show(workspace, fqn)
        except KnowledgeDocError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        facts = doc["facts"]
        by_id = {f["id"]: f for f in facts}
        return templates.TemplateResponse(
            request,
            "knowledge_table.html",
            {
                "nav": "knowledge",
                "doc": doc,
                "comp": doc["completeness"],
                "live_facts": [f for f in facts if f["standing"] != "retired"],
                "retired_facts": [f for f in facts if f["standing"] == "retired"],
                "contested": [
                    {**c, "pair": [by_id.get(i) for i in c["facts"]]} for c in doc["contested"]
                ],
                "agent_actions": doc["agent_actions"],
                "policy": doc["policy"],
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

    def _lifecycle(fqn: str, fn, *args, **kwargs) -> Any:
        """One console lifecycle action: the human's, so never policy-refused;
        the action commits itself as one library commit."""
        from grayson.knowledge.actions import ActionRefused

        try:
            fn(workspace, *args, actor="user", surface="console", **kwargs)
        except ActionRefused as e:  # pragma: no cover - a user is never refused
            raise HTTPException(status_code=403, detail=str(e)) from e
        except (KnowledgeDocError, ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e.args[0] if e.args else e)) from e
        return _redirect(f"/knowledge/{fqn}")

    @app.post("/knowledge/{fqn}/fact/{fact_id}/retire")
    async def knowledge_retire_fact(request: Request, fqn: str, fact_id: str) -> Any:
        _check(request)
        from grayson.knowledge import actions as knowledge_actions

        form = await request.form()
        reason = str(form.get("reason", "")).strip()
        if not reason:
            raise HTTPException(status_code=400, detail="retiring a fact needs a reason")
        return _lifecycle(fqn, knowledge_actions.retire, fqn, fact_id, reason=reason)

    @app.post("/knowledge/{fqn}/fact/{fact_id}/restore")
    def knowledge_restore_fact(request: Request, fqn: str, fact_id: str) -> Any:
        _check(request)
        from grayson.knowledge import actions as knowledge_actions

        return _lifecycle(fqn, knowledge_actions.restore, fqn, fact_id)

    @app.post("/knowledge/{fqn}/fact/{fact_id}/supersede")
    async def knowledge_supersede_fact(request: Request, fqn: str, fact_id: str) -> Any:
        """A human correcting a fact: the corrected one is recorded, confirmed,
        and the supersession executes — one action, user provenance."""
        _check(request)
        from grayson.knowledge import actions as knowledge_actions

        form = await request.form()
        text = str(form.get("fact", "")).strip()
        if not text:
            raise HTTPException(status_code=400, detail="the corrected fact's text is required")
        return _lifecycle(fqn, knowledge_actions.supersede, fqn, fact_id, text)

    @app.post("/knowledge/{fqn}/reanchor")
    def knowledge_reanchor(request: Request, fqn: str) -> Any:
        _check(request)
        from grayson.knowledge import actions as knowledge_actions

        return _lifecycle(fqn, knowledge_actions.reanchor, fqn, None)

    @app.post("/knowledge/{fqn}/resolve")
    async def knowledge_resolve_pair(request: Request, fqn: str) -> Any:
        _check(request)
        from grayson.knowledge import actions as knowledge_actions

        form = await request.form()
        a = str(form.get("fact_a", "")).strip()
        b = str(form.get("fact_b", "")).strip()
        if not a or not b:
            raise HTTPException(status_code=400, detail="two fact ids are required")
        note = str(form.get("note", "")).strip()
        return _lifecycle(fqn, knowledge_actions.resolve, fqn, a, b, note=note)

    @app.post("/knowledge/{fqn}/question/dismiss")
    async def knowledge_dismiss_question(request: Request, fqn: str) -> Any:
        _check(request)
        from grayson.knowledge import actions as knowledge_actions

        form = await request.form()
        question = str(form.get("question", "")).strip()
        reason = str(form.get("reason", "")).strip()
        if not question or not reason:
            raise HTTPException(status_code=400, detail="question and reason are required")
        return _lifecycle(fqn, knowledge_actions.dismiss_question, fqn, question, reason)

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

    @app.post("/knowledge/{fqn}/definition")
    async def knowledge_add_definition(request: Request, fqn: str) -> Any:
        """A human recording where a table is defined. A local path is resolved
        against the workspace (repo, commit, hash); the entry carries user
        provenance and the console shows what is still missing."""
        _check(request)
        from grayson.knowledge.define import record_definition

        form = await request.form()
        path = str(form.get("path", "")).strip()
        if not path:
            raise HTTPException(status_code=400, detail="a definition needs a path")
        kind = str(form.get("kind", "")).strip() or None
        repo = str(form.get("repo", "")).strip() or None
        description = str(form.get("description", "")).strip() or None
        capture = str(form.get("capture", "")).strip().lower() in ("1", "on", "true", "yes")

        def write(store: KnowledgeStore) -> None:
            record_definition(
                store,
                fqn,
                path,
                workspace.root,
                kind=kind,
                repo=repo,
                description=description,
                capture=capture,
                by="user",
            )

        return _knowledge_write(fqn, f"grayson knowledge: definition for {fqn.upper()}", write)

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
        """Sessions per workflow: count, how many are still open, most recent start."""
        usage: dict[str, dict] = {}
        for sid in workspace.list_session_ids():
            try:
                meta = Session(workspace, sid).meta_all()
            except (OSError, ValueError):
                continue
            u = usage.setdefault(meta.get("workflow") or "", {"count": 0, "open": 0, "last": ""})
            u["count"] += 1
            if meta.get("stage") != "closed":
                u["open"] += 1
            u["last"] = max(u["last"], meta.get("created_at") or "")
        return usage

    _NO_USAGE = {"count": 0, "open": 0, "last": ""}

    def _schema_catalog() -> list[dict]:
        """Every findings schema unpacked — built in and library — with the
        workflows that use it and, for library ones, whose it is."""
        from grayson.findings.authoring import can_edit_schema, workflows_using
        from grayson.findings.library import core_schema_names, list_library_schemas
        from grayson.findings.schemas import describe_schema
        from grayson.identity import get_user_id

        user_id = get_user_id()
        schemas_dir = workspace.findings_schemas_dir
        rows = []
        for name in sorted(core_schema_names()):
            rows.append(
                {
                    **describe_schema(name, None, schemas_dir),
                    "used_by": workflows_using(name, workspace.workflows_dir),
                    "mine": False,
                    "editable": False,
                    "field_count": 0,
                }
            )
        for sc in list_library_schemas(schemas_dir):
            rows.append(
                {
                    **describe_schema(sc.name, None, schemas_dir),
                    "used_by": workflows_using(sc.name, workspace.workflows_dir),
                    "mine": bool(user_id) and sc.created_by == user_id,
                    "editable": can_edit_schema(sc, user_id),
                    "field_count": len(sc.fields),
                }
            )
        return rows

    def _workflows_context(error: str | None = None) -> dict:
        from grayson.identity import get_user_id
        from grayson.workflows import list_workflows, override_problems
        from grayson.workflows.registry import core_names

        user_id = get_user_id()
        usage = _workflow_usage()
        rows = []
        for tpl in list_workflows(workspace.workflows_dir):
            core = tpl.name in core_names()
            mine = bool(user_id) and tpl.created_by == user_id
            tags = ["core" if core else "team"]
            if mine:
                tags.append("mine")
            if tpl.forked_from:
                tags.append("fork")
            if tpl.chart_requirements():
                tags.append("charts")
            if tpl.findings_fields:
                tags.append("custom-schema")
            u = usage.get(tpl.name, _NO_USAGE)
            if u["open"]:
                tags.append("active")
            rows.append(
                {
                    "tpl": tpl,
                    "usage": u,
                    "mine": mine,
                    "core": core,
                    "tags": tags + [f"tag-{t}" for t in tpl.tags],
                    "group": 0 if core else 1,
                }
            )
        problems = override_problems(workspace.workflows_dir)
        user_tags = sorted({t for r in rows for t in r["tpl"].tags})
        return {
            "nav": "workflows",
            "rows": rows,
            "core_count": sum(1 for r in rows if r["core"]),
            "team_count": sum(1 for r in rows if not r["core"]),
            "problems": problems,
            "user_tags": user_tags,
            "tag_filters": [(f"tag-{t}", f"#{t}") for t in user_tags],
            "fold_open": len(rows) <= 8,
            "schemas": _schema_catalog(),
            "fork_bases": sorted(r["tpl"].name for r in rows),
            "user_id": user_id,
            "auto_push": bool(workspace.config.library_auto_push),
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

    def _detail_context(tpl, error: str | None = None) -> dict:
        from grayson.findings.library import core_schema_names, known_schema_names
        from grayson.findings.schemas import describe_schema
        from grayson.identity import get_user_id
        from grayson.workflows.authoring import can_edit, format_chart_lines
        from grayson.workflows.lint import lint_template
        from grayson.workflows.registry import core_names

        user_id = get_user_id()
        is_core = tpl.name in core_names()
        inputs_used = {i.key: tpl.checks_using(i.key) for i in tpl.setup_inputs}
        return {
            "nav": "workflows",
            "tpl": tpl,
            "is_core": is_core,
            "editable": can_edit(tpl, user_id),
            "mine": bool(user_id) and tpl.created_by == user_id,
            "usage": _workflow_usage().get(tpl.name, _NO_USAGE),
            "schema": describe_schema(
                tpl.findings_schema, tpl.findings_fields, workspace.findings_schemas_dir
            ),
            "schema_names": known_schema_names(workspace.findings_schemas_dir),
            "can_promote": bool(tpl.findings_fields) and tpl.findings_schema in core_schema_names(),
            "promote_name": f"{tpl.name.replace('-', '_')}_v1",
            "guard_profiles": sorted(workspace.config.guard_profiles),
            "warnings": [] if is_core else lint_template(tpl),
            "inputs_used": inputs_used,
            "chart_lines": {
                c.key: format_chart_lines(c.charts)
                for c in tpl.required_checks + tpl.suggested_checks
            },
            "check_keys": [c.key for c in tpl.required_checks + tpl.suggested_checks],
            "fork_name": f"{tpl.name}-{user_id}" if user_id else f"{tpl.name}-fork",
            "user_id": user_id,
            "auto_push": bool(workspace.config.library_auto_push),
            "error": error,
        }

    @app.get("/workflows/{name}", response_class=HTMLResponse)
    def workflow_detail(request: Request, name: str) -> Any:
        _check(request)
        tpl = _workflow_or_404(name)
        return templates.TemplateResponse(request, "workflow_detail.html", _detail_context(tpl))

    def _workflow_text(name: str, tpl) -> str:
        """The file as it stands in the library; a core template renders from
        its model (there is no library file to show)."""
        from grayson.workflows.authoring import _dump

        path = workspace.workflows_dir / f"{name}.yaml"
        return path.read_text(encoding="utf-8") if path.is_file() else _dump(tpl)

    @app.get("/workflows/{name}/yaml", response_class=HTMLResponse)
    def workflow_yaml(request: Request, name: str, raw: str = "") -> Any:
        """The definition as YAML: an in-console page with a copy button, or
        (`?raw=1`) the bare file as a download."""
        _check(request)
        from grayson.identity import get_user_id
        from grayson.workflows.authoring import can_edit
        from grayson.workflows.registry import core_names

        tpl = _workflow_or_404(name)
        text = _workflow_text(name, tpl)
        if raw:
            return Response(
                text,
                media_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{name}.yaml"'},
            )
        return templates.TemplateResponse(
            request,
            "workflow_yaml.html",
            {
                "nav": "workflows",
                "noun": "workflow",
                "base_path": "/workflows",
                "name": tpl.name,
                "text": text,
                "is_core": name in core_names(),
                "editable": can_edit(tpl, get_user_id()),
            },
        )

    def _workflow_edit_context(name: str, text: str, error: str | None = None) -> dict:
        return {
            "nav": "workflows",
            "noun": "workflow",
            "base_path": "/workflows",
            "name": name,
            "text": text,
            "error": error,
            "auto_push": bool(workspace.config.library_auto_push),
        }

    def _editable_or_403(name: str):
        """The library template, if the person at the console may edit it."""
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
        return tpl

    @app.get("/workflows/{name}/edit", response_class=HTMLResponse)
    def workflow_edit(request: Request, name: str) -> Any:
        _check(request)
        _editable_or_403(name)
        path = workspace.workflows_dir / f"{name}.yaml"
        return templates.TemplateResponse(
            request,
            "workflow_edit.html",
            _workflow_edit_context(name, path.read_text(encoding="utf-8")),
        )

    def _review_page(
        request: Request,
        *,
        base_path: str,
        file_dir: str,
        name: str,
        before: str,
        after: str,
        preview: str,
        warnings: list[str],
        origin: str,
        what: str,
        extra_files: list[tuple[str, str]] | None = None,
        confirm_path: str | None = None,
        hidden: dict[str, str] | None = None,
    ) -> Any:
        """The confirmation step every library edit passes through: what
        changes, the file as it will read, and lint's opinion of it. Nothing
        is written until the person confirms from here."""
        from grayson.util import unified_diff_text

        return templates.TemplateResponse(
            request,
            "workflow_review.html",
            {
                "nav": "workflows",
                "base_path": base_path,
                "file_dir": file_dir,
                "name": name,
                "text": after,
                "diff": unified_diff_text(
                    before,
                    after,
                    f"{file_dir}/{name}.yaml (library)",
                    f"{file_dir}/{name}.yaml (after save)",
                ),
                "unchanged": before == after and not extra_files,
                "preview": preview,
                "warnings": warnings,
                "origin": origin,
                "what": what,
                "extra_files": extra_files or [],
                "confirm_path": confirm_path or f"{base_path}/{name}/edit",
                "hidden": hidden or {},
                "auto_push": bool(workspace.config.library_auto_push),
            },
        )

    def _review(request: Request, name: str, current, new_tpl, origin: str, what: str) -> Any:
        from grayson.workflows.authoring import _dump, render_preview
        from grayson.workflows.lint import lint_template

        return _review_page(
            request,
            base_path="/workflows",
            file_dir="workflows",
            name=name,
            before=_workflow_text(name, current),
            after=_dump(new_tpl),
            preview=render_preview(new_tpl, workspace.findings_schemas_dir),
            warnings=lint_template(new_tpl),
            origin=origin,
            what=what,
        )

    @app.post("/workflows/{name}/edit")
    async def workflow_save(request: Request, name: str) -> Any:
        """The YAML editor's submit. `action` is review (validate and show the
        confirmation step), confirm (write what was reviewed), or back (return
        to the editor with the draft intact)."""
        _check(request)
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push
        from grayson.workflows.authoring import (
            WorkflowAuthoringError,
            save_workflow_yaml,
            validate_workflow_text,
        )

        form = await request.form()
        text = str(form.get("yaml", ""))
        action = str(form.get("action", "review"))
        if action == "back":
            return templates.TemplateResponse(
                request, "workflow_edit.html", _workflow_edit_context(name, text)
            )
        try:
            if action == "confirm":
                save_workflow_yaml(workspace.workflows_dir, name, text, get_user_id())
            else:
                new_tpl = validate_workflow_text(workspace.workflows_dir, name, text, get_user_id())
        except WorkflowAuthoringError as e:
            return templates.TemplateResponse(
                request,
                "workflow_edit.html",
                _workflow_edit_context(name, text, error=str(e)),
                status_code=400,
            )
        if action != "confirm":
            current = _workflow_or_404(name)
            return _review(request, name, current, new_tpl, "yaml", "the YAML you edited")
        maybe_auto_push(workspace, f"grayson workflows: edit {name}")
        return _redirect(f"/workflows/{name}")

    @app.post("/workflows/{name}/element")
    async def workflow_element(request: Request, name: str) -> Any:
        """One element edit from the workflow page (header, a setup input, a
        checkpoint, a findings field): applied to the template, then shown
        for confirmation like any other edit."""
        _check(request)
        from grayson.workflows.authoring import WorkflowAuthoringError, apply_element_edit

        tpl = _editable_or_403(name)
        form = await request.form()
        op = {k: str(v) for k, v in form.items() if k != "t"}
        if op.get("action", "upsert") == "upsert" and op.get("kind") in ("input", "field"):
            # unchecked boxes are absent from a form post, not false
            op["required"] = "required" in form
            op["adds_scope"] = "adds_scope" in form
        try:
            new_tpl = apply_element_edit(tpl, op, workspace.findings_schemas_dir)
        except WorkflowAuthoringError as e:
            return templates.TemplateResponse(
                request,
                "workflow_detail.html",
                _detail_context(tpl, error=str(e)),
                status_code=400,
            )
        what = {
            "meta": "the header",
            "input": f"setup input '{op.get('key') or op.get('orig_key') or ''}'",
            "check": f"checkpoint '{op.get('key') or op.get('orig_key') or ''}'",
            "field": f"findings field '{op.get('key') or op.get('orig_key') or ''}'",
        }.get(op.get("kind", ""), "an element")
        verb = {"delete": "removing", "move": "moving"}.get(op.get("action", ""), "editing")
        return _review(request, name, tpl, new_tpl, "element", f"{verb} {what}")

    @app.post("/workflows/{name}/delete")
    async def workflow_delete(request: Request, name: str) -> Any:
        """Remove a library workflow. The form repeats the name as a typed
        confirmation; ownership and open sessions are checked server-side."""
        _check(request)
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push
        from grayson.workflows import WorkflowNotFound, get_workflow
        from grayson.workflows.authoring import (
            WorkflowAuthoringError,
            delete_workflow,
            open_sessions_on,
        )

        form = await request.form()
        typed = str(form.get("confirm_name", "")).strip()
        try:
            if typed != name:
                raise WorkflowAuthoringError(
                    f"type the workflow's name ('{name}') to confirm its deletion"
                )
            delete_workflow(
                workspace.workflows_dir, name, get_user_id(), open_sessions_on(workspace, name)
            )
        except WorkflowAuthoringError as e:
            try:
                tpl = get_workflow(name, workspace.workflows_dir)
            except WorkflowNotFound:
                tpl = None  # a broken file: the gallery is where it shows
            if tpl is None:
                return templates.TemplateResponse(
                    request, "workflows.html", _workflows_context(error=str(e)), status_code=400
                )
            return templates.TemplateResponse(
                request, "workflow_detail.html", _detail_context(tpl, error=str(e)), status_code=400
            )
        maybe_auto_push(workspace, f"grayson workflows: delete {name}")
        return _redirect("/workflows")

    @app.post("/workflows/{name}/fork")
    async def workflow_fork(request: Request, name: str) -> Any:
        _check(request)
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push
        from grayson.workflows import WorkflowNotFound
        from grayson.workflows.authoring import WorkflowAuthoringError, create_workflow

        form = await request.form()
        new_name = str(form.get("new_name", "")).strip()
        tpl = _workflow_or_404(name)
        try:
            create_workflow(workspace.workflows_dir, new_name, fork_of=name, user_id=get_user_id())
        except (WorkflowAuthoringError, WorkflowNotFound) as e:
            return templates.TemplateResponse(
                request,
                "workflow_detail.html",
                _detail_context(tpl, error=str(e.args[0] if e.args else e)),
                status_code=400,
            )
        maybe_auto_push(workspace, f"grayson workflows: fork {name} -> {new_name}")
        return _redirect(f"/workflows/{new_name}")

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
        return _redirect(f"/workflows/{new_name}")

    @app.post("/workflows/{name}/promote")
    async def workflow_promote(request: Request, name: str) -> Any:
        """Lift a workflow's own findings fields into a shared library schema
        and point the workflow at it — reviewed first like any edit: the
        workflow's diff and the schema file that would be created."""
        _check(request)
        from grayson.findings.authoring import dump_schema, lint_schema, render_schema_preview
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push
        from grayson.workflows.authoring import (
            WorkflowAuthoringError,
            _dump,
            plan_promotion,
            promote_fields,
        )

        tpl = _editable_or_403(name)
        form = await request.form()
        schema_name = str(form.get("schema_name", "")).strip()
        title = str(form.get("title", "")).strip()
        description = str(form.get("description", "")).strip()
        action = str(form.get("action", "review"))
        try:
            if action == "confirm":
                promote_fields(
                    workspace.workflows_dir, name, schema_name, get_user_id(), title, description
                )
            else:
                schema, repointed = plan_promotion(
                    workspace.workflows_dir, name, schema_name, get_user_id(), title, description
                )
        except WorkflowAuthoringError as e:
            return templates.TemplateResponse(
                request,
                "workflow_detail.html",
                _detail_context(tpl, error=str(e)),
                status_code=400,
            )
        if action == "confirm":
            maybe_auto_push(workspace, f"grayson schemas: promote {name} fields -> {schema_name}")
            return _redirect(f"/schemas/{schema_name}")
        return _review_page(
            request,
            base_path="/workflows",
            file_dir="workflows",
            name=name,
            before=_workflow_text(name, tpl),
            after=_dump(repointed),
            preview=render_schema_preview(schema, None, [name]),
            warnings=lint_schema(schema),
            origin="element",
            what=f"promoting its findings fields to the shared schema '{schema_name}'",
            extra_files=[(f"findings_schemas/{schema_name}.yaml", dump_schema(schema))],
            confirm_path=f"/workflows/{name}/promote",
            hidden={"schema_name": schema_name, "title": title, "description": description},
        )

    # -- findings schemas ---------------------------------------------------

    def _schemas_context(error: str | None = None) -> dict:
        from grayson.findings.library import list_library_schemas, schema_problems
        from grayson.findings.schemas import FINDINGS_SCHEMAS
        from grayson.identity import get_user_id

        rows = _schema_catalog()
        for r in rows:
            tags = ["builtin" if not r["library"] else "library"]
            if r["mine"]:
                tags.append("mine")
            if r["forked_from"]:
                tags.append("fork")
            if r["discriminator"]:
                tags.append("branches")
            if not r["used_by"]:
                tags.append("unused")
            r["tags"] = tags
        return {
            "nav": "workflows",
            "rows": rows,
            "problems": schema_problems(workspace.findings_schemas_dir),
            "bases": sorted(FINDINGS_SCHEMAS),
            "fork_bases": [sc.name for sc in list_library_schemas(workspace.findings_schemas_dir)],
            "user_id": get_user_id(),
            "auto_push": bool(workspace.config.library_auto_push),
            "error": error,
        }

    @app.get("/schemas", response_class=HTMLResponse)
    def schemas_page(request: Request) -> Any:
        _check(request)
        return templates.TemplateResponse(request, "schemas.html", _schemas_context())

    def _schema_or_404(name: str):
        """A library schema (model), or None for a built-in; 404 otherwise."""
        from grayson.findings.library import SchemaNotFound, core_schema_names, get_library_schema

        if name in core_schema_names():
            return None
        try:
            return get_library_schema(name, workspace.findings_schemas_dir)
        except SchemaNotFound as e:
            raise HTTPException(status_code=404, detail=str(e.args[0] if e.args else e)) from e

    def _schema_detail_context(name: str, error: str | None = None) -> dict:
        from grayson.findings.authoring import can_edit_schema, lint_schema, workflows_using
        from grayson.findings.library import core_schema_names
        from grayson.findings.schemas import FINDINGS_SCHEMAS, describe_schema
        from grayson.identity import get_user_id

        schema = _schema_or_404(name)
        user_id = get_user_id()
        return {
            "nav": "workflows",
            "name": name,
            "schema": schema,
            "spec": describe_schema(name, None, workspace.findings_schemas_dir),
            "is_core": name in core_schema_names(),
            "editable": schema is not None and can_edit_schema(schema, user_id),
            "mine": schema is not None and bool(user_id) and schema.created_by == user_id,
            "used_by": workflows_using(name, workspace.workflows_dir),
            "warnings": lint_schema(schema) if schema is not None else [],
            "bases": sorted(FINDINGS_SCHEMAS),
            "fork_name": f"{name}_{user_id}" if user_id else f"{name}_fork",
            "user_id": user_id,
            "auto_push": bool(workspace.config.library_auto_push),
            "error": error,
        }

    @app.get("/schemas/{name}", response_class=HTMLResponse)
    def schema_detail(request: Request, name: str) -> Any:
        _check(request)
        return templates.TemplateResponse(
            request, "schema_detail.html", _schema_detail_context(name)
        )

    def _schema_text(name: str) -> str:
        from grayson.findings.authoring import dump_schema

        path = workspace.findings_schemas_dir / f"{name}.yaml"
        if path.is_file():
            return path.read_text(encoding="utf-8")
        schema = _schema_or_404(name)
        if schema is None:
            raise HTTPException(status_code=404, detail="built-in schemas have no YAML file")
        return dump_schema(schema)

    @app.get("/schemas/{name}/yaml", response_class=HTMLResponse)
    def schema_yaml(request: Request, name: str, raw: str = "") -> Any:
        _check(request)
        from grayson.findings.authoring import can_edit_schema
        from grayson.identity import get_user_id

        schema = _schema_or_404(name)
        text = _schema_text(name)
        if raw:
            return Response(
                text,
                media_type="text/plain; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{name}.yaml"'},
            )
        return templates.TemplateResponse(
            request,
            "workflow_yaml.html",
            {
                "nav": "workflows",
                "noun": "schema",
                "base_path": "/schemas",
                "name": name,
                "text": text,
                "is_core": False,
                "editable": schema is not None and can_edit_schema(schema, get_user_id()),
            },
        )

    def _schema_edit_context(name: str, text: str, error: str | None = None) -> dict:
        return {
            "nav": "workflows",
            "noun": "schema",
            "base_path": "/schemas",
            "name": name,
            "text": text,
            "error": error,
            "auto_push": bool(workspace.config.library_auto_push),
        }

    def _schema_editable_or_403(name: str):
        from grayson.findings.authoring import can_edit_schema
        from grayson.identity import get_user_id

        schema = _schema_or_404(name)
        if schema is None:
            raise HTTPException(
                status_code=403, detail="built-in schemas are canonical — extend one instead"
            )
        if not can_edit_schema(schema, get_user_id()):
            raise HTTPException(
                status_code=403,
                detail=f"'{name}' was created by '{schema.created_by}' — fork it instead",
            )
        return schema

    @app.get("/schemas/{name}/edit", response_class=HTMLResponse)
    def schema_edit(request: Request, name: str) -> Any:
        _check(request)
        _schema_editable_or_403(name)
        return templates.TemplateResponse(
            request, "workflow_edit.html", _schema_edit_context(name, _schema_text(name))
        )

    def _schema_review(request: Request, name: str, new_schema, origin: str, what: str) -> Any:
        from grayson.findings.authoring import (
            dump_schema,
            lint_schema,
            render_schema_preview,
            workflows_using,
        )

        return _review_page(
            request,
            base_path="/schemas",
            file_dir="findings_schemas",
            name=name,
            before=_schema_text(name),
            after=dump_schema(new_schema),
            preview=render_schema_preview(
                new_schema, None, workflows_using(name, workspace.workflows_dir)
            ),
            warnings=lint_schema(new_schema),
            origin=origin,
            what=what,
        )

    @app.post("/schemas/{name}/edit")
    async def schema_save(request: Request, name: str) -> Any:
        _check(request)
        from grayson.findings.authoring import (
            SchemaAuthoringError,
            save_schema_yaml,
            validate_schema_text,
        )
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push

        form = await request.form()
        text = str(form.get("yaml", ""))
        action = str(form.get("action", "review"))
        if action == "back":
            return templates.TemplateResponse(
                request, "workflow_edit.html", _schema_edit_context(name, text)
            )
        try:
            if action == "confirm":
                save_schema_yaml(workspace.findings_schemas_dir, name, text, get_user_id())
            else:
                new_schema = validate_schema_text(
                    workspace.findings_schemas_dir, name, text, get_user_id()
                )
        except SchemaAuthoringError as e:
            return templates.TemplateResponse(
                request,
                "workflow_edit.html",
                _schema_edit_context(name, text, error=str(e)),
                status_code=400,
            )
        if action != "confirm":
            return _schema_review(request, name, new_schema, "yaml", "the YAML you edited")
        maybe_auto_push(workspace, f"grayson schemas: edit {name}")
        return _redirect(f"/schemas/{name}")

    @app.post("/schemas/{name}/element")
    async def schema_element(request: Request, name: str) -> Any:
        _check(request)
        from grayson.findings.authoring import SchemaAuthoringError, apply_schema_edit

        schema = _schema_editable_or_403(name)
        form = await request.form()
        op = {k: str(v) for k, v in form.items() if k != "t"}
        if op.get("action", "upsert") == "upsert" and op.get("kind") == "field":
            op["required"] = "required" in form
        try:
            new_schema = apply_schema_edit(schema, op)
        except SchemaAuthoringError as e:
            return templates.TemplateResponse(
                request,
                "schema_detail.html",
                _schema_detail_context(name, error=str(e)),
                status_code=400,
            )
        key = op.get("key") or op.get("orig_key") or ""
        what = {
            "meta": "the header",
            "field": f"{'branch ' + op['branch'] + ' ' if op.get('branch') else ''}field '{key}'",
            "discriminator": f"the discriminator ('{key}')"
            if key
            else "the discriminator (clearing it)",
        }.get(op.get("kind", ""), "an element")
        verb = {"delete": "removing", "move": "moving"}.get(op.get("action", ""), "editing")
        return _schema_review(request, name, new_schema, "element", f"{verb} {what}")

    @app.post("/schemas/{name}/delete")
    async def schema_delete(request: Request, name: str) -> Any:
        _check(request)
        from grayson.findings.authoring import (
            SchemaAuthoringError,
            delete_schema,
            workflows_using,
        )
        from grayson.findings.library import SchemaNotFound, get_library_schema
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push

        form = await request.form()
        typed = str(form.get("confirm_name", "")).strip()
        try:
            if typed != name:
                raise SchemaAuthoringError(
                    f"type the schema's name ('{name}') to confirm its deletion"
                )
            delete_schema(
                workspace.findings_schemas_dir,
                name,
                get_user_id(),
                workflows_using(name, workspace.workflows_dir),
            )
        except SchemaAuthoringError as e:
            try:
                get_library_schema(name, workspace.findings_schemas_dir)
            except SchemaNotFound:
                return templates.TemplateResponse(
                    request, "schemas.html", _schemas_context(error=str(e)), status_code=400
                )
            return templates.TemplateResponse(
                request,
                "schema_detail.html",
                _schema_detail_context(name, error=str(e)),
                status_code=400,
            )
        maybe_auto_push(workspace, f"grayson schemas: delete {name}")
        return _redirect("/schemas")

    @app.post("/schemas/{name}/fork")
    async def schema_fork(request: Request, name: str) -> Any:
        _check(request)
        from grayson.findings.authoring import SchemaAuthoringError, create_schema
        from grayson.findings.library import SchemaNotFound, core_schema_names
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push

        form = await request.form()
        new_name = str(form.get("new_name", "")).strip()
        _schema_or_404(name)
        try:
            if name in core_schema_names():
                create_schema(
                    workspace.findings_schemas_dir, new_name, base=name, user_id=get_user_id()
                )
            else:
                create_schema(
                    workspace.findings_schemas_dir, new_name, fork_of=name, user_id=get_user_id()
                )
        except (SchemaAuthoringError, SchemaNotFound) as e:
            return templates.TemplateResponse(
                request,
                "schema_detail.html",
                _schema_detail_context(name, error=str(e.args[0] if e.args else e)),
                status_code=400,
            )
        maybe_auto_push(workspace, f"grayson schemas: fork {name} -> {new_name}")
        return _redirect(f"/schemas/{new_name}")

    @app.post("/schemas/new")
    async def schema_create(request: Request) -> Any:
        _check(request)
        from grayson.findings.authoring import SchemaAuthoringError, create_schema
        from grayson.findings.library import SchemaNotFound
        from grayson.identity import get_user_id
        from grayson.library import maybe_auto_push

        form = await request.form()
        new_name = str(form.get("new_name", "")).strip()
        base = str(form.get("base", "")).strip() or None
        fork_of = str(form.get("fork_of", "")).strip() or None
        try:
            create_schema(
                workspace.findings_schemas_dir,
                new_name,
                base=None if fork_of else base,
                fork_of=fork_of,
                user_id=get_user_id(),
            )
        except (SchemaAuthoringError, SchemaNotFound) as e:
            return templates.TemplateResponse(
                request,
                "schemas.html",
                _schemas_context(error=str(e.args[0] if e.args else e)),
                status_code=400,
            )
        maybe_auto_push(
            workspace,
            f"grayson schemas: new {new_name}" + (f" (fork of {fork_of})" if fork_of else ""),
        )
        return _redirect(f"/schemas/{new_name}")

    # -- settings ---------------------------------------------------------

    def _settings_context(error: str | None = None) -> dict:
        from grayson.config_edit import config_summary
        from grayson.library import (
            effective_policy,
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
            "policy": effective_policy(workspace).summary(),
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

    @app.get("/records/{sid}/charts/{name}")
    def record_chart(request: Request, sid: str, name: str) -> Any:
        """A chart file published beside a session's report (records/<sid>/charts/),
        so the record page of a teammate's session shows the pictures the
        report embeds. Declared before the generic record route, which would
        otherwise read `charts` as a record kind."""
        from grayson.charts.spec import CHART_ID_RE
        from grayson.util import ensure_within

        _check(request)
        if not (name.endswith(".svg") and CHART_ID_RE.match(name[:-4])):
            raise HTTPException(status_code=404, detail="no such chart file")
        try:
            path = ensure_within(
                workspace.records_dir, workspace.records_dir / sid / "charts" / name
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail="no such chart file") from e
        if not path.is_file():
            raise HTTPException(status_code=404, detail="no such chart file")
        return Response(path.read_text(encoding="utf-8"), media_type="image/svg+xml")

    def _published_chart_files(sid: str) -> list[str]:
        folder = workspace.records_dir / sid / "charts"
        return sorted(p.name for p in folder.glob("c_*.svg")) if folder.is_dir() else []

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
                "chart_files": _published_chart_files(sid) if kind == "report" else [],
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
            "checkpoints": engine.checkpoints_view(s, workspace.workflows_dir),
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
        if profile:
            try:
                settings = workspace.config.resolve_profile(profile)
            except KeyError as e:
                return templates.TemplateResponse(
                    request, "session.html", _session_context(s, str(e.args[0])), status_code=400
                )
            # Re-applying the session's own profile name is not a no-op: its
            # numbers may have been edited in Settings since the session
            # snapshotted them. Compare the resolved settings, not the label.
            if profile != current["guard_profile"] or settings.model_dump() != current["guard"]:
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
