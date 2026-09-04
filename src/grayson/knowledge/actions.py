"""Lifecycle actions on knowledge, policy-gated — one implementation behind
the CLI, the MCP server and the console.

Each action here does the same three things whichever surface called it:
checks the effective policy for the actor (an agent may act only where the
human said so; a human always may), performs the write through the store
(which enforces the evidence rule for agents), and lands the change as its
own library commit carrying a `Grayson-Via` trailer — so one action is one
revert, and `git log` answers who did what.
"""

from __future__ import annotations

import re
from typing import Any

from grayson.knowledge.policy import ACTIONS, EffectivePolicy
from grayson.knowledge.standing import StandingContext, annotate_doc
from grayson.knowledge.store import KnowledgeStore, completeness
from grayson.workspace import Workspace

Actor = str  # "agent" | "user"
Surface = str  # "mcp" | "cli" | "console"

_QID = re.compile(r"^q_\d+$")


class ActionRefused(PermissionError):
    """The policy keeps this action the human's. The message names the setting."""

    def __init__(self, message: str, policy: EffectivePolicy):
        super().__init__(message)
        self.policy = policy


def policy_for(workspace: Workspace) -> EffectivePolicy:
    from grayson.library import effective_policy

    return effective_policy(workspace)


def require(workspace: Workspace, action: str, actor: Actor) -> EffectivePolicy:
    """The effective policy, after refusing an agent an action it withholds."""
    policy = policy_for(workspace)
    if action not in ACTIONS:
        raise ValueError(f"unknown knowledge action {action!r}")
    if actor == "agent" and not policy.allows_agent(action):
        raise ActionRefused(policy.refusal(action), policy)
    return policy


def _via(actor: Actor, surface: Surface) -> str | None:
    if actor != "agent":
        return None
    return "mcp-agent" if surface == "mcp" else "cli-agent"


def _commit(workspace: Workspace, table: str, message: str, actor: Actor, surface: Surface) -> dict:
    """One library commit for this action alone (the doc's own path), pushed
    when the workspace auto-pushes. A library that is not a git repo just
    keeps the file as changed."""
    from grayson.library import commit_library_paths

    store = KnowledgeStore(workspace.knowledge_dir)
    rel = store.table_path(table).relative_to(store.dir.parent)
    return commit_library_paths(workspace, [str(rel)], message, via=_via(actor, surface))


def _session_evidence(workspace: Workspace, session_id: str | None, evidence: list[str]) -> None:
    """When the caller names a session, query ids in the evidence must have
    executed there — the same rule findings live under."""
    if not session_id:
        return
    from grayson.core.session import Session, resolve_session_id

    session = Session(workspace, resolve_session_id(workspace, session_id))
    executed = session.executed_qids()
    missing = [e for e in evidence if _QID.match(e) and e not in executed]
    if missing:
        raise ValueError(
            f"evidence cites query ids that did not execute in session {session.id}: "
            f"{missing} — only successfully executed queries count"
        )


def show(workspace: Workspace, table: str, live_columns: dict | None = None) -> dict:
    """A table's knowledge as every reader should see it: the doc with each
    fact's standing and role, the completeness report, captured definition
    snapshots, contested pairs, recent agent actions, and what the policy lets
    an agent do about any of it."""
    from grayson.knowledge import SNAPSHOT_INLINE_CHARS

    store = KnowledgeStore(workspace.knowledge_dir)
    doc = store.read(table)
    policy = policy_for(workspace)
    ctx = StandingContext.build(workspace.records_dir, policy, live_columns=live_columns)
    annotated = annotate_doc(doc, ctx)
    snapshots: dict[str, str] = {}
    for d in doc["definitions"]:
        name = d.get("snapshot")
        text = store.read_snapshot(doc["table"], str(name)) if name else None
        if text is not None:
            snapshots[str(name)] = text[:SNAPSHOT_INLINE_CHARS]
    return {
        **annotated,
        "completeness": completeness(doc),
        "definition_snapshots": snapshots,
        "policy": {"actions": dict(policy.actions), "trust": policy.trust},
    }


def retire(
    workspace: Workspace,
    table: str,
    fact_id: str,
    reason: str = "",
    evidence: list[str] | None = None,
    actor: Actor = "agent",
    surface: Surface = "mcp",
    session_id: str | None = None,
) -> dict:
    policy = require(workspace, "retire", actor)
    evidence = [str(e) for e in (evidence or []) if str(e).strip()]
    _session_evidence(workspace, session_id, evidence)
    store = KnowledgeStore(workspace.knowledge_dir)
    fact = store.retire_fact(table, fact_id, reason=reason, by=actor, evidence=evidence)
    fqn = store.read(table)["table"]
    why = reason or ", ".join(evidence)
    sync = _commit(
        workspace,
        fqn,
        f"grayson knowledge: retire {fact_id} on {fqn}\n\n{why}",
        actor,
        surface,
    )
    return {"fact": fact, "library_sync": sync, "policy_actor": policy.actor("retire")}


def supersede(
    workspace: Workspace,
    table: str,
    fact_id: str,
    text: str,
    evidence: list[str] | None = None,
    status: str = "proposed",
    actor: Actor = "agent",
    surface: Surface = "mcp",
    session_id: str | None = None,
    new_id: str | None = None,
) -> dict:
    """Record a corrected fact that supersedes `fact_id`. Always recorded; the
    supersession itself executes now when a human acts (the new fact is
    confirmed in the same step) or when the policy lets the agent and the new
    fact ranks high enough under trust — otherwise it stays pending, the pair
    reads as contested, and the human's confirm executes it."""
    policy = policy_for(workspace)
    evidence = [str(e) for e in (evidence or []) if str(e).strip()]
    _session_evidence(workspace, session_id, evidence)
    store = KnowledgeStore(workspace.knowledge_dir)
    if status not in ("proposed", "data_inferred"):
        raise ValueError("status must be proposed or data_inferred; confirmation is separate")
    fact = store.add_fact(
        table,
        text,
        fact_id=new_id,
        status=status,  # type: ignore[arg-type]
        created_by=actor,
        evidence=evidence,
        supersedes=fact_id,
    )
    fqn = store.read(table)["table"]
    out: dict[str, Any] = {"fact": fact, "supersedes": fact_id, "executed": False}
    if actor != "agent":
        # a human superseding is a human confirming the correction
        out["fact"] = store.confirm_fact(table, fact["id"])
        out["executed"] = True
    elif policy.allows_agent("supersede"):
        try:
            store.execute_supersession(table, fact["id"], by="agent", trust=policy.trust)
            out["executed"] = True
            out["fact"] = store.fact(table, fact["id"])
        except ValueError as e:  # the rank rule: it waits for the human
            out["pending"] = str(e)
    else:
        out["pending"] = policy.refusal("supersede")
    if not out["executed"]:
        out["hint"] = (
            "recorded as a pending supersession — the pair reads as contested until the "
            "user confirms the new fact in the console (or `grayson knowledge confirm`)"
        )
    label = "supersede" if out["executed"] else "propose supersession of"
    sync = _commit(
        workspace,
        fqn,
        f"grayson knowledge: {label} {fact_id} with {fact['id']} on {fqn}",
        actor,
        surface,
    )
    out["library_sync"] = sync
    return out


def restore(
    workspace: Workspace,
    table: str,
    fact_id: str,
    actor: Actor = "agent",
    surface: Surface = "mcp",
) -> dict:
    require(workspace, "restore", actor)
    store = KnowledgeStore(workspace.knowledge_dir)
    fact = store.restore_fact(table, fact_id, by=actor)
    fqn = store.read(table)["table"]
    sync = _commit(workspace, fqn, f"grayson knowledge: restore {fact_id} on {fqn}", actor, surface)
    return {"fact": fact, "library_sync": sync}


def reanchor(
    workspace: Workspace,
    table: str,
    fact_id: str | None = None,
    actor: Actor = "user",
    surface: Surface = "console",
) -> dict:
    """Re-baseline anchors after a definition was re-recorded on purpose —
    the human saying 'these still hold'. Same permission as restore."""
    require(workspace, "restore", actor)
    store = KnowledgeStore(workspace.knowledge_dir)
    out = store.reanchor(table, fact_id)
    what = fact_id or "all facts"
    sync = _commit(
        workspace,
        out["table"],
        f"grayson knowledge: re-anchor {what} on {out['table']}",
        actor,
        surface,
    )
    return {**out, "library_sync": sync}


def dismiss_question(
    workspace: Workspace,
    table: str,
    question: str,
    reason: str,
    actor: Actor = "agent",
    surface: Surface = "mcp",
) -> dict:
    require(workspace, "dismiss_question", actor)
    store = KnowledgeStore(workspace.knowledge_dir)
    out = store.dismiss_question(table, question, reason, by=actor)
    fqn = store.read(table)["table"]
    sync = _commit(
        workspace,
        fqn,
        f"grayson knowledge: dismiss question on {fqn}\n\n{out['question']}\n{reason}",
        actor,
        surface,
    )
    return {**out, "library_sync": sync}


def resolve(
    workspace: Workspace,
    table: str,
    fact_a: str,
    fact_b: str,
    note: str = "",
    actor: Actor = "agent",
    surface: Surface = "mcp",
) -> dict:
    require(workspace, "resolve_contested", actor)
    store = KnowledgeStore(workspace.knowledge_dir)
    out = store.resolve_contested(table, fact_a, fact_b, by=actor, note=note)
    fqn = store.read(table)["table"]
    sync = _commit(
        workspace,
        fqn,
        f"grayson knowledge: {fact_a} and {fact_b} compatible on {fqn}"
        + (f"\n\n{note.strip()}" if note.strip() else ""),
        actor,
        surface,
    )
    return {**out, "library_sync": sync}


def reconcile(
    workspace: Workspace,
    actor: Actor = "agent",
    surface: Surface = "mcp",
    dry_run: bool = False,
) -> dict:
    """The reconcile pass, from a surface: a dry run is always allowed (it
    writes nothing); the real pass is policy-gated for agents."""
    from grayson.library import reconcile_library

    if not dry_run:
        require(workspace, "reconcile", actor)
    return reconcile_library(workspace, dry_run=dry_run)
