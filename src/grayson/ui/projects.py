"""Human project review, monitoring, and control surfaces."""

import json

import yaml
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool


def register(app, workspace, templates, check, session, redirect, session_context):
    from grayson.core.engine import workflow_for
    from grayson.projects import engine

    @app.get("/session/{sid}/project")
    def project_page(request: Request, sid: str):
        check(request)
        s = session(sid)
        if workflow_for(s, workspace.workflows_dir).project is None:
            raise HTTPException(400, "This is a standard QA session, not a project workflow")
        return redirect(f"/session/{sid}")

    @app.get("/session/{sid}/project/report")
    def project_report(request: Request, sid: str):
        check(request)
        from fastapi.responses import Response

        data = engine.status(session(sid))
        return Response(
            json.dumps(data, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="project-evidence.json"'},
        )

    @app.post("/session/{sid}/project/{action}")
    async def project_action(request: Request, sid: str, action: str):
        check(request)
        s = session(sid)
        form = await request.form()
        try:
            revision = int(form.get("revision", "0"))
            if action == "draft":
                engine.draft(s, yaml.safe_load(str(form.get("spec", ""))), revision)
            elif action in {"approve", "approve-candidate"}:
                fn = engine.approve if action == "approve" else engine.approve_candidate
                fn(s, revision, str(form.get("digest", "")), "user")
            elif action == "finish":
                engine.finish(s, revision, "user")
            elif action == "approval":
                engine.set_approval(s, str(form.get("level", "")), revision, "user")
            elif action == "accept-deployment":
                engine.accept_deployment(s, revision, "user")
            elif action in {"pause", "resume", "cancel"}:
                engine.control(s, action, str(form.get("reason", "")), revision, "user")
            elif action == "deployment":
                engine.deployment_package(s, revision)
            elif action == "verify":
                await run_in_threadpool(engine.verify, s, revision)
            elif action == "deployment-check":
                await run_in_threadpool(engine.deployment_check, s, revision)
            else:
                raise ValueError("unknown project action")
        except (ValueError, OSError, KeyError, yaml.YAMLError) as e:
            return templates.TemplateResponse(
                request, "project.html", session_context(s, str(e)), status_code=400
            )
        return redirect(f"/session/{sid}")
