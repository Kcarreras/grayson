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
actor's action with a reason, and status is provenance.
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
    contested_pairs,
    effective_standing,
)
from grayson.knowledge.store import KnowledgeDocError, KnowledgeStore
from grayson.util import utcnow

_STANDING_FIELDS = ("standing", "standing_reason", "standing_at", "standing_by")


def _norm_question(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().rstrip("?").lower()


def reconcile_docs(
    store: KnowledgeStore,
    records_dir: Path | None,
    policy: Any | None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict:
    """Run the rules over every doc. With `dry_run` nothing is written — the
    report is the same, which is how `library doctor` reads it."""
    ctx = StandingContext.build(records_dir, policy, now=now)
    stamp = utcnow() if now is None else now.strftime("%Y-%m-%dT%H:%M:%SZ")
    materialized: list[dict] = []
    folded: list[dict] = []
    retired_questions: list[dict] = []
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
        "needs_human": needs_human,
        "agent_actions": recent_agent,
        "errors": errors,
        "touched": touched,
        "dry_run": dry_run,
    }
