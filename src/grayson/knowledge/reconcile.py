"""The reconcile pass: the library's unattended housekeeping.

Everything here is a rule, not a judgment. It materializes each fact's
standing onto its doc (so hand readers and git diffs see what the rules
already say), folds duplicate open questions, retires questions about columns
the warehouse dropped, and reports what it could not decide — contested pairs,
unverified facts, the stale — as the queue a human or a permitted agent works.
It needs no warehouse, so it runs from CI on the library repo as well as from
a workspace, and lands as one commit with a `Grayson-Via: reconcile` trailer
(docs/LIBRARY.md, "Standing, pruning, and the knowledge policy").

It never retires a fact by rule and never touches status: retiring is an
actor's action with a reason, and status is provenance. The one write it makes
to a fact's standing beyond materializing the rules is executing a
supersession a human already confirmed (a confirm done by an older grayson,
or by hand) — the decision was the human's; the pass only records it.

With `anchor_missing` it also anchors facts written before anchors existed:
column and definition anchors from the doc as it stands, and for a verified-
fix fact the record its id encodes, when that record is still in the library.
An anchor set this way says "flag this fact if these change from here on",
which is the honest claim about a fact nobody re-verified; the stamp
(`anchored_by: reconcile`) says it was baselined, not recorded, at that time.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from grayson.knowledge.standing import (
    StandingContext,
    agent_actions,
    column_mentions,
    confirmed_successor,
    contested_pairs,
    derive_anchors,
    effective_standing,
    legacy_fix_record,
)
from grayson.knowledge.store import KnowledgeDocError, KnowledgeStore, apply_supersession
from grayson.util import utcnow

_STANDING_FIELDS = ("standing", "standing_reason", "standing_at", "standing_by")


def _norm_question(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().rstrip("?").lower()


def _anchor_fact(f: dict, doc: dict, ctx: StandingContext, stamp: str) -> dict | None:
    """Anchor one unanchored fact from the doc as it stands. Returns what was
    anchored, or None when the doc offers nothing to anchor it to."""
    record = legacy_fix_record(str(f.get("id", "")))
    keep: list[dict] = []
    record_found = False
    if record is not None:
        sid, pid = record
        # only a record still in the library is worth pointing at: an anchor
        # to one that is gone would read as stale on an unverified premise
        if f"{sid}/{pid}" in ctx.records:
            keep.append({"kind": "record", "session": sid, "id": pid, "record_kind": "proposal"})
            record_found = True
    anchors = derive_anchors(str(f.get("fact", "")), doc, keep=keep)
    kind_set = record is not None and not f.get("kind")
    if not anchors and not kind_set:
        return None
    f["anchors"] = anchors
    if kind_set:
        f["kind"] = "verified_fix"
    f["anchored_by"] = "reconcile"
    f["anchored_at"] = stamp
    return {
        "fact_id": f["id"],
        "anchors": len(anchors),
        "record": record_found,
        "legacy_fix": record is not None,
    }


def reconcile_docs(
    store: KnowledgeStore,
    records_dir: Path | None,
    policy: Any | None,
    dry_run: bool = False,
    now: datetime | None = None,
    anchor_missing: bool = False,
) -> dict:
    """Run the rules over every doc. With `dry_run` nothing is written — the
    report is the same, which is how `library doctor` reads it. With
    `anchor_missing`, facts that carry no anchors are anchored first (the
    upgrade pass for a library written before standing existed)."""
    ctx = StandingContext.build(records_dir, policy, now=now)
    stamp = utcnow() if now is None else now.strftime("%Y-%m-%dT%H:%M:%SZ")
    materialized: list[dict] = []
    folded: list[dict] = []
    retired_questions: list[dict] = []
    executed: list[dict] = []
    anchored: list[dict] = []
    unanchorable = 0
    errors: list[dict] = []
    touched: list[str] = []
    needs_human: dict[str, dict] = {}
    recent_agent: dict[str, list] = {}
    counts = {"current": 0, "unverified": 0, "stale": 0, "retired": 0}
    checked = 0
    for fqn in store.all_tables():
        try:
            doc = store.read(fqn)
        except (KnowledgeDocError, ValueError) as e:
            errors.append({"table": fqn, "error": str(e)})
            continue
        checked += 1
        changed = False
        table_queue: dict[str, list] = {"unverified": [], "stale": []}
        annotated_facts: list[dict] = []
        # a supersession a human confirmed but nothing executed: record it
        for f in doc["facts"]:
            if f.get("superseded_by") or f.get("standing") == "retired":
                continue
            successor = confirmed_successor(f, doc)
            if successor is None:
                continue
            apply_supersession(f, successor["id"], "reconcile", stamp)
            f["standing_reason"] = (
                f"superseded by {successor['id']} (confirmed by "
                f"{successor.get('confirmed_by') or 'a user'})"
            )
            executed.append({"table": doc["table"], "fact_id": f["id"], "by": successor["id"]})
            changed = True
        if anchor_missing:
            for f in doc["facts"]:
                if f.get("anchors") or f.get("superseded_by") or f.get("standing") == "retired":
                    continue
                result = _anchor_fact(f, doc, ctx, stamp)
                if result is None:
                    unanchorable += 1
                    continue
                anchored.append({"table": doc["table"], **result})
                changed = True
        for f in doc["facts"]:
            standing, reason = effective_standing(f, doc, ctx)
            counts[standing] += 1
            annotated_facts.append({**f, "standing": standing, "standing_reason": reason})
            if standing == "retired":
                continue
            if standing == "current":
                if f.get("standing") in ("stale", "unverified"):
                    materialized.append(
                        {
                            "table": doc["table"],
                            "fact_id": f["id"],
                            "from": f["standing"],
                            "to": "current",
                        }
                    )
                    for key in _STANDING_FIELDS:
                        f[key] = None
                    changed = True
                continue
            table_queue[standing].append({"fact_id": f["id"], "reason": reason})
            if f.get("standing") != standing or (f.get("standing_reason") or "") != reason:
                materialized.append(
                    {
                        "table": doc["table"],
                        "fact_id": f["id"],
                        "from": f.get("standing") or "current",
                        "to": standing,
                        "reason": reason,
                    }
                )
                f["standing"] = standing
                f["standing_reason"] = reason
                f["standing_at"] = stamp
                f["standing_by"] = "reconcile"
                changed = True
        # open questions: fold exact duplicates, retire those about dropped columns
        questions = [str(q) for q in doc.get("open_questions") or []]
        kept: list[str] = []
        seen: set[str] = set()
        dropped_cols = [c for c in doc.get("columns") or [] if c.get("dropped")]
        for q in questions:
            key = _norm_question(q)
            if key in seen:
                folded.append({"table": doc["table"], "question": q})
                changed = True
                continue
            seen.add(key)
            about = column_mentions(q, [{"name": c["name"]} for c in dropped_cols])
            if about:
                entry = {
                    "question": q,
                    "reason": f"column {', '.join(about)} was dropped from the warehouse",
                    "by": "reconcile",
                    "at": stamp,
                }
                doc.setdefault("retired_questions", []).append(entry)
                retired_questions.append({"table": doc["table"], **entry})
                changed = True
                continue
            kept.append(q)
        if changed:
            doc["open_questions"] = kept
            if not dry_run:
                store.save(fqn, doc)
            touched.append(str(store.table_path(fqn).relative_to(store.dir.parent)))
        contested = contested_pairs({**doc, "facts": annotated_facts})
        if contested or table_queue["unverified"] or table_queue["stale"]:
            needs_human[doc["table"]] = {"contested": contested, **table_queue}
        recent = agent_actions(doc, ctx)
        if recent:
            recent_agent[doc["table"]] = recent
    return {
        "checked": checked,
        "counts": counts,
        "materialized": materialized,
        "questions_folded": folded,
        "questions_retired": retired_questions,
        "supersessions_executed": executed,
        "anchored": anchored,
        "unanchorable": unanchorable,
        "needs_human": needs_human,
        "agent_actions": recent_agent,
        "errors": errors,
        "touched": touched,
        "dry_run": dry_run,
        "anchor_missing": anchor_missing,
    }
