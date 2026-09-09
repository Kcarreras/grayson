"""Durable project state machine shared by CLI, MCP, console and runners.

Mutations use SQLite transactions and compare-and-swap revisions. Evidence is
bound to contract, candidate, connection and scope; verdicts are computed, never
accepted from a model. No function in this module executes warehouse writes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import time

from grayson.checks.regression import Expectation, evaluate
from grayson.core.session import Session
from grayson.projects import policy
from grayson.projects.models import Candidate, Contract, PlanStep, Review
from grayson.projects.sql import (
    baseline,
    compile_candidate,
    deployment_target,
    verification_queries,
)
from grayson.util import utcnow

KEY = "project_v1"
TERMINAL = {"complete", "cancelled"}
ACTIVE_PHASES = {"building", "verifying", "needs_revision", "needs_review"}


def elapsed(s):
    return s.get("active_seconds", 0) + (
        max(0, time.time() - s["active_since"]) if s.get("active_since") else 0
    )


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def state(session: Session) -> dict | None:
    raw = session.get_meta(KEY)
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("format") != 1:
        raise ValueError("unsupported project state; upgrade Grayson before continuing")
    return value


def _candidate_phase(s, level):
    """Reconcile pending SQL with current gates without interrupting a verifier."""
    phase = s["phase"]
    if not s.get("candidate") or s.get("lease", {}).get("expires", 0) > time.time():
        return phase
    if phase == "candidate_review" and level != "guided":
        return "verifying"
    if (
        phase == "verifying"
        and not s.get("verification")
        and level == "guided"
        and s.get("candidate_approved") != s["candidate_digest"]
    ):
        return "candidate_review"
    return phase


def _mutate(session, revision, event, change, actor="agent"):
    con = session._con()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
        current = json.loads(row[0]) if row else None
        if (current or {}).get("revision", 0) != revision:
            raise ValueError("project changed; read project_status and retry against its revision")
        if current and current.get("format") != 1:
            raise ValueError("unsupported project format")
        updated = change(copy.deepcopy(current))
        if updated["phase"] in {"candidate_review", "verifying"}:
            level = policy.effective(
                session,
                updated["contract"]["policy"]["approval"],
                override=updated.get("approval_override")
                or updated["contract"]["policy"]["approval"],
            )["approval"]
            updated["phase"] = _candidate_phase(updated, level)
        updated["active_seconds"] = elapsed(current) if current else 0
        updated["active_since"] = time.time() if updated["phase"] in ACTIVE_PHASES else None
        proposal = updated.pop("_new_proposal", None)
        if proposal:
            n = con.execute("SELECT COALESCE(MAX(rowid),0) FROM proposals").fetchone()[0]
            pid = f"p_{n + 1:03d}"
            con.execute(
                "INSERT INTO proposals(pid,ts,worker,kind,status,title,payload) "
                "VALUES(?,?,?,'ddl_snippet','proposed',?,?)",
                (pid, utcnow(), actor, "Deploy project candidate", json.dumps(proposal)),
            )
            updated["deployment"]["pid"] = pid
        if updated["phase"] in TERMINAL:
            if updated["phase"] == "cancelled":
                pending = con.execute(
                    "SELECT iid FROM interventions WHERE status='open'"
                ).fetchall()
                con.execute("UPDATE interventions SET status='cancelled' WHERE status='open'")
                con.executemany(
                    "INSERT INTO events(ts,actor,type,payload) VALUES(?,?,?,?)",
                    [
                        (utcnow(), actor, "intervention_cancelled", json.dumps({"iid": row[0]}))
                        for row in pending
                    ],
                )
            outcome = "project_verified" if updated["phase"] == "complete" else "abandoned"
            con.executemany(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE "
                "SET value=excluded.value",
                [
                    ("stage", "closed"),
                    ("outcome", outcome),
                    ("outcome_note", updated.get("completion", {}).get("label", "Cancelled")),
                ],
            )
        updated["revision"] = revision + 1
        updated["updated_at"] = utcnow()
        con.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE "
            "SET value=excluded.value",
            (KEY, json.dumps(updated)),
        )
        con.execute(
            "INSERT INTO events(ts,actor,type,payload) VALUES(?,?,?,?)",
            (
                utcnow(),
                actor,
                "project_" + event,
                json.dumps({"revision": revision + 1, "state": updated}),
            ),
        )
        con.commit()
    finally:
        con.close()
    return status(session)


def _human(actor):
    if actor != "user":
        raise ValueError("this is a human project decision; use the console or interactive CLI")


def _editable(s):
    if s is None or s["phase"] in TERMINAL:
        raise ValueError("project is missing or finished")
    if s.get("lease", {}).get("expires", 0) > time.time():
        raise ValueError("verification is in progress; pause or cancel it before editing")


def _approved(session, s):
    if session.stage == "closed":
        raise ValueError("session is closed")
    if not session.strict_scope:
        raise ValueError("project strict scope was disabled; restore it before continuing")
    if not s or s.get("approved_digest") != s["contract_digest"]:
        raise ValueError("the project brief needs human approval")
    if (
        session.connection != s["connection"]
        or sorted(session.scope_tables) != s["contract"]["scope"]
    ):
        raise ValueError("connection or scope changed; draft and approve a revised brief")
    if s["phase"] in TERMINAL or s["phase"] in {"paused", "blocked"}:
        raise ValueError(f"project is {s['phase']}; a human must resume or revise the brief")
    if s.get("replay_only"):
        parent = state(Session(session.workspace, s["revalidation_of"]))
        if not any(
            r.get("session") == session.id for r in parent.get("revalidations", {}).values()
        ):
            raise ValueError("revalidation creation is not attached to its approved grant")


def draft(session: Session, spec: dict, revision: int = 0) -> dict:
    from grayson.core.engine import workflow_for

    if session.stage == "closed":
        raise ValueError("cannot start a project on a closed session")
    tpl = workflow_for(session, session.workspace.workflows_dir)
    if tpl.project is None:
        raise ValueError("choose a project workflow; standard QA sessions retain their lifecycle")
    if isinstance(spec, dict):
        spec = dict(spec)
        spec.setdefault("kind", tpl.project.kind)
        if "policy" not in spec or isinstance(spec["policy"], dict):
            spec["policy"] = {**tpl.project.model_dump(), **spec.get("policy", {})}
    contract = Contract.model_validate(spec)
    if contract.kind != tpl.project.kind:
        raise ValueError("project kind must match the workflow")
    if not session.strict_scope or set(contract.scope) != session.scope_tables:
        raise ValueError("projects require strict scope matching the session's explicit tables")
    if contract.deployment_target:
        deployment_target(contract.deployment_target)
    for check in contract.checks:
        if check.baseline_sql:
            baseline(check.baseline_sql, set(contract.scope))
    policy.effective(session, contract.policy.approval)
    payload = contract.model_dump(mode="json")
    binding = digest(
        {
            "contract": payload,
            "connection": session.connection,
            "workflow": tpl.model_dump(mode="json"),
        }
    )

    def change(old):
        if old:
            if old.get("replay_only"):
                raise ValueError(
                    "revalidation is immutable; create a new project to change its brief"
                )
            _editable(old)
        return {
            "format": 1,
            "phase": "awaiting_brief",
            "contract": payload,
            "contract_digest": binding,
            "connection": session.connection,
            "workflow": tpl.model_dump(mode="json"),
            "approved_digest": "",
            "iterations": 0,
            "runner_steps": 0,
            "stalled": 0,
            "query_start": session.budget_consumed_count(),
            "candidate": None,
            "candidate_digest": "",
            "verification": None,
            "review": None,
            "plan": [],
            "history": [],
            "lease": {},
            "created_at": (old or {}).get("created_at", utcnow()),
            "started": None,
        }

    return _mutate(session, revision, "brief_drafted", change)


def approve(session, revision, expected_digest, actor="user"):
    _human(actor)

    def change(s):
        _editable(s)
        if s["contract_digest"] != expected_digest or s["phase"] != "awaiting_brief":
            raise ValueError("brief changed or is already approved; review the current version")
        if s["connection"] != session.connection or s["contract"]["scope"] != sorted(
            session.scope_tables
        ):
            raise ValueError("session scope/connection no longer matches the brief")
        s.update(
            approved_digest=expected_digest,
            phase="building",
            started=time.time(),
            query_start=session.budget_consumed_count(),
            approved_by=actor,
        )
        return s

    return _mutate(session, revision, "brief_approved", change, actor)


def submit_candidate(session, spec, revision):
    candidate = Candidate.model_validate(spec)

    def change(s):
        _editable(s)
        _approved(session, s)
        contract = Contract.model_validate(s["contract"])
        if s.get("replay_only"):
            raise ValueError("revalidation cannot change the candidate")
        if s["iterations"] >= contract.policy.max_iterations:
            raise ValueError("iteration budget exhausted; a human must revise the brief")
        if s["stalled"] >= contract.policy.max_stalled_iterations:
            raise ValueError("progress stalled; request a human decision")
        compiled = compile_candidate(candidate, contract)
        queries = verification_queries(candidate, contract)
        fingerprint = digest({"sql": compiled["sql"], "contract": s["contract_digest"]})
        # Comparing SQL also preserves evidence for older stored fingerprints
        # that included narrative fields. Do not rewrite those existing bindings.
        if compiled["sql"] == s.get("candidate_sql"):
            raise ValueError(
                "candidate SQL is unchanged; rerun verification or diagnose another approach"
            )
        if s.get("verification") and s["verification"]["verdict"] != "pass":
            failed = {r["id"] for r in s["verification"]["results"] if r["status"] != "pass"}
            if not failed.intersection(candidate.addressed_checks):
                raise ValueError("a repair must name a failed/unproven check it addresses")
        if s["candidate"]:
            s["history"].append(
                {
                    "digest": s["candidate_digest"],
                    "candidate": s["candidate"],
                    "verification": s["verification"],
                    "review": s["review"],
                }
            )
        s.update(
            candidate=candidate.model_dump(mode="json"),
            candidate_digest=fingerprint,
            candidate_sql=compiled["sql"],
            risks=compiled["risks"],
            verification_queries=queries,
            last_verification=s.get("verification") or s.get("last_verification"),
            verification=None,
            review=None,
            candidate_approved="",
            lease={},
            deployment=None,
            deployed_verification=None,
            iterations=s["iterations"] + 1,
            phase="candidate_review"
            if policy.effective(session, contract.policy.approval)["approval"] == "guided"
            else "verifying",
        )
        return s

    return _mutate(session, revision, "candidate_submitted", change)


def approve_candidate(session, revision, expected_digest, actor="user"):
    _human(actor)

    def change(s):
        _editable(s)
        _approved(session, s)
        if not s["candidate"] or s["candidate_digest"] != expected_digest:
            raise ValueError("candidate changed; review its current SQL")
        s.update(candidate_approved=expected_digest, phase="verifying")
        return s

    return _mutate(session, revision, "candidate_approved", change, actor)


def query_blocker(session, allocated=False) -> str | None:
    """Used by the common query path, including queries outside the runner."""
    parent = session.get_meta("project_verification_parent")
    if parent:
        return query_blocker(Session(session.workspace, parent), allocated)
    s = state(session)
    if not s:
        return None
    try:
        _approved(session, s)
    except ValueError as e:
        return str(e)
    p = Contract.model_validate(s["contract"]).policy
    if queries_used(session, s) - int(allocated) >= p.max_queries:
        return "project query budget exhausted"
    if elapsed(s) >= p.max_minutes * 60:
        return "project time budget exhausted"
    return None


def verify(session, revision, executor=None):
    from grayson.core.run import run_statement

    token = secrets.token_hex(16)

    def claim(s):
        _editable(s)
        _approved(session, s)
        if not s["candidate"]:
            raise ValueError("submit a candidate first")
        p = Contract.model_validate(s["contract"]).policy
        if (
            policy.effective(session, p.approval)["approval"] == "guided"
            and s.get("candidate_approved") != s["candidate_digest"]
        ):
            raise ValueError("guided mode requires human candidate approval")
        s["lease"] = {"token": token, "expires": time.time() + p.max_minutes * 60 + 300}
        s["phase"] = "verifying"
        s["review"] = None
        # Keep the latest completed attempt across reruns and interrupted runs.
        # Candidate history alone misses repeated verification of the same SQL.
        s["last_verification"] = (
            s.get("verification")
            or s.get("last_verification")
            or (s["history"][-1].get("verification") if s["history"] else None)
        )
        s["verification"] = None
        return s

    claimed = _mutate(session, revision, "verification_started", claim)
    s = claimed["project"]
    results = []
    started = time.time()
    queries = s["verification_queries"]
    try:
        for check in queries:
            current = state(session)
            if current.get("lease", {}).get("token") != token or current["phase"] != "verifying":
                break
            try:
                output = run_statement(
                    session, check["sql"], label="project: " + check["name"], executor=executor
                )
                if output["status"] != "executed" or output.get("cache_error"):
                    raise ValueError(
                        output.get("reason")
                        or output.get("error")
                        or output.get("cache_error")
                        or output["status"]
                    )
                outcome = evaluate(
                    session, output["qid"], Expectation.model_validate(check["expectation"])
                )
            except (ValueError, OSError) as e:
                outcome = {"status": "unproven", "observed": None, "details": str(e)}
            results.append(
                {**check, **outcome, "qid": output.get("qid") if "output" in locals() else None}
            )
            output = {}
    finally:
        current = state(session)
        if current.get("lease", {}).get("token") == token:

            def finish(v):
                by_id = {r["id"]: r for r in results}
                full = [
                    by_id.get(
                        q["id"],
                        {
                            **q,
                            "status": "unproven",
                            "qid": None,
                            "details": "verification interrupted",
                        },
                    )
                    for q in queries
                ]
                verdict = (
                    "fail"
                    if any(r["status"] == "fail" for r in full)
                    else "pass"
                    if all(r["status"] == "pass" for r in full)
                    else "unproven"
                )
                previous = v.get("last_verification")
                passed = {r["id"] for r in full if r["status"] == "pass"}
                old_passed = {
                    r["id"] for r in (previous or {}).get("results", []) if r["status"] == "pass"
                }
                v["stalled"] = 0 if verdict == "pass" or passed > old_passed else v["stalled"] + 1
                v["verification"] = {
                    "verdict": verdict,
                    "results": full,
                    "candidate_digest": v["candidate_digest"],
                    "contract_digest": v["contract_digest"],
                    "started": started,
                    "finished": time.time(),
                    "connection": v["connection"],
                }
                v["lease"] = {}
                v["phase"] = "needs_review" if verdict == "pass" else "needs_revision"
                if v["stalled"] >= v["contract"]["policy"]["max_stalled_iterations"]:
                    v["phase"] = "blocked"
                    v["block_reason"] = "Repeated iterations made no verification progress"
                return v

            _mutate(session, current["revision"], "verification_finished", finish)
    return status(session)


def record_review(session, spec, revision):
    review = Review.model_validate(spec)

    def change(s):
        _editable(s)
        _approved(session, s)
        report = s.get("verification")
        if not report or report["verdict"] != "pass" or not _fresh(s):
            raise ValueError("review requires fresh passing verification for this candidate")
        if len(review.answers) != len(s["contract"]["review_questions"]) or not all(review.answers):
            raise ValueError("answer every review question in order")
        qids = {r["qid"] for r in report["results"]}
        if not set(review.evidence) <= qids:
            raise ValueError("review must cite this candidate's verification queries")
        s["review"] = {
            **review.model_dump(),
            "by": "agent",
            "candidate_digest": s["candidate_digest"],
        }
        s["phase"] = "needs_revision" if review.issues else "ready_for_review"
        return s

    return _mutate(session, revision, "review_recorded", change)


def _fresh(s):
    """Bound the age of the earliest probe, not just the report's last result."""
    report = s.get("verification")
    return bool(
        report
        and report["candidate_digest"] == s["candidate_digest"]
        and report["contract_digest"] == s["contract_digest"]
        and time.time() - report["started"] <= s["contract"]["policy"]["evidence_minutes"] * 60
    )


def finish(session, revision, actor="agent"):
    def change(s):
        _editable(s)
        _approved(session, s)
        level = policy.effective(session, s["contract"]["policy"]["approval"])["approval"]
        if level != "bounded":
            _human(actor)
        if (
            not _fresh(s)
            or s["verification"]["verdict"] != "pass"
            or not s.get("review")
            or s["review"]["issues"]
        ):
            raise ValueError(
                "fresh passing checks and a review without unresolved issues are required"
            )
        if (
            s["contract"]["human_semantic_review"]
            and s.get("candidate_approved") != s["candidate_digest"]
            and actor != "user"
        ):
            raise ValueError("untestable semantic definitions require human candidate review")
        if any(i["status"] == "open" for i in session.interventions()):
            raise ValueError("human interventions remain unresolved")
        from grayson.core.engine import readiness

        if readiness(session, session.workspace.workflows_dir)["open_checks"]:
            raise ValueError("workflow checkpoints remain open")
        if actor == "user":
            s["candidate_approved"] = s["candidate_digest"]
        s.update(
            phase="ready_for_deployment" if s["contract"]["kind"] == "pipeline" else "complete",
            completion={
                "by": actor,
                "label": "human accepted"
                if actor == "user"
                else "machine verified; not human accepted",
                "at": utcnow(),
            },
        )
        return s

    return _mutate(session, revision, "completed", change, actor)


def deployment_package(session, revision):
    def change(s):
        _editable(s)
        _approved(session, s)
        if s.get("deployment"):
            if s["phase"] == "ready_for_deployment":
                s["phase"] = "awaiting_deployment"
            return s
        if s["phase"] != "ready_for_deployment" or not _fresh(s):
            raise ValueError("finish a freshly verified candidate before preparing deployment")
        target = s["contract"]["deployment_target"]
        if not target:
            raise ValueError("approve a brief with an explicit deployment_target first")
        target = deployment_target(target)
        ddl = (
            f"CREATE {s['contract']['materialization'].upper()} {target.upper()} AS\n"
            f"{s['candidate_sql']}"
        )
        s["_new_proposal"] = {
            "ddl": ddl,
            "run_target": target.upper(),
            "rationale": s["contract"]["goal"],
            "project_candidate_digest": s["candidate_digest"],
            "project_contract_digest": s["contract_digest"],
        }
        s["deployment"] = {
            "ddl_digest": digest(ddl),
            "candidate_digest": s["candidate_digest"],
            "note": "Human approval and execution required. CREATE does not replace objects.",
            "recovery": "Inspect warehouse state before retrying. No rollback is assumed.",
            "verification_scope": sorted(set(s["contract"]["scope"]) | {target.upper()}),
        }
        s["phase"] = "awaiting_deployment"
        return s

    return _mutate(session, revision, "deployment_prepared", change)


def deployment_check(session, revision, executor=None):
    """Read the deployed target under the explicitly approved output scope."""
    s = state(session)
    _editable(s)
    _approved(session, s)
    d = s.get("deployment")
    target = deployment_target(s["contract"]["deployment_target"])
    proposal = session.proposal(d["pid"]) if d else None
    if (
        not proposal
        or proposal["status"] != "applied"
        or digest(proposal["payload"]["ddl"]) != d["ddl_digest"]
        or d["candidate_digest"] != s["candidate_digest"]
    ):
        raise ValueError("the exact current deployment must be human-approved and reported applied")
    if s["revision"] != revision:
        raise ValueError("project changed; reload before deployment verification")
    token = secrets.token_hex(16)
    previous_phase = s.get("lease", {}).get("previous_phase", s["phase"])

    def claim(v):
        _editable(v)
        v["lease"] = {"token": token, "expires": time.time() + 60, "previous_phase": previous_phase}
        v["phase"] = "verifying"
        v["deployed_verification"] = None
        return v

    _mutate(session, revision, "deployment_verification_started", claim)
    try:
        return _verify_deployment(session, s, target, token, executor)
    finally:
        current = state(session)
        if current.get("lease", {}).get("token") == token:

            def release(v):
                if v.get("lease", {}).get("token") == token:
                    v.update(lease={}, phase=previous_phase, deployed_verification=None)
                return v

            _mutate(session, current["revision"], "deployment_verification_interrupted", release)


def _verify_deployment(session, s, target, token, executor):
    from grayson.core.run import run_statement

    d = s["deployment"]
    # A separate audit session prevents temporarily widening the project's source
    # scope. Its allowance is already part of the approved brief and DDL package.
    settings = session.guard_settings.model_copy()
    settings.budget_cap = max(1, status(session)["remaining"]["queries"])
    child = Session.create(
        session.workspace,
        workflow="table-health",
        targets=d["verification_scope"],
        strict_scope=True,
        guard=settings,
        guard_profile=session.summary()["guard_profile"],
        connection=s["connection"],
        title="Deployment verification: " + session.id,
        actor="agent",
        project_verification_parent=session.id,
    )
    current = state(session)

    def attach(v):
        if v.get("lease", {}).get("token") != token:
            raise ValueError("deployment verification was cancelled or replaced")
        v.setdefault("verification_sessions", []).append(child.id)
        v["lease"]["expires"] = time.time() + v["contract"]["policy"]["max_minutes"] * 60 + 300
        return v

    _mutate(session, current["revision"], "deployment_session", attach)
    from grayson.projects.sql import deployment_equivalence_queries, ident

    original = f'"CANDIDATE" AS (SELECT * FROM {ident(s["candidate"]["output"])})'
    actual = f'"CANDIDATE" AS (SELECT * FROM {target})'
    queries = [
        {**check, "sql": check["sql"].replace(original, actual, 1)}
        for check in s["verification_queries"]
    ]
    queries += deployment_equivalence_queries(
        Candidate.model_validate(s["candidate"]), Contract.model_validate(s["contract"])
    )
    results = []
    started = time.time()
    for check in queries:
        current = state(session)
        if current.get("lease", {}).get("token") != token:
            return status(session)
        sql = check["sql"]
        output = run_statement(
            child, sql, label="deployed project: " + check["name"], executor=executor
        )
        try:
            if output["status"] != "executed" or output.get("cache_error"):
                raise ValueError(
                    output.get("reason") or output.get("error") or "execution incomplete"
                )
            result = evaluate(
                child, output["qid"], Expectation.model_validate(check["expectation"])
            )
        except (ValueError, OSError) as e:
            result = {"status": "unproven", "details": str(e)}
        results.append(
            {**check, **result, "sql": sql, "qid": output["qid"], "session_id": child.id}
        )
    current = state(session)

    def complete(v):
        if v.get("lease", {}).get("token") != token:
            raise ValueError("deployment verification was cancelled")
        verdict = (
            "fail"
            if any(r["status"] == "fail" for r in results)
            else "pass"
            if all(r["status"] == "pass" for r in results)
            else "unproven"
        )
        v["deployed_verification"] = {
            "verdict": verdict,
            "results": results,
            "candidate_digest": v["candidate_digest"],
            "started": started,
            "finished": time.time(),
        }
        v["lease"] = {}
        v["phase"] = "deployment_review" if verdict == "pass" else "deployment_failed"
        return v

    return _mutate(session, current["revision"], "deployment_verified", complete)


def accept_deployment(session, revision, actor="agent"):
    def change(s):
        _editable(s)
        _approved(session, s)
        if policy.effective(session, s["contract"]["policy"]["approval"])["approval"] != "bounded":
            _human(actor)
        report = s.get("deployed_verification")
        if (
            not report
            or report["verdict"] != "pass"
            or report["candidate_digest"] != s["candidate_digest"]
            or time.time() - report["started"] > s["contract"]["policy"]["evidence_minutes"] * 60
        ):
            raise ValueError("fresh passing deployment checks are required")
        s.update(
            phase="complete",
            completion={
                "by": actor,
                "at": utcnow(),
                "label": "deployment human accepted"
                if actor == "user"
                else "deployment machine verified; not human accepted",
            },
        )
        return s

    return _mutate(session, revision, "deployment_accepted", change, actor)


def queries_used(session, s):
    count = session.budget_consumed_count() - s["query_start"]
    for sid in s.get("verification_sessions", []):
        count += Session(session.workspace, sid).budget_consumed_count()
    return count


def control(session, action, reason, revision, actor="user"):
    if action not in {"pause", "resume", "cancel", "block"} or not reason.strip():
        raise ValueError("control requires pause/resume/cancel/block and a reason")
    if action in {"resume", "cancel"}:
        _human(actor)

    def change(s):
        if not s or s["phase"] in TERMINAL:
            raise ValueError("project is missing or finished")
        if action == "block" and actor != "user":
            _editable(s)
        if action == "resume":
            if s["phase"] not in {"paused", "blocked"}:
                raise ValueError("only a paused or blocked project can resume")
            previous = s.pop("paused_from", None)
            was_paused = s["phase"] == "paused"
            restore = was_paused and previous not in {
                None,
                "paused",
                "blocked",
                "verifying",
            }
            if restore:
                s["phase"] = previous
            else:
                s["phase"] = "needs_revision" if s["candidate"] else "building"
            if was_paused and previous == "verifying":
                level = policy.effective(session, s["contract"]["policy"]["approval"])["approval"]
                pending_phase = _candidate_phase({**s, "phase": "verifying", "lease": {}}, level)
                if pending_phase == "candidate_review":
                    s["phase"] = pending_phase
            s["stalled"] = 0
        else:
            if action == "pause" and s["phase"] != "paused":
                s["paused_from"] = s["phase"]
            s["phase"] = {"pause": "paused", "cancel": "cancelled", "block": "blocked"}[action]
        s.update(block_reason=reason, lease={}, runner={})
        return s

    return _mutate(session, revision, action, change, actor)


def set_approval(session, level, revision, actor="user"):
    _human(actor)
    if level not in policy.LEVELS:
        raise ValueError("approval must be guided, milestones or bounded")

    def change(s):
        if not s or s["phase"] in TERMINAL:
            raise ValueError("project is missing or finished")
        s["approval_override"] = level
        # Quality, evidence and the brief stay unchanged. Only human interrupt
        # points change, still narrowed by workspace/library ceilings.
        return s

    return _mutate(session, revision, "approval_level_changed", change, actor)


def plan(session, steps, revision):
    parsed = [PlanStep.model_validate(x) for x in steps]
    seen = set()
    executed = session.executed_qids()
    for step in parsed:
        if step.id in seen or not set(step.depends_on) <= seen:
            raise ValueError("plan requires unique steps ordered after their dependencies")
        if step.status == "done" and (not step.evidence or not set(step.evidence) <= executed):
            raise ValueError("completed plan steps require executed evidence")
        seen.add(step.id)

    def change(s):
        _editable(s)
        _approved(session, s)
        s["plan"] = [p.model_dump() for p in parsed]
        return s

    return _mutate(session, revision, "plan_updated", change)


def status(session):
    s = state(session)
    if not s:
        return {"project": None, "next_action": "draft a project brief"}
    effective = policy.effective(session, s["contract"]["policy"]["approval"])
    if _candidate_phase(s, effective["approval"]) != s["phase"]:
        # Workspace/library ceilings can change outside a project mutation.
        # Persist the gate and its revision so the console and runner agree.
        return _mutate(
            session, s["revision"], "candidate_gate_changed", lambda value: value, "system"
        )
    p = s["contract"]["policy"]
    blocker = query_blocker(session)
    actions = {
        "awaiting_brief": "Human: review and approve the project brief",
        "building": "Develop a candidate graph using only the approved resources and semantics",
        "candidate_review": "Human: review the exact candidate SQL",
        "verifying": "Run all generated verification probes",
        "needs_revision": "Diagnose failures; submit a corrected candidate naming addressed checks",
        "needs_review": "Review semantics, lossy operations and limitations against fresh evidence",
        "ready_for_review": "Accept the result under the effective approval policy",
        "ready_for_deployment": "Prepare DDL for human approval and execution",
        "deployment_review": "Accept the freshly verified deployment under the approval policy",
        "deployment_failed": "Diagnose deployed failures; every revised DDL needs human approval",
        "awaiting_deployment": "Human: approve and run the exact DDL, then report application",
        "complete": "Project complete; no further work authorised",
        "paused": "Human: resume when ready",
        "blocked": "Human: resolve the blocker or revise the project brief",
        "cancelled": "Project cancelled",
    }
    return {
        "project": s,
        "effective_policy": effective,
        "query_blocker": blocker,
        "evidence_current": _fresh(s),
        "next_action": blocker or actions.get(s["phase"], s["phase"]),
        "remaining": {
            "queries": max(0, p["max_queries"] - queries_used(session, s)),
            "iterations": max(0, p["max_iterations"] - s["iterations"]),
            "seconds": max(0, p["max_minutes"] * 60 - elapsed(s)),
        },
    }


def brief(session):
    """Bounded resume context; detailed SQL/evidence stays available in status."""
    view = status(session)
    s = view["project"]
    if s:
        s["history"] = [
            {
                "digest": h["digest"],
                "diagnosis": h["candidate"]["diagnosis"],
                "verdict": (h.get("verification") or {}).get("verdict"),
            }
            for h in s["history"][-5:]
        ]
        s.pop("verification_queries", None)
        for key in ("verification", "deployed_verification"):
            if s.get(key):
                for result in s[key]["results"]:
                    if result["status"] == "pass":
                        result.pop("sql", None)
        s.pop("workflow", None)
    return view


def diagnose(session, check_id, max_rows=20, executor=None):
    """Return bounded examples from a failed probe, never acceptance evidence."""
    import sqlglot
    from sqlglot import exp

    from grayson.core.run import run_statement

    s = state(session)
    _approved(session, s)
    if not 1 <= max_rows <= 100:
        raise ValueError("diagnostic examples must be limited to 1-100 rows")
    report = s.get("verification")
    item = next((r for r in (report or {}).get("results", []) if r["id"] == check_id), None)
    if not item or item["status"] == "pass":
        raise ValueError("choose a failed or unproven current verification check")
    tree = sqlglot.parse_one(item["sql"], read="snowflake")
    if len(tree.expressions) == 1 and isinstance(tree.expressions[0], exp.Alias):
        if isinstance(tree.expressions[0].this, exp.Count) and not tree.args.get("group"):
            tree.set("expressions", [exp.Star()])
        elif item["expectation"]["kind"] != "no_rows":
            raise ValueError("this custom scalar needs a separately authored diagnostic query")
    result = run_statement(
        session,
        tree.limit(max_rows).sql(dialect="snowflake"),
        label="project diagnostic: " + check_id,
        executor=executor,
    )
    return {
        **result,
        "check_id": check_id,
        "candidate_digest": s["candidate_digest"],
        "notice": "Bounded diagnostic examples only; rerun full verification after a repair",
    }
