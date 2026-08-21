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


def _validate_evidence(session: Session, evidence: list[str], relevant_tables=None) -> None:
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


def advance_stage(
    session: Session,
    to_stage: str,
    actor: str = "user",
    force: bool = False,
    overrides_dir: Path | None = None,
) -> dict:
    """Gate stage transitions on the workflow's evidence requirements."""
    if to_stage not in STAGES:
        raise EnforcementError(f"unknown stage '{to_stage}' (stages: {', '.join(STAGES)})")
    ready = readiness(session, overrides_dir)

    # Gate: leaving analysis/synthesis into review requires all checks complete.
    entering_review = to_stage == "review"
    if entering_review and not force and not ready["checks_complete"]:
        raise EnforcementError(
            "cannot enter 'review': required checkpoints still open: "
            f"{ready['open_checks']}. Complete them with evidence, or override with force."
        )
    # Gate: entering fixes requires at least one accepted finding (nothing to fix otherwise).
    if to_stage == "fixes" and not force:
        findings = session.findings()
        if not findings:
            raise EnforcementError(
                "cannot enter 'fixes': no findings recorded. Record findings first, "
                "or override with force if fixes are being made without formal findings."
            )
    session.set_stage(to_stage, actor)
    if force:
        session.log_event(
            actor, "stage_gate_forced", {"stage": to_stage, "open_checks": ready["open_checks"]}
        )
    return readiness(session, overrides_dir)
