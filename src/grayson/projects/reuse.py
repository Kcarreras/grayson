"""Reuse verified methods without turning historical results into current facts."""

import copy
import re
import secrets
import time

from grayson.projects import engine
from grayson.util import utcnow


def revalidate(session, request_id, revision, executor=None):
    """Consume one explicitly approved replay, producing a separate audit session.

    Replays cannot change candidates, contracts or permissions. request_id makes
    retries idempotent, and the parent reserves its finite allowance transactionally.
    """
    from grayson.core.session import Session

    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request_id):
        raise ValueError("request_id must be 1-80 letters, digits, underscores or hyphens")
    source = engine.state(session)
    existing = source.get("revalidations", {}).get(request_id) if source else None
    if existing and existing.get("session"):
        return engine.status(Session(session.workspace, existing["session"]))
    token = secrets.token_hex(16)

    def reserve(s):
        if not s or s["phase"] != "complete" or s.get("revalidation_of") or s.get("replay_only"):
            raise ValueError("only an original completed project can authorise revalidation")
        runs = s.setdefault("revalidations", {})
        prior = runs.get(request_id)
        if prior and (prior.get("session") or prior.get("expires", 0) > time.time()):
            raise ValueError("revalidation creation is in progress; retry the same request later")
        if not prior and len(runs) >= s["contract"]["policy"]["max_revalidations"]:
            raise ValueError(
                "no preapproved revalidations remain; request a new human-approved project"
            )
        runs[request_id] = {
            "reserved_at": utcnow(),
            "session": None,
            "token": token,
            "expires": time.time() + 60,
        }
        return s

    engine._mutate(
        session,
        source["revision"] if existing else revision,
        "revalidation_reserved",
        reserve,
        "system",
    )
    try:
        child = _create_revalidation(session, request_id, token)
    except Exception:
        # Ordinary failures release the creation lease immediately. A process exit
        # leaves a short lease that the same request can reclaim on retry.
        def release(s):
            entry = s["revalidations"].get(request_id, {})
            if entry.get("token") == token and not entry.get("session"):
                entry.update(token=None, expires=0)
            return s

        engine._mutate(
            session, engine.state(session)["revision"], "revalidation_released", release, "system"
        )
        raise
    result = engine.verify(child, engine.state(child)["revision"], executor)["project"]

    def conclude(s):
        if s["verification"]["verdict"] == "pass":
            s.update(
                phase="complete",
                completion={
                    "by": "system",
                    "at": utcnow(),
                    "label": "machine revalidated under prior human grant; no new human acceptance",
                },
            )
        else:
            s.update(
                phase="blocked", block_reason="Previously verified SQL failed fresh revalidation"
            )
        return s

    return engine._mutate(child, result["revision"], "revalidation_finished", conclude, "system")


def _create_revalidation(session, request_id, token):
    from grayson.core.session import Session

    source = engine.state(session)
    deployed = source.get("deployed_verification")
    report = deployed or source["verification"]
    scope = source["contract"]["scope"]
    if deployed:
        scope = source["deployment"]["verification_scope"]
    child = Session.create(
        session.workspace,
        workflow=session.workflow,
        targets=scope,
        guard=session.guard_settings,
        guard_profile=session.summary()["guard_profile"],
        strict_scope=True,
        connection=source["connection"],
        title="Revalidate: " + session.id,
        actor="system",
    )
    child.set_meta("workflow_snapshot_v1", session.get_meta("workflow_snapshot_v1"))
    cloned = copy.deepcopy(source)
    cloned.update(
        phase="verifying",
        active_seconds=0,
        active_since=time.time(),
        query_start=0,
        iterations=1,
        stalled=0,
        history=[],
        plan=[],
        lease={},
        runner={},
        revalidation_of=session.id,
        replay_only=True,
        revalidations={},
        verification_sessions=[],
        verification=None,
        last_verification=None,
        review=None,
        deployment=None,
        deployed_verification=None,
        created_at=utcnow(),
        started=time.time(),
        approved_by="inherited human revalidation grant",
    )
    cloned.pop("completion", None)
    cloned["contract"]["scope"] = sorted(scope)
    cloned["contract_digest"] = engine.digest(
        {
            "contract": cloned["contract"],
            "original_approval": source["approved_digest"],
            "request_id": request_id,
        }
    )
    cloned["approved_digest"] = cloned["contract_digest"]
    cloned["verification_queries"] = [
        {k: r[k] for k in ("id", "name", "sql", "expectation", "repair")} for r in report["results"]
    ]
    engine._mutate(child, 0, "revalidation_created", lambda _: cloned, "system")
    current = engine.state(session)

    def attach(s):
        entry = s["revalidations"].get(request_id, {})
        if entry.get("token") != token or entry.get("session"):
            raise ValueError("revalidation reservation changed; retry the original request")
        entry["session"] = child.id
        entry.pop("token", None)
        entry.pop("expires", None)
        return s

    engine._mutate(session, current["revision"], "revalidation_attached", attach, "system")
    return child


def propose_regression(session, criterion_id, check_id):
    from grayson.checks.regression import propose_check
    from grayson.core.session import Session

    s = engine.state(session)
    report = (s or {}).get("deployed_verification") or (s or {}).get("verification")
    if not report or report["verdict"] != "pass":
        raise ValueError("a passed project verification is required")
    if time.time() - report["started"] > s["contract"]["policy"]["evidence_minutes"] * 60:
        raise ValueError("rerun stale verification before promoting a check")
    item = next((r for r in report["results"] if r["id"] == criterion_id), None)
    if not item:
        raise ValueError("unknown verification check")
    source = Session(session.workspace, item["session_id"]) if item.get("session_id") else session
    return propose_check(
        source,
        item["qid"],
        check_id,
        item["name"],
        "Project acceptance check: " + item["repair"],
        item["expectation"],
    )


def recipe(session, name):
    """Return a workflow draft for the existing author/lint/preview/save flow."""
    import yaml

    from grayson.workflows.authoring import render_preview
    from grayson.workflows.models import WorkflowTemplate

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
        raise ValueError("use a lowercase workflow name")
    s = engine.state(session)
    if not s or not s.get("verification") or s["verification"]["verdict"] != "pass":
        raise ValueError("a verified candidate is required to propose a recipe")
    raw = copy.deepcopy(s["workflow"])
    raw.update(
        name=name,
        forked_from=session.workflow,
        created_by="",
        title="Project recipe: " + s["contract"]["goal"][:100],
        project=s["contract"]["policy"],
        project_example={
            "source_session": session.id,
            "brief": s["contract"],
            "candidate": s["candidate"],
            "notice": "Historical example only. Re-interview and approve each new brief.",
        },
    )
    tpl = WorkflowTemplate.model_validate(raw)
    return {
        "yaml": yaml.safe_dump(tpl.model_dump(mode="json"), sort_keys=False),
        "preview": render_preview(tpl),
        "saved": False,
        "next_action": "Review and save through workflow authoring; no permissions transfer",
    }
