"""Standing: whether what a fact rests on still holds.

A fact's *status* says who vouches for it (proposed, data_inferred,
user_confirmed). Its *standing* says whether the world has moved since:

- ``current`` — nothing it rests on has changed
- ``unverified`` — something it rests on changed and nobody has looked
  (a definition's hash moved; a proposed fact sat unconfirmed past the horizon)
- ``stale`` — something it rests on is gone (a column it names was dropped,
  the record it came from was superseded or removed)
- ``retired`` — someone retired it, or a confirmed successor superseded it

The two axes are orthogonal: a user-confirmed fact can be stale. Standing is
derived from *anchors* — the things that would falsify the fact — which the
store records at write time from what it can already see (column names in the
text, the table's definition hashes, the record a verified fix came from).
Everything here is deterministic and reads only library files, so a
knowledge-only server computes it for free; ``retired`` is the one sticky
state, set by an actor and cleared only by a restore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STANDINGS: tuple[str, ...] = ("current", "unverified", "stale", "retired")
STANDING_RANK: dict[str, int] = {"current": 0, "unverified": 1, "stale": 2, "retired": 3}

#: the fact fields this module owns; written to disk only when set, so a fact
#: that has never been touched by a lifecycle action looks exactly as before
LIFECYCLE_KEYS: tuple[str, ...] = (
    "standing",
    "standing_reason",
    "standing_at",
    "standing_by",
    "anchors",
    "supersedes",
    "superseded_by",
    "retired_by",
    "retired_at",
    "restored_by",
    "restored_at",
    "kind",
    "compatible_with",
    "verified_at",
)

#: contested-pair kinds, from strongest signal to weakest
CONTEST_SUPERSESSION = "supersession"  # a fact proposes to supersede another; pending
CONTEST_SAME_QUESTION = "same_question"  # two facts answer one open question
CONTEST_SHARED_COLUMN = "shared_column"  # same column, mixed provenance (informational)

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def parse_ts(value: object) -> datetime | None:
    """A grayson timestamp (`utcnow()` shape) as an aware datetime; None if not."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def age_days(value: object, now: datetime) -> float | None:
    then = parse_ts(value)
    if then is None:
        return None
    return (now - then).total_seconds() / 86400.0


# -- anchors -----------------------------------------------------------------


def column_mentions(text: str, columns: list[dict]) -> list[str]:
    """Recorded (non-dropped) column names the text mentions as whole words,
    in the doc's order — the anchors that make 'dropped' falsify the fact."""
    if not text or not columns:
        return []
    words = {w.upper() for w in _WORD.findall(text)}
    out: list[str] = []
    for col in columns:
        if col.get("dropped"):
            continue
        name = str(col.get("name") or "")
        if name and name.upper() in words:
            out.append(name)
    return out


def derive_anchors(text: str, doc: dict, keep: list[dict] | None = None) -> list[dict]:
    """Anchors for a fact on this doc, from what the doc records now: one
    ``column`` per mentioned column, one ``definition`` per hashed definition
    entry, plus whatever the caller passes in `keep` (record anchors survive a
    re-anchor; duplicates fold)."""
    out: list[dict] = []
    seen: set[str] = set()

    def _add(anchor: dict) -> None:
        key = _anchor_key(anchor)
        if key in seen:
            return
        seen.add(key)
        out.append(anchor)

    for a in keep or []:
        if isinstance(a, dict) and a.get("kind") and a.get("kind") not in ("column", "definition"):
            _add(dict(a))
    for name in column_mentions(text, doc.get("columns") or []):
        _add({"kind": "column", "name": name})
    for d in doc.get("definitions") or []:
        key = d.get("path") or d.get("snapshot")
        if key and d.get("hash"):
            _add({"kind": "definition", "key": str(key), "hash": str(d["hash"])})
    return out


def _anchor_key(anchor: dict) -> str:
    kind = anchor.get("kind")
    if kind == "column":
        return f"column:{str(anchor.get('name', '')).upper()}"
    if kind == "definition":
        return f"definition:{anchor.get('key')}"
    if kind == "record":
        return f"record:{anchor.get('session')}/{anchor.get('id')}"
    return f"{kind}:{sorted(anchor.items())}"


# -- context ------------------------------------------------------------------


@dataclass
class StandingContext:
    """What standing is computed against: the library's records index (for
    record anchors), the clock, the policy's horizon and trust, and — at
    session start — the warehouse's live column lists, so a column anchor is
    judged against the DESCRIBE that just ran rather than the last sync."""

    records: dict[str, dict] = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    proposed_horizon_days: int = 90
    trust: str = "data_inferred"
    agent_window_days: int = 30
    live_columns: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        records_dir: Path | None,
        policy: Any | None = None,
        live_columns: dict[str, list[dict]] | None = None,
        now: datetime | None = None,
    ) -> StandingContext:
        ctx = cls(records=records_index(records_dir) if records_dir else {})
        if now is not None:
            ctx.now = now
        if policy is not None:
            ctx.proposed_horizon_days = int(policy.proposed_horizon_days)
            ctx.trust = str(policy.trust)
            ctx.agent_window_days = int(policy.agent_window_days)
        for table, cols in (live_columns or {}).items():
            ctx.live_columns[table.upper()] = {str(c.get("name", "")).upper() for c in cols}
        return ctx

    def admits(self, status: str) -> bool:
        from grayson.knowledge.policy import STATUS_RANK

        return STATUS_RANK.get(status, -1) >= STATUS_RANK.get(self.trust, 1)


def records_index(records_dir: Path) -> dict[str, dict]:
    """Every published record by `session/id`, reduced to what standing needs."""
    from grayson.records import library_records

    out: dict[str, dict] = {}
    for r in library_records(records_dir):
        key = f"{r.get('session_id')}/{r.get('id')}"
        out[key] = {
            "kind": r.get("kind"),
            "superseded_by": r.get("superseded_by"),
            "verdict": r.get("verdict"),
            "status": r.get("status"),
            "title": r.get("title", ""),
        }
    return out


# -- the rules ---------------------------------------------------------------


def effective_standing(fact: dict, doc: dict, ctx: StandingContext) -> tuple[str, str]:
    """(standing, reason) for one fact, from the rules — retired is sticky,
    everything else is recomputed from the anchors each time."""
    if fact.get("standing") == "retired" or fact.get("superseded_by"):
        reason = fact.get("standing_reason") or (
            f"superseded by {fact['superseded_by']}" if fact.get("superseded_by") else "retired"
        )
        return "retired", str(reason)
    if fact.get("standing") == "unverified" and fact.get("standing_by") == "verify":
        # a re-run that disagreed with the record is an observation, not a rule:
        # it stands until the next verify agrees or someone restores the fact
        return "unverified", str(fact.get("standing_reason") or "re-run differed")
    table = str(doc.get("table", "")).upper()
    dropped = {str(c.get("name", "")).upper() for c in doc.get("columns") or [] if c.get("dropped")}
    live = ctx.live_columns.get(table)
    definitions = {str(d.get("path") or d.get("snapshot")): d for d in doc.get("definitions") or []}
    unverified: list[str] = []
    for anchor in fact.get("anchors") or []:
        kind = anchor.get("kind")
        if kind == "column":
            name = str(anchor.get("name", ""))
            if name.upper() in dropped:
                return "stale", f"column {name} was dropped from the warehouse"
            if live is not None and name.upper() not in live:
                return "stale", f"column {name} is no longer in the warehouse (live DESCRIBE)"
        elif kind == "record":
            key = f"{anchor.get('session')}/{anchor.get('id')}"
            rec = ctx.records.get(key)
            if ctx.records and rec is None:
                return "stale", f"record {key} is no longer in the library"
            if rec and rec.get("superseded_by"):
                return "stale", f"record {key} was superseded by {rec['superseded_by']}"
        elif kind == "definition":
            key = str(anchor.get("key", ""))
            current = definitions.get(key)
            if current is None:
                unverified.append(f"definition {key} is no longer recorded")
            elif current.get("hash") and str(current["hash"]) != str(anchor.get("hash", "")):
                unverified.append(f"definition {key} changed since this fact was recorded")
    if unverified:
        return "unverified", "; ".join(unverified)
    if fact.get("status") == "proposed" and not fact.get("confirmed_at"):
        age = age_days(fact.get("created_at"), ctx.now)
        horizon = ctx.proposed_horizon_days
        if horizon and age is not None and age > horizon:
            return "unverified", f"proposed {int(age)} days ago and never confirmed"
    return "current", ""


def annotate_fact(fact: dict, doc: dict, ctx: StandingContext) -> dict:
    """A copy of the fact carrying its effective standing and its role in a
    briefing (knowledge or hypothesis, by the trust setting)."""
    standing, reason = effective_standing(fact, doc, ctx)
    out = dict(fact)
    out["standing"] = standing
    out["standing_reason"] = reason
    out["role"] = "knowledge" if ctx.admits(str(fact.get("status", ""))) else "hypothesis"
    return out


def annotate_doc(doc: dict, ctx: StandingContext) -> dict:
    """The doc with every fact annotated, plus the derived views a reader
    wants beside them: standing counts, contested pairs, recent agent actions."""
    out = dict(doc)
    out["facts"] = [annotate_fact(f, doc, ctx) for f in doc.get("facts") or []]
    counts = dict.fromkeys(STANDINGS, 0)
    for f in out["facts"]:
        counts[f["standing"]] += 1
    out["standing_counts"] = counts
    out["contested"] = contested_pairs(out)
    out["agent_actions"] = agent_actions(out, ctx)
    return out


# -- contested pairs -----------------------------------------------------------


def _question_key(text: str) -> str | None:
    """The question part of an answer fact (`answer_open_question` writes
    `<question>? — <answer>`), normalized; None for any other fact."""
    if "? — " not in text:
        return None
    head = text.split("? — ", 1)[0]
    key = re.sub(r"\s+", " ", head).strip().lower()
    return key or None


def contested_pairs(doc: dict) -> list[dict]:
    """Pairs that need a reading grayson cannot give: a pending supersession,
    two answers to one question, or (weakest, informational) two facts on
    one column with mixed provenance. Facts already retired or stale are out,
    and so is any pair a human (or a permitted agent) marked compatible."""
    facts = [
        f
        for f in doc.get("facts") or []
        if f.get("standing", "current") in ("current", "unverified")
    ]
    by_id = {f["id"]: f for f in facts}
    compatible = {
        frozenset((f["id"], other)) for f in facts for other in f.get("compatible_with") or []
    }
    pairs: list[dict] = []
    seen: set[frozenset] = set()

    def _add(kind: str, a: dict, b: dict, why: str) -> None:
        key = frozenset((a["id"], b["id"]))
        if key in seen or key in compatible or a["id"] == b["id"]:
            return
        seen.add(key)
        pairs.append(
            {
                "kind": kind,
                "facts": [a["id"], b["id"]],
                "why": why,
                "statuses": [a.get("status"), b.get("status")],
            }
        )

    for f in facts:
        target = f.get("supersedes")
        if target and target in by_id and not by_id[target].get("superseded_by"):
            _add(
                CONTEST_SUPERSESSION,
                f,
                by_id[target],
                f"{f['id']} proposes to supersede {target}; the supersession has not "
                "executed (a human confirms the new fact, or the policy lets the agent)",
            )
    by_question: dict[str, list[dict]] = {}
    for f in facts:
        key = _question_key(str(f.get("fact", "")))
        if key:
            by_question.setdefault(key, []).append(f)
    for key, group in by_question.items():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                _add(CONTEST_SAME_QUESTION, a, b, f"both answer the question {key!r}")
    by_column: dict[str, list[dict]] = {}
    for f in facts:
        for anchor in f.get("anchors") or []:
            if anchor.get("kind") == "column":
                by_column.setdefault(str(anchor.get("name", "")).upper(), []).append(f)
    for name, group in by_column.items():
        confirmed = [f for f in group if f.get("status") == "user_confirmed"]
        others = [f for f in group if f.get("status") != "user_confirmed"]
        for a in confirmed:
            for b in others:
                _add(
                    CONTEST_SHARED_COLUMN,
                    a,
                    b,
                    f"both concern column {name}; one is confirmed, the other is not",
                )
    return pairs


# -- agent actions ---------------------------------------------------------------


def agent_actions(doc: dict, ctx: StandingContext) -> list[dict]:
    """Lifecycle actions agents took on this doc within the policy's window —
    the audit hook that replaces pre-approval: what an agent retired,
    restored, dismissed or resolved recently, with its reason."""
    window = ctx.agent_window_days
    out: list[dict] = []

    def _recent(value: object) -> bool:
        age = age_days(value, ctx.now)
        return age is not None and age <= window

    for f in doc.get("facts") or []:
        if f.get("retired_by") == "agent" and _recent(f.get("retired_at")):
            out.append(
                {
                    "kind": "retired",
                    "fact_id": f["id"],
                    "at": f.get("retired_at"),
                    "reason": f.get("standing_reason") or "",
                    "evidence": list(f.get("evidence") or []),
                }
            )
        if f.get("restored_by") == "agent" and _recent(f.get("restored_at")):
            out.append(
                {"kind": "restored", "fact_id": f["id"], "at": f.get("restored_at"), "reason": ""}
            )
    for q in doc.get("retired_questions") or []:
        if q.get("by") == "agent" and _recent(q.get("at")):
            out.append(
                {
                    "kind": "dismissed_question",
                    "question": q.get("question", ""),
                    "at": q.get("at"),
                    "reason": q.get("reason", ""),
                }
            )
    for r in doc.get("resolutions") or []:
        if r.get("by") == "agent" and _recent(r.get("at")):
            out.append(
                {
                    "kind": "resolved",
                    "facts": list(r.get("facts") or []),
                    "at": r.get("at"),
                    "reason": r.get("note", ""),
                }
            )
    out.sort(key=lambda a: str(a.get("at") or ""), reverse=True)
    return out


# -- on-disk shape -------------------------------------------------------------------


def fact_for_disk(fact: dict) -> dict:
    """The fact as written: lifecycle fields only when they carry something,
    and never a computed `standing: current` or the briefing's `role` — so a
    doc nobody touched with a lifecycle action diffs as before."""
    out = {k: v for k, v in fact.items() if k not in LIFECYCLE_KEYS and k != "role"}
    for key in LIFECYCLE_KEYS:
        value = fact.get(key)
        if key == "standing" and value in (None, "current"):
            continue
        if value in (None, "", [], {}):
            continue
        out[key] = value
    return out
