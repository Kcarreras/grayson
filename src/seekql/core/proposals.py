"""Fix proposals and verification.

A proposal is a concrete remediation an agent drafts for a finding — either a
file diff against a work-repo definition file, or a standalone DDL snippet the
user runs. seekql stores and gates proposals; it never writes outside its own
workspace, so an approved file diff is applied by the harness agent, not seekql.

Verification records deterministic before/after evidence that a fix worked, so
"the fix resolved the issue" is a claim backed by re-run queries, not assertion.
"""

from __future__ import annotations

from pydantic import ValidationError

from seekql.cache.store import compare_artifacts
from seekql.core.session import Session
from seekql.views import ViewEntry

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
    body = {
        "ddl": str(ddl),
        "run_target": payload.get("run_target", ""),
        "rationale": payload.get("rationale", ""),
    }
    # A DDL snippet that creates a QA view declares it, so the library compounds:
    # once the user has run the DDL and the proposal is marked applied, the view
    # is registered (and scoped) automatically — no separate manual step.
    view_name = payload.get("view_name")
    if view_name:
        try:
            ViewEntry(name=str(view_name))  # early validation, clear message
        except ValidationError as e:
            first = e.errors()[0]
            raise ProposalError(f"invalid view_name: {first['msg']}") from e
        body["view_name"] = str(view_name)
        body["source_tables"] = [str(t).upper() for t in payload.get("source_tables") or []]
        body["base_files"] = [str(b) for b in payload.get("base_files") or []]
        body["purpose"] = str(payload.get("purpose", ""))
    return body


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
    out = session.proposal(pid)
    if p["kind"] == "ddl_snippet" and p["payload"].get("view_name"):
        out["view_registered"] = _register_applied_view(session, p["payload"], pid, actor)
    return out


def _register_applied_view(session: Session, payload: dict, pid: str, actor: str) -> dict:
    """Close the propose→execute→register loop for view-creating DDL.

    Runs only after a user approved the proposal (mark_applied gates on that),
    so agents cannot launder arbitrary names into the registry/scope. The
    staleness baseline is captured now — the sources' LAST_ALTERED at the moment
    the view exists — and the view enters this session's scope so verification
    queries against it count as evidence.
    """
    from seekql.core.run import fetch_last_altered
    from seekql.library import maybe_auto_push
    from seekql.views import ViewRegistry

    sources = payload.get("source_tables") or []
    snapshot = fetch_last_altered(session.connection, session.workspace.root, sources)
    entry = ViewEntry(
        name=payload["view_name"],
        purpose=payload.get("purpose", ""),
        source_tables=sources,
        base_files=payload.get("base_files") or [],
    )
    ViewRegistry(session.workspace.views_dir).register(
        entry, ddl=payload["ddl"], source_last_altered=snapshot
    )
    session.add_scope([entry.name])
    session.log_event(actor, "view_registered", {"view": entry.name, "proposal": pid})
    registered = {
        "name": entry.name,
        "source_tables": sources,
        "staleness_baseline_captured": bool(snapshot),
        "in_session_scope": True,
    }
    sync = maybe_auto_push(session.workspace, f"seekql views: register {entry.name} ({pid})")
    if sync is not None:
        registered["library_sync"] = sync
    return registered


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
    # Verification comes after the user approved and the fix was applied — it must
    # not be a back door that stamps an un-approved (or rejected) proposal 'verified'.
    if p["status"] not in {"approved", "applied", "verification_failed"}:
        raise ProposalError(
            f"proposal '{pid}' must be approved (and applied) before verification "
            f"(status={p['status']})"
        )
    if before_qid == after_qid:
        raise ProposalError(
            "before and after evidence must be different queries — a query compared "
            "to itself proves nothing"
        )
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
