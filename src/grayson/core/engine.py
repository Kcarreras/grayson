"""Checkpoint & findings engine — the deterministic QA-of-QA gate.

Enforces that checkpoints close only with real evidence, findings validate
against their schema and cite executed queries, and stage transitions respect
the workflow's required checks. grayson makes no judgement about analytical
*quality*; it makes it impossible to *claim* work that leaves no evidence.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from grayson.core.session import OUTCOMES, STAGES, Session
from grayson.findings.schemas import validate_finding
from grayson.workflows import WorkflowTemplate, get_workflow


class EnforcementError(ValueError):
    """A gate refused an action; message explains what evidence is missing."""


def workflow_for(session: Session, overrides_dir: Path | None = None) -> WorkflowTemplate:
    return get_workflow(session.workflow, overrides_dir)


def seed_from_workflow(session: Session, overrides_dir: Path | None = None) -> list[dict]:
    tpl = workflow_for(session, overrides_dir)
    session.seed_checkpoints([(c.key, c.title) for c in tpl.required_checks])
    return session.checkpoints()


def _validate_evidence(session: Session, evidence: list[str]) -> list[str]:
    """Check evidence exists, executed, and is relevant. Returns the off-scope ids."""
    if not evidence:
        raise EnforcementError(
            "evidence required: cite the executed query ids (q_XXXX) that support this"
        )
    executed = session.executed_qids()
    missing = [q for q in evidence if q not in executed]
    if missing:
        raise EnforcementError(
            f"evidence cites query ids that were not executed successfully: {missing}. "
            "Only successfully executed queries count as evidence."
        )
    # Relevance: evidence must actually touch the tables under investigation, so a
    # trivial `SELECT 1` can't be laundered into evidence. Skipped only when the
    # session declared no targets (nothing to bind relevance to).
    #
    # Deliberately at-least-one, not all: `upstream_trace` exists to walk lineage
    # through tables that are NOT declared targets, and those probes are legitimate
    # evidence. Requiring every cited qid to touch scope would push agents to stuff
    # a target table into each upstream query — a new laundering incentive in place
    # of the old one. Instead the off-scope ids are reported, so a human reading the
    # checkpoint sees "1 of 4 cited queries touched scope" and can judge it.
    scope = session.scope_tables
    if not scope:
        return []
    tables_by_qid = session.query_tables_many(list(evidence))
    in_scope = [q for q in evidence if {t.upper() for t in tables_by_qid.get(q, [])} & scope]
    if not in_scope:
        raise EnforcementError(
            "evidence does not touch any table under investigation "
            f"(session scope: {sorted(scope)}). Cite queries that actually read the "
            "target tables, not unrelated probes."
        )
    return [q for q in evidence if q not in in_scope]


def complete_checkpoint(
    session: Session,
    key: str,
    evidence: list[str],
    note: str = "",
    actor: str = "agent",
    overrides_dir: Path | None = None,
) -> dict:
    tpl = workflow_for(session, overrides_dir)
    if tpl.check(key) is None and session.checkpoint(key) is None:
        known = ", ".join(tpl.required_check_keys() + tpl.suggested_check_keys())
        raise EnforcementError(
            f"unknown checkpoint '{key}' for workflow '{tpl.name}' (checks: {known})"
        )
    # Ordering, where a workflow declares it: bug-hunter's "no cause-hunting until
    # it reproduces" was prose in a description and enforced by nothing.
    cleared = {c["key"] for c in session.checkpoints() if c["status"] in ("complete", "waived")}
    unmet = tpl.unmet_dependencies(key, cleared)
    if unmet:
        raise EnforcementError(
            f"'{key}' depends on {unmet}, which {'is' if len(unmet) == 1 else 'are'} not "
            "closed yet. Close them first (or have the user waive them) — the order is "
            "part of the method, not bookkeeping."
        )
    off_scope = _validate_evidence(session, evidence)
    if session.checkpoint(key) is None:
        # a suggested check is not seeded up front (it would read as an open gate);
        # it materializes the moment an agent decides to do it
        defn = tpl.check(key)
        session.seed_checkpoints([(key, defn.title if defn else key)])
    session.complete_checkpoint(key, evidence, note, actor)
    out = session.checkpoint(key)
    if off_scope:
        # legitimate for lineage probes; surfaced so a human can tell the
        # difference between walking upstream and padding the citation list
        out["evidence_off_scope"] = off_scope
        session.log_event(
            actor, "evidence_off_scope", {"key": key, "qids": off_scope, "cited": len(evidence)}
        )
    return out


def waive_checkpoint(
    session: Session,
    key: str,
    reason: str,
    actor: str = "user",
    overrides_dir: Path | None = None,
) -> dict:
    """Mark a checkpoint not-applicable. A user action, with a mandatory reason.

    An unwaivable gate on a genuinely inapplicable check manufactures the exact
    evidence-laundering the rail exists to prevent — the agent's only way through
    is a query chosen to satisfy the scope test rather than to learn anything. So
    the gap gets an honest, named, audited exit instead. Agents may *ask* for one
    (file an intervention); only a human grants it.
    """
    tpl = workflow_for(session, overrides_dir)
    if actor != "user":
        raise EnforcementError(
            "waiving a checkpoint is a user action; agents cannot waive their own gates. "
            "File an intervention asking the user to waive it, and say why it does not apply."
        )
    if tpl.check(key) is None and session.checkpoint(key) is None:
        known = ", ".join(tpl.required_check_keys())
        raise EnforcementError(
            f"unknown checkpoint '{key}' for workflow '{tpl.name}' (checks: {known})"
        )
    try:
        session.waive_checkpoint(key, reason, actor)
    except ValueError as e:
        raise EnforcementError(str(e.args[0] if e.args else e)) from e
    return session.checkpoint(key)


def record_finding(
    session: Session,
    payload: dict,
    worker: str | None = None,
    overrides_dir: Path | None = None,
) -> dict:
    tpl = workflow_for(session, overrides_dir)
    try:
        finding = validate_finding(payload, tpl.findings_schema)
    except (ValidationError, ValueError) as e:
        raise EnforcementError(f"finding failed schema '{tpl.findings_schema}': {e}") from e
    _validate_evidence(session, finding.evidence)
    if finding.supersedes:
        target = session.finding(finding.supersedes)
        if target is None:
            raise EnforcementError(
                f"supersedes cites unknown finding '{finding.supersedes}'. "
                "Cite an existing finding in this session."
            )
        if target.get("superseded_by"):
            raise EnforcementError(
                f"finding '{finding.supersedes}' is already superseded by "
                f"'{target['superseded_by']}' — supersede the head of the chain instead."
            )
    fid = session.add_finding(
        schema_name=finding.schema_name,
        severity=finding.severity,
        confidence=finding.confidence,
        title=finding.title,
        payload=finding.model_dump(),
        worker=worker,
    )
    if finding.supersedes:
        # a proposal only — nothing changes on the old finding until the user
        # accepts this one (see Session.accept_finding)
        session.log_event(
            worker or "agent",
            "supersession_proposed",
            {"fid": fid, "supersedes": finding.supersedes},
        )
    return session.finding(fid)


def readiness(session: Session, overrides_dir: Path | None = None) -> dict:
    """Report what still blocks the next stage transition."""
    tpl = workflow_for(session, overrides_dir)
    checkpoints = {c["key"]: c for c in session.checkpoints()}
    keys = tpl.required_check_keys()
    # a waived check satisfies the gate but never masquerades as a closed one:
    # it is reported separately, with its reason, everywhere readiness is shown
    open_checks = [
        k for k in keys if checkpoints.get(k, {}).get("status") not in ("complete", "waived")
    ]
    waived_checks = [
        {
            "key": k,
            "reason": checkpoints[k].get("note") or "",
            "by": checkpoints[k].get("completed_by"),
        }
        for k in keys
        if checkpoints.get(k, {}).get("status") == "waived"
    ]
    findings = session.findings()
    # a superseded finding was replaced by a corrected one: it no longer counts
    # as accepted for any gate, however it got there
    unaccepted = [f["fid"] for f in findings if not f["accepted"] or f.get("superseded_by")]
    # a finding the user has neither accepted nor rejected is still theirs to
    # judge — it blocks a clean close, where a rejected one does not
    pending = [
        f["fid"]
        for f in findings
        if not f["accepted"] and not f.get("rejected") and not f.get("superseded_by")
    ]
    accepted_count = len(findings) - len(unaccepted)
    out = {
        "stage": session.stage,
        "outcome": session.outcome,
        "workflow": tpl.name,
        "required_checks": keys,
        "open_checks": open_checks,
        "waived_checks": waived_checks,
        "checks_complete": not open_checks,
        "findings_total": len(findings),
        "findings_accepted": accepted_count,
        "findings_pending": pending,
        "findings_unaccepted": unaccepted,
        "findings_superseded": [
            {"fid": f["fid"], "by": f["superseded_by"]} for f in findings if f.get("superseded_by")
        ],
        "findings_rejected": [
            {"fid": f["fid"], "reason": f["rejected_reason"]} for f in findings if f.get("rejected")
        ],
    }
    done = {c["key"] for c in session.checkpoints() if c["status"] == "complete"}
    out["suggested_checks"] = [
        {"key": c.key, "title": c.title, "done": c.key in done} for c in tpl.suggested_checks
    ]
    out["clean_close_available"] = clean_close_blockers(out) == []
    out["next_action"] = _next_action(out)
    return out


def clean_close_blockers(ready: dict) -> list[str]:
    """What stands between this session and a confirmed clean close.

    Clean means: every required check cleared (closed with evidence or waived),
    nothing accepted as a finding, and nothing still awaiting the user's
    judgement. Findings the user *rejected* do not block — the user has already
    said they were not real, which is itself a clean result.
    """
    blockers = []
    if ready["open_checks"]:
        blockers.append(f"required checkpoints still open: {ready['open_checks']}")
    if ready["findings_accepted"]:
        blockers.append(
            f"{ready['findings_accepted']} accepted finding(s) — this run found something, "
            "so it closes through fixes/verification, not clean"
        )
    if ready["findings_pending"]:
        blockers.append(f"findings awaiting the user's accept/reject: {ready['findings_pending']}")
    return blockers


def _next_action(ready: dict) -> str:
    """One sentence telling the agent what to do next. Readiness is the tool
    agents poll when stuck; a bare list of blockers leaves them guessing."""
    if ready["stage"] == "closed":
        return "session is closed"
    if ready["open_checks"]:
        return (
            f"close the remaining checkpoints with evidence: {', '.join(ready['open_checks'])}"
            " — or, if one genuinely does not apply, ask the user to waive it (an"
            " intervention explaining why); agents cannot waive their own gates"
        )
    if ready["findings_pending"]:
        return (
            f"findings {ready['findings_pending']} are waiting on the user to accept or "
            "reject in the console"
        )
    if ready["findings_accepted"]:
        return "accepted findings exist — advance to fixes and propose remediation"
    return (
        "checks are clear and nothing was found worth acting on — ask the user to close "
        "this session as a clean result (console button, or `grayson session close "
        "<sid> --clean`). A clean run is a real outcome; do not manufacture a finding "
        "to close it."
    )


#: stages at or beyond this index require all checkpoints complete
_REVIEW_IDX = STAGES.index("review")
#: stages at or beyond this index require at least one accepted finding
_FIXES_IDX = STAGES.index("fixes")


def advance_stage(
    session: Session,
    to_stage: str,
    actor: str = "user",
    force: bool = False,
    overrides_dir: Path | None = None,
) -> dict:
    """Gate stage transitions on the workflow's evidence requirements.

    Gates are *cumulative and target-index based*, not keyed to one specific
    target stage — so jumping straight to 'verification' or 'closed' cannot skip
    them. Loop-backs to earlier stages are always allowed. `force` is a human
    escape hatch: it is honored only for the 'user' actor (an agent cannot
    self-authorize a bypass).
    """
    if to_stage not in STAGES:
        raise EnforcementError(f"unknown stage '{to_stage}' (stages: {', '.join(STAGES)})")
    if force and actor != "user":
        raise EnforcementError(
            "force override is a user action; agents cannot bypass evidence gates. "
            "Ask the user to advance with force if a bypass is genuinely needed."
        )
    ready = readiness(session, overrides_dir)
    target_idx = STAGES.index(to_stage)

    # Gate: reaching review or later requires all required checkpoints complete.
    if target_idx >= _REVIEW_IDX and not force and not ready["checks_complete"]:
        raise EnforcementError(
            f"cannot reach '{to_stage}': required checkpoints still open: "
            f"{ready['open_checks']}. Complete them with evidence first."
        )
    # Gate: reaching fixes or later requires at least one user-accepted finding.
    if target_idx >= _FIXES_IDX and not force and ready["findings_accepted"] == 0:
        # A run that found nothing is not an unfinished run. Rather than dead-end
        # it against a gate it can never satisfy, name the route that does apply.
        if to_stage == "closed" and ready["clean_close_available"]:
            raise EnforcementError(
                "nothing was found worth acting on, so there is no accepted finding to "
                "advance on — this is a clean run, and it closes as one. Ask the user to "
                "close it clean (console, or `grayson session close <sid> --clean`). Do "
                "not record a finding you do not believe in order to clear this gate."
            )
        raise EnforcementError(
            f"cannot reach '{to_stage}': no user-accepted finding. Findings must be "
            "recorded and accepted by the user (in the console) before fixes."
        )
    session.set_stage(to_stage, actor)
    if to_stage == "closed" and not session.outcome:
        # 'clean' means a human vouched for a negative result, so a forced close
        # never earns it — it leaves the outcome blank rather than overstating it
        if ready["findings_accepted"]:
            session.set_outcome("findings", actor)
        elif not force:
            session.set_outcome("clean", actor)
    if force:
        session.log_event(
            actor, "stage_gate_forced", {"stage": to_stage, "open_checks": ready["open_checks"]}
        )
    return readiness(session, overrides_dir)


def close_session(
    session: Session,
    actor: str = "user",
    note: str = "",
    overrides_dir: Path | None = None,
) -> dict:
    """Close a session through the gates, recording *how* it ended.

    Two legitimate endings, and both are real results:
    - `findings` — at least one accepted finding; the ordinary route, and it must
      still satisfy every gate `advance_stage` applies.
    - `clean` — the required checks cleared and nothing was found worth acting on.

    Closing is a user action either way: it is the last of grayson's human
    boundaries, and a clean close in particular is a human vouching for a
    negative result. Agents ask; they do not self-certify.
    """
    if actor != "user":
        raise EnforcementError(
            "closing a session is a user action. Ask the user to close it (console, or "
            "`grayson session close`); report what you found — or that you found nothing."
        )
    ready = readiness(session, overrides_dir)
    if ready["findings_accepted"]:
        # a run with accepted findings closes on the normal gated path
        advance_stage(session, "closed", actor, False, overrides_dir)
        return readiness(session, overrides_dir)
    blockers = clean_close_blockers(ready)
    if blockers:
        raise EnforcementError(
            "cannot close: " + "; ".join(blockers) + ". A session closes either with "
            "accepted findings or as a confirmed clean result — not with work still open."
        )
    session.set_outcome(OUTCOMES[0], actor, note)
    session.set_stage("closed", actor)
    return readiness(session, overrides_dir)
