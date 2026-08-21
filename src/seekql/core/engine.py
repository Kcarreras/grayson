"""Checkpoint & findings engine — the deterministic QA-of-QA gate.

Enforces that checkpoints close only with real evidence, findings validate
against their schema and cite executed queries, and stage transitions respect
the workflow's required checks. seekql makes no judgement about analytical
*quality*; it makes it impossible to *claim* work that leaves no evidence.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from seekql.core.session import STAGES, Session
from seekql.findings.schemas import validate_finding
from seekql.workflows import WorkflowTemplate, get_workflow


class EnforcementError(ValueError):
    """A gate refused an action; message explains what evidence is missing."""


def workflow_for(session: Session, overrides_dir: Path | None = None) -> WorkflowTemplate:
    return get_workflow(session.workflow, overrides_dir)


def seed_from_workflow(session: Session, overrides_dir: Path | None = None) -> list[dict]:
    tpl = workflow_for(session, overrides_dir)
    session.seed_checkpoints([(c.key, c.title) for c in tpl.required_checks])
    return session.checkpoints()


def _validate_evidence(session: Session, evidence: list[str]) -> None:
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
    scope = session.scope_tables
    if scope:
        touched: set[str] = set()
        for qid in evidence:
            touched.update(t.upper() for t in session.query_tables(qid))
        if not (touched & scope):
            raise EnforcementError(
                "evidence does not touch any table under investigation "
                f"(session scope: {sorted(scope)}). Cite queries that actually read the "
                "target tables, not unrelated probes."
            )


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
        known = ", ".join(tpl.required_check_keys())
        raise EnforcementError(
            f"unknown checkpoint '{key}' for workflow '{tpl.name}' (checks: {known})"
        )
    _validate_evidence(session, evidence)
    session.complete_checkpoint(key, evidence, note, actor)
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
    fid = session.add_finding(
        schema_name=finding.schema_name,
        severity=finding.severity,
        confidence=finding.confidence,
        title=finding.title,
        payload=finding.model_dump(),
        worker=worker,
    )
    return session.finding(fid)


def readiness(session: Session, overrides_dir: Path | None = None) -> dict:
    """Report what still blocks the next stage transition."""
    tpl = workflow_for(session, overrides_dir)
    checkpoints = {c["key"]: c for c in session.checkpoints()}
    open_checks = [
        k for k in tpl.required_check_keys() if checkpoints.get(k, {}).get("status") != "complete"
    ]
    findings = session.findings()
    unaccepted = [f["fid"] for f in findings if not f["accepted"]]
    return {
        "stage": session.stage,
        "workflow": tpl.name,
        "required_checks": tpl.required_check_keys(),
        "open_checks": open_checks,
        "checks_complete": not open_checks,
        "findings_total": len(findings),
        "findings_unaccepted": unaccepted,
    }


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
    if target_idx >= _FIXES_IDX and not force:
        accepted = [f for f in session.findings() if f["accepted"]]
        if not accepted:
            raise EnforcementError(
                f"cannot reach '{to_stage}': no user-accepted finding. Findings must be "
                "recorded and accepted by the user (in the console) before fixes."
            )
    session.set_stage(to_stage, actor)
    if force:
        session.log_event(
            actor, "stage_gate_forced", {"stage": to_stage, "open_checks": ready["open_checks"]}
        )
    return readiness(session, overrides_dir)
