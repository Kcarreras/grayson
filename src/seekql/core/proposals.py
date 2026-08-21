"""Fix proposals and verification.

A proposal is a concrete remediation an agent drafts for a finding — either a
file diff against a work-repo definition file, or a standalone DDL snippet the
user runs. seekql stores and gates proposals; it never writes outside its own
workspace, so an approved file diff is applied by the harness agent, not seekql.

Verification records deterministic before/after evidence that a fix worked, so
"the fix resolved the issue" is a claim backed by re-run queries, not assertion.
"""

from __future__ import annotations

from seekql.cache.store import compare_artifacts
from seekql.core.session import Session

PROPOSAL_KINDS = {"file_diff", "ddl_snippet"}


class ProposalError(ValueError):
    pass


def build_proposal_payload(kind: str, payload: dict) -> dict:
    if kind not in PROPOSAL_KINDS:
        raise ProposalError(
            f"unknown proposal kind '{kind}' (kinds: {', '.join(sorted(PROPOSAL_KINDS))})"
        )
    if kind == "file_diff":
        target = payload.get("target_file")
        if not target:
            raise ProposalError("file_diff requires 'target_file' (path in the work repo)")
        diff = payload.get("diff")
        new_content = payload.get("new_content")
        if not diff and not new_content:
            raise ProposalError("file_diff requires 'diff' or 'new_content'")
        return {
            "target_file": str(target),
            "diff": diff or "",
            "new_content": new_content or "",
            "rationale": payload.get("rationale", ""),
        }
    # ddl_snippet
    ddl = payload.get("ddl")
    if not ddl:
        raise ProposalError("ddl_snippet requires 'ddl'")
    return {
        "ddl": str(ddl),
        "run_target": payload.get("run_target", ""),
        "rationale": payload.get("rationale", ""),
    }


def record_proposal(
    session: Session,
    kind: str,
    title: str,
    payload: dict,
    finding_fid: str | None,
    worker: str | None = None,
) -> dict:
    body = build_proposal_payload(kind, payload)
    if finding_fid is not None and session.finding(finding_fid) is None:
        raise ProposalError(f"proposal references unknown finding '{finding_fid}'")
    pid = session.add_proposal(kind, title, body, finding_fid, worker)
    return session.proposal(pid)


def decide(session: Session, pid: str, approve: bool, actor: str = "user") -> dict:
    try:
        session.decide_proposal(pid, "approved" if approve else "rejected", actor)
    except (KeyError, ValueError) as e:
        raise ProposalError(str(e.args[0] if e.args else e)) from e
    return session.proposal(pid)


def mark_applied(session: Session, pid: str, actor: str = "agent") -> dict:
    p = session.proposal(pid)
    if p is None:
        raise ProposalError(f"no proposal '{pid}'")
    if p["status"] != "approved":
        raise ProposalError(
            f"proposal '{pid}' must be approved before it is applied (status={p['status']})"
        )
    session.set_proposal_status(pid, "applied", actor)
    return session.proposal(pid)


def verify(
    session: Session,
    pid: str,
    before_qid: str,
    after_qid: str,
    verdict: str,
    note: str = "",
    actor: str = "agent",
) -> dict:
    """Record a verification for a proposal, citing before/after evidence.

    The mechanical comparison is computed deterministically; the pass/fail
    verdict is the agent's analytical call but must cite executed queries.
    """
    if verdict not in {"pass", "fail"}:
        raise ProposalError("verdict must be 'pass' or 'fail'")
    p = session.proposal(pid)
    if p is None:
        raise ProposalError(f"no proposal '{pid}'")
    executed = session.executed_qids()
    missing = [q for q in (before_qid, after_qid) if q not in executed]
    if missing:
        raise ProposalError(
            f"verification must cite successfully executed queries; missing: {missing}"
        )
    try:
        comparison = compare_artifacts(session.cache, before_qid, after_qid)
    except KeyError as e:
        raise ProposalError(str(e.args[0])) from e
    verification = {
        "verdict": verdict,
        "before_qid": before_qid,
        "after_qid": after_qid,
        "comparison": comparison,
        "note": note,
    }
    session.attach_verification(pid, verification, actor)
    return session.proposal(pid)
