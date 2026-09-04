"""The knowledge briefing: what a session is told about its tables at start.

The raw doc is the wrong shape for a context window. It lists every fact ever
recorded, every status mixed together, no cap, and a verified fix on a busy
table adds one more line forever. The briefing is the same knowledge ranked
and bounded:

- ranked by standing (current before unverified), then by status (confirmed,
  data-inferred, proposed), then newest first; each fact carries its `role`
  under the policy's trust — knowledge or hypothesis — so the agent is told
  which is which instead of inferring it;
- capped per table at the policy's `briefing_cap`, with the count of the rest
  stated and the tool that fetches them named, exactly as the session brief
  does for queries;
- stale and retired facts hidden and counted, never silently dropped;
- verified-fix facts folded into one line per table pointing at record
  search, since the record already holds the full story;
- contested pairs and recent agent actions surfaced beside the facts, so the
  human's audit and the agent's judgment both have somewhere to start.

Read-only, deterministic, and the same for the CLI, MCP and the console.
"""

from __future__ import annotations

from typing import Any

from grayson.knowledge.policy import STATUS_RANK
from grayson.knowledge.standing import (
    CONTEST_SHARED_COLUMN,
    STANDING_RANK,
    StandingContext,
    annotate_doc,
)
from grayson.knowledge.store import KnowledgeStore

#: characters of a fact shown in the briefing before it is elided
FACT_CHARS = 400
#: open questions listed per table before the rest are counted
QUESTIONS_CAP = 8


def _compact(text: str, limit: int = FACT_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _briefing_fact(f: dict) -> dict:
    out: dict[str, Any] = {
        "id": f["id"],
        "fact": _compact(f.get("fact", "")),
        "status": f.get("status"),
        "role": f.get("role"),
        "standing": f.get("standing"),
        "created_at": f.get("created_at"),
    }
    if f.get("standing") not in (None, "current"):
        out["standing_reason"] = f.get("standing_reason") or ""
    if f.get("confirmed_at"):
        out["confirmed_at"] = f["confirmed_at"]
        out["confirmed_by"] = f.get("confirmed_by")
    if f.get("evidence"):
        out["evidence"] = list(f["evidence"])
    if f.get("supersedes"):
        out["supersedes"] = f["supersedes"]
    if f.get("verified_at"):
        out["verified_at"] = f["verified_at"]
    return out


def _sort(facts: list[dict]) -> list[dict]:
    """Standing first (current before unverified), then status (confirmed,
    data-inferred, proposed), then newest first — a stable sort on a
    newest-first list."""
    newest_first = sorted(facts, key=lambda f: str(f.get("created_at") or ""), reverse=True)
    return sorted(
        newest_first,
        key=lambda f: (
            STANDING_RANK.get(str(f.get("standing") or "current"), 0),
            -STATUS_RANK.get(str(f.get("status")), 0),
        ),
    )


def brief_table(doc: dict, ctx: StandingContext, cap: int) -> dict:
    """The briefing for one (already read) doc."""
    annotated = annotate_doc(doc, ctx)
    facts = annotated["facts"]
    shown = [f for f in facts if f["standing"] in ("current", "unverified")]
    hidden = {
        "stale": sum(1 for f in facts if f["standing"] == "stale"),
        "retired": sum(1 for f in facts if f["standing"] == "retired"),
    }
    fixes = [f for f in shown if f.get("kind") == "verified_fix"]
    shown = [f for f in shown if f.get("kind") != "verified_fix"]
    ranked = _sort(shown)
    listed = ranked[:cap] if cap else ranked
    omitted = len(ranked) - len(listed)
    out: dict[str, Any] = {
        "table": annotated["table"],
        "facts": [_briefing_fact(f) for f in listed],
        "omitted": omitted,
        "hidden": hidden,
        "counts": annotated["standing_counts"],
        "contested": [c for c in annotated["contested"] if c["kind"] != CONTEST_SHARED_COLUMN],
        "agent_actions": annotated["agent_actions"],
    }
    if fixes:
        latest = max(fixes, key=lambda f: str(f.get("created_at") or ""))
        out["verified_fixes"] = {
            "count": len(fixes),
            "latest": {
                "id": latest["id"],
                "fact": _compact(latest.get("fact", "")),
                "created_at": latest.get("created_at"),
                "standing": latest.get("standing"),
            },
            "hint": (
                f"{len(fixes)} verified fix(es) are on record for {annotated['table']} — "
                f"records_search('{annotated['table']}') has each diagnosis and fix in full"
            ),
        }
    questions = [str(q) for q in doc.get("open_questions") or []]
    out["open_questions"] = questions[:QUESTIONS_CAP]
    out["open_questions_omitted"] = max(0, len(questions) - QUESTIONS_CAP)
    return out


def build_briefing(
    store: KnowledgeStore, tables: list[str], ctx: StandingContext, cap: int
) -> dict[str, dict]:
    """Briefings for each target table, keyed by the doc's own table name.
    A table with no doc at all briefs as empty — the caller reports the gap."""
    out: dict[str, dict] = {}
    for t in tables:
        try:
            doc = store.read(t)
        except ValueError as e:  # not an FQN, or an unreadable doc
            out[t.upper()] = {"table": t.upper(), "facts": [], "error": str(e)}
            continue
        out[doc["table"]] = brief_table(doc, ctx, cap)
    return out


def briefing_hints(briefings: dict[str, dict], fetch: str = "knowledge_show") -> list[str]:
    """The lines a session start appends to its hint: what was left out and
    what needs a reading."""
    hints: list[str] = []
    omitted = {t: b["omitted"] for t, b in briefings.items() if b.get("omitted")}
    if omitted:
        parts = ", ".join(f"{t} ({n})" for t, n in omitted.items())
        hints.append(
            f"knowledge is ranked and capped per table — {parts} more fact(s) not shown; "
            f"{fetch} lists every fact with its standing"
        )
    contested = {t: len(b["contested"]) for t, b in briefings.items() if b.get("contested")}
    if contested:
        parts = ", ".join(f"{t} ({n})" for t, n in contested.items())
        hints.append(
            f"contested knowledge on {parts}: two facts disagree or one proposes to "
            "supersede another (knowledge_briefing.<table>.contested). Read both; if the "
            "evidence settles it, record the corrected fact with knowledge_supersede — "
            "otherwise say in your findings that both hold, and why"
        )
    unverified = {
        t: sum(1 for f in b.get("facts", []) if f.get("standing") == "unverified")
        for t, b in briefings.items()
    }
    unverified = {t: n for t, n in unverified.items() if n}
    if unverified:
        parts = ", ".join(f"{t} ({n})" for t, n in unverified.items())
        hints.append(
            f"unverified facts on {parts}: something they rest on changed since they were "
            "recorded (each carries its standing_reason). Treat them as leads to re-check, "
            "not as settled; a fact that still holds can be restored, one that does not "
            "can be retired or superseded with evidence"
        )
    hidden = {
        t: b["hidden"]["stale"] for t, b in briefings.items() if b.get("hidden", {}).get("stale")
    }
    if hidden:
        parts = ", ".join(f"{t} ({n})" for t, n in hidden.items())
        hints.append(
            f"stale facts hidden on {parts} (a column they name was dropped, or their "
            f"record is gone) — {fetch} shows them if the history matters"
        )
    return hints
