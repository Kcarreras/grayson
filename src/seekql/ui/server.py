"""Local web console: FastAPI + server-rendered Jinja2, loopback only.

The console is the user's window into sessions — interventions, checkpoints,
findings, query log, proposals. Agents never touch it. It binds to 127.0.0.1
and gates every request on a per-launch token, carries no external assets, and
only ever mutates state through the same core APIs the CLI uses.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from seekql.core import engine
from seekql.core.engine import EnforcementError
from seekql.core.session import Session
from seekql.interventions import validate_response
from seekql.interventions.types import InterventionError
from seekql.workspace import Workspace

TEMPLATES_DIR = Path(__file__).parent / "templates"
_COOKIE = "seekql_token"


def build_app(workspace: Workspace, token: str | None = None) -> FastAPI:
    app = FastAPI(title="seekql console", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["token"] = token or ""

    def _valid(supplied: str | None) -> bool:
        return bool(supplied) and secrets.compare_digest(supplied, token)

    def _check(request: Request) -> None:
        # Host allowlist defeats DNS-rebinding (a malicious hostname resolving to
        # 127.0.0.1): the browser sends that hostname in Host, which we reject.
        host = (request.headers.get("host") or "").split(":")[0]
        if host and host not in {"127.0.0.1", "localhost", "::1", ""}:
            raise HTTPException(status_code=403, detail="unexpected Host header")
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
                    "open_checks": len(ready["open_checks"]),
                    "findings": ready["findings_total"],
                }
            )
        sessions.sort(key=lambda x: x["summary"]["created_at"] or "", reverse=True)
        response = templates.TemplateResponse(request, "dashboard.html", {"sessions": sessions})
        _set_cookie(response)  # subsequent navigation authenticates via cookie, not URL
        return response

    def _session_context(s: Session, error: str | None = None) -> dict:
        return {
            "s": s.summary(),
            "readiness": engine.readiness(s, workspace.workflows_dir),
            "checkpoints": s.checkpoints(),
            "findings": s.findings(),
            "interventions": s.interventions(),
            "proposals": s.proposals(),
            "queries": s.query_log(100),
            "events": s.events(40),
            "error": error,
        }

    @app.get("/session/{sid}", response_class=HTMLResponse)
    def session_detail(request: Request, sid: str) -> Any:
        _check(request)
        return templates.TemplateResponse(request, "session.html", _session_context(_session(sid)))

    @app.get("/session/{sid}/intervention/{iid}", response_class=HTMLResponse)
    def intervention_detail(request: Request, sid: str, iid: str) -> Any:
        _check(request)
        s = _session(sid)
        item = s.intervention(iid)
        if item is None:
            raise HTTPException(status_code=404, detail=f"no intervention '{iid}'")
        return templates.TemplateResponse(request, "intervention.html", {"sid": sid, "item": item})

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
                {"sid": sid, "item": item, "error": str(e.args[0] if e.args else e)},
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
        from seekql.core import proposals as proposals_engine
        from seekql.core.proposals import ProposalError

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
    workspace: Workspace, host: str = "127.0.0.1", port: int = 8765, use_token: bool = True
) -> None:
    import uvicorn

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("refusing to bind the console to a non-loopback host")
    token = secrets.token_urlsafe(16) if use_token else None
    app = build_app(workspace, token)
    url = f"http://{host}:{port}/"
    if token:
        url += f"?t={token}"
    print(f"seekql console: {url}")
    print("(loopback only; keep this token private — it grants access to this workspace)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
