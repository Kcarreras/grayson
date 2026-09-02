"""Scoring sandbox sessions against the planted ground truth.

The sandbox plants known problems; a session's findings either name them or
do not. This module turns that comparison into numbers, deterministically:
the same rubric the answer key states for a human — identified, explained,
quantified, one point each per planted problem — applied by pattern to the
findings a session recorded. It is what makes the sandbox an evaluation
harness: run the same workflow under two harnesses, two models, or two
versions of the protocol file, and compare the score and the cost.

Matching is heuristic by necessity (a finding is prose), so every credit and
every miss is reported with what was looked for, never as a bare number. The
ground truth comes from re-running the deterministic seed in memory, so
scoring reads no warehouse and needs no answer-key file.

This is a human's tool. Its output reveals the planted problems, so the CLI
gates it on an interactive terminal like every other user-only action, and
it has no MCP twin: an agent that scores itself mid-run is no longer being
evaluated.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grayson.core.session import Session
    from grayson.workspace import Workspace

#: a number within this fraction of the planted value counts as quantified
TOLERANCE = 0.02

#: dates and timestamps come out before numbers are read, so 2026-07-15 does
#: not contribute 2026, 7, and 15 to a finding's numbers
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")
#: a number not glued to an identifier (q_0003 and f_001 are not numbers)
_NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


@dataclass(frozen=True)
class Problem:
    """One planted problem and how a finding earns each of its three points.

    Term groups are regexes matched case-insensitively; every group must hit
    at least once (a conjunction of disjunctions). `quantities` are planted
    numbers of which any one within TOLERANCE earns the point; `ids` are
    planted keys of which at least half must be named instead.
    """

    id: str
    workflow: str
    tables: tuple[str, ...]
    title: str
    identify: tuple[tuple[str, ...], ...]
    explain: tuple[tuple[str, ...], ...]
    explain_hint: str
    quantify_hint: str
    quantities: tuple[str, ...] = ()  # keys into the truth section
    ids: str | None = None  # key into the truth section
    section: str = field(default="")


def _fq(name: str) -> str:
    from grayson.sandbox.seed import CATALOG, SCHEMA

    return f"{CATALOG}.{SCHEMA}.{name}"


PLANTED: tuple[Problem, ...] = (
    Problem(
        id="customers_null_email",
        workflow="table-health",
        tables=(_fq("CUSTOMERS"),),
        section="customers",
        title="Email NULL regression after the signup cutoff",
        identify=((r"\bemail",), (r"\bnull", r"\bmissing", r"\bblank", r"\bempty")),
        explain=(
            (
                r"2026-07-15",
                r"\b07-15\b",
                r"\bjuly 15",
                r"\bcut-?off",
                r"\bsignup[_ ]date",
                r"\bsign-?up date",
                r"\bonset",
                r"\bsince 2026-07",
                r"\bafter 2026-07",
                r"\bfrom 2026-07",
            ),
        ),
        explain_hint="names the onset: signups on or after 2026-07-15 (a cutoff/date)",
        quantify_hint="the NULL EMAIL row count",
        quantities=("null_email_count",),
    ),
    Problem(
        id="customers_duplicate_ids",
        workflow="table-health",
        tables=(_fq("CUSTOMERS"),),
        section="customers",
        title="Duplicate customer ids with conflicting emails",
        identify=(
            (
                r"\bduplicat",
                r"\bdup\b",
                r"\bdups\b",
                r"\btwice",
                r"\bnon-?unique",
                r"\bnot unique",
                r"\brepeated",
                r"\bmore than once",
            ),  # fmt: skip
            (r"\bcustomer[_ ]ids?\b", r"\bprimary key", r"\bkey\b", r"\bids?\b"),
        ),
        explain=(
            (r"\bconflict", r"\bdiffer", r"\bdistinct emails?", r"\btwo emails?", r"\b\.net\b"),
        ),
        explain_hint="says the duplicates carry conflicting (different) emails",
        quantify_hint="the duplicated ids themselves (101, 202, 303) — at least two of them",
        ids="duplicate_customer_ids",
    ),
    Problem(
        id="customers_future_birthdates",
        workflow="table-health",
        tables=(_fq("CUSTOMERS"),),
        section="customers",
        title="Impossible (future) birthdates",
        identify=(
            (r"\bbirth",),
            (
                r"\bfuture",
                r"\b2031\b",
                r"\bimpossible",
                r"\binvalid",
                r"\bnot yet born",
                r"\bafter today",
                r"\bunborn",
                r"\bnegative age",
            ),  # fmt: skip
        ),
        explain=((r"\b2031\b",),),
        explain_hint="names the year the birthdates fall in (2031)",
        quantify_hint="the affected customer ids (77, 411, 902) — at least two of them",
        ids="future_birthdate_ids",
    ),
    Problem(
        id="orders_join_fanout",
        workflow="bug-hunter",
        tables=(_fq("ORDERS_ENRICHED"),),
        section="orders_enriched",
        title="Join fan-out from re-issued promo codes",
        identify=(
            (
                r"\bduplicat",
                r"\bfan-?\s?out",
                r"\bdoubl",
                r"\btwice",
                r"\binflat",
                r"\bextra rows?",
                r"\bmultipl",
            ),  # fmt: skip
        ),
        explain=(
            (r"\bpromo",),
            (
                r"\bduplicat",
                r"\bnon-?unique",
                r"\bnot unique",
                r"\bre-?issued",
                r"\btwice",
                r"\bsummer25",
                r"\bflash5",
                r"\bmultiple rows?",
                r"\bjoin key",
                r"\bappears? twice",
                r"\bmore than once",
            ),  # fmt: skip
        ),
        explain_hint="blames non-unique promo codes in PROMOS (SUMMER25 / FLASH5 exist twice)",
        quantify_hint="the extra (duplicated) rows, or the orders affected",
        quantities=("extra_rows", "affected_orders"),
    ),
    Problem(
        id="payments_missing_refunded",
        workflow="migration-parity",
        tables=(_fq("PAYMENTS"), _fq("PAYMENTS_V2")),
        section="payments",
        title="Refunded rows dropped by the migration",
        identify=(
            (r"\brefund",),
            (
                r"\bmissing",
                r"\bdrop",
                r"\babsent",
                r"\blost",
                r"\bfilter",
                r"\bexclud",
                r"\bomit",
                r"\bnot (present|migrated|in|copied)",
                r"\bfewer",
                r"\bonly settled",
            ),  # fmt: skip
        ),
        explain=(
            (
                r"\bfilter",
                r"\bwhere\b",
                r"\bstatus\s*=",
                r"\bsettled\b",
                r"\bbackfill",
                r"\bexclud",
                r"\bonly (the )?settled",
            ),  # fmt: skip
        ),
        explain_hint="blames the backfill filter (only STATUS = 'settled' was copied)",
        quantify_hint="the missing row count",
        quantities=("missing_refunded_rows",),
    ),
    Problem(
        id="payments_eur_truncation",
        workflow="migration-parity",
        tables=(_fq("PAYMENTS"), _fq("PAYMENTS_V2")),
        section="payments",
        title="EUR amounts truncated to whole units",
        identify=(
            (r"\beur\b", r"\beuro"),
            (
                r"\btruncat",
                r"\bround",
                r"\binteger",
                r"\bwhole",
                r"\bcast",
                r"\bdrift",
                r"\bmismatch",
                r"\bdiffer",
                r"\bdecimal",
                r"\bfraction",
                r"\bcents?\b",
                r"\bamounts? (do|does) not match",
                r"\bnot equal",
            ),  # fmt: skip
        ),
        explain=(
            (
                r"\btruncat",
                r"\bcast",
                r"\binteger",
                r"\bwhole (unit|number)",
                r"\bfloor",
                r"\bdecimals? (lost|dropped|stripped)",
                r"\bfractional part",
            ),  # fmt: skip
        ),
        explain_hint="says the amounts were truncated/cast to whole units (not rounded noise)",
        quantify_hint="the count of EUR rows whose amounts differ",
        quantities=("eur_amount_mismatches",),
    ),
)


def ground_truth() -> dict:
    """The planted values, recomputed from the deterministic seed in memory."""
    from grayson.sandbox.seed import seed_connection

    con = sqlite3.connect(":memory:")
    try:
        return seed_connection(con)
    finally:
        con.close()


def problems_for(targets: list[str]) -> list[Problem]:
    """Planted problems whose tables the session targets."""
    wanted = {t.upper() for t in targets}
    return [p for p in PLANTED if wanted & set(p.tables)]


# -- matching ----------------------------------------------------------------


def _haystack(finding: dict) -> str:
    payload = finding.get("payload") or {}
    parts = [str(finding.get("title", ""))]
    for key in ("summary", "reproduction", "proposed_remediation"):
        parts.append(str(payload.get(key) or ""))
    parts.append(" ".join(str(o) for o in payload.get("affected_objects") or []))
    parts.append(" ".join(str(q) for q in payload.get("open_questions") or []))
    parts.append(json.dumps(payload.get("extra") or {}, default=str))
    return " ".join(parts).lower()


def _groups_hit(groups: tuple[tuple[str, ...], ...], text: str) -> bool:
    return all(any(re.search(term, text, re.IGNORECASE) for term in group) for group in groups)


def _numbers(text: str) -> set[float]:
    cleaned = _DATE_RE.sub(" ", text)
    out: set[float] = set()
    for m in _NUM_RE.finditer(cleaned):
        try:
            out.add(float(m.group(0).replace(",", "")))
        except ValueError:
            continue
    return out


def _near(found: set[float], expected: float) -> bool:
    if expected <= 0:
        return expected in found
    return any(abs(n - expected) <= TOLERANCE * expected for n in found)


def _quantified(problem: Problem, truth: dict, text: str) -> tuple[bool, list]:
    section = truth[problem.section]
    numbers = _numbers(text)
    if problem.ids:
        ids = [float(i) for i in section[problem.ids]]
        hits = [int(i) for i in ids if i in numbers]
        return len(hits) >= math.ceil(len(ids) / 2), [int(i) for i in ids]
    expected = [float(section[k]) for k in problem.quantities]
    return any(_near(numbers, e) for e in expected), [section[k] for k in problem.quantities]


def score_finding(problem: Problem, truth: dict, finding: dict) -> dict:
    """One finding against one problem: which of the three points it earns."""
    text = _haystack(finding)
    identified = _groups_hit(problem.identify, text)
    explained = identified and _groups_hit(problem.explain, text)
    quantified, expected = _quantified(problem, truth, text) if identified else (False, [])
    return {
        "fid": finding["fid"],
        "identified": identified,
        "explained": explained,
        "quantified": quantified,
        "points": int(identified) + int(explained) + int(quantified),
        "expected": expected,
    }


# -- sessions ----------------------------------------------------------------


def _effort(session: Session) -> dict:
    stats = session.query_stats()
    log = session.query_log(limit=stats["total"] + 1)
    executed_ts = sorted(q["ts"] for q in log if q["status"] == "executed" and q["ts"])
    interventions = session.interventions()
    checkpoints = session.checkpoints()
    from grayson.charts import list_charts

    elapsed = None
    if len(executed_ts) >= 2:
        from datetime import datetime

        try:
            first = datetime.fromisoformat(executed_ts[0].replace("Z", "+00:00"))
            last = datetime.fromisoformat(executed_ts[-1].replace("Z", "+00:00"))
            elapsed = round((last - first).total_seconds())
        except ValueError:
            elapsed = None
    return {
        "queries_executed": stats["by_status"].get("executed", 0),
        "queries_rejected": stats["by_status"].get("rejected", 0),
        "queries_failed": stats["by_status"].get("failed", 0),
        "budget_used": session.budget_consumed_count(),
        "warehouse_ms": stats["executed_duration_ms"],
        "rows_returned": stats["executed_rows"],
        "elapsed_seconds": elapsed,
        "interventions": len(interventions),
        "interventions_open": sum(1 for i in interventions if i["status"] == "open"),
        "checkpoints_complete": sum(1 for c in checkpoints if c["status"] == "complete"),
        "checkpoints_waived": sum(1 for c in checkpoints if c["status"] == "waived"),
        "checkpoints_open": sum(
            1 for c in checkpoints if c["status"] not in ("complete", "waived")
        ),
        "charts": len(list_charts(session)),
    }


def score_session(session: Session, truth: dict | None = None) -> dict:
    """Score one session against the planted problems its targets carry."""
    truth = truth or ground_truth()
    problems = problems_for(session.targets)
    if not problems:
        raise ValueError(
            f"session {session.id} targets {session.targets} — no planted problems there. "
            "Sandbox problems live in SANDBOX.SHOP.CUSTOMERS, ORDERS_ENRICHED, and "
            "PAYMENTS/PAYMENTS_V2."
        )
    findings = session.findings()
    live = [f for f in findings if not f.get("rejected") and not f.get("superseded_by")]
    accepted = {f["fid"] for f in findings if f["accepted"] and not f.get("superseded_by")}
    matched: set[str] = set()
    rows = []
    for problem in problems:
        scored = [score_finding(problem, truth, f) for f in live]
        hits = [s for s in scored if s["identified"]]
        matched.update(s["fid"] for s in hits)
        best = max(hits, key=lambda s: (s["points"], s["fid"] in accepted), default=None)
        rows.append(
            {
                "id": problem.id,
                "title": problem.title,
                "workflow": problem.workflow,
                "identified": bool(best),
                "explained": bool(best and best["explained"]),
                "quantified": bool(best and best["quantified"]),
                "points": best["points"] if best else 0,
                "by": best["fid"] if best else None,
                "accepted": bool(best and best["fid"] in accepted),
                "also": sorted(s["fid"] for s in hits if best and s["fid"] != best["fid"]),
                "looked_for": {
                    "explained": problem.explain_hint,
                    "quantified": problem.quantify_hint,
                    "expected": best["expected"] if best else _quantified(problem, truth, "")[1],
                },
            }
        )
    points = sum(r["points"] for r in rows)
    possible = 3 * len(rows)
    return {
        "session": session.id,
        "title": session.get_meta("title", "") or "",
        "workflow": session.workflow,
        "stage": session.stage,
        "outcome": session.outcome,
        "targets": session.targets,
        "score": {"points": points, "possible": possible},
        "problems": rows,
        "findings": {
            "total": len(findings),
            "accepted": len(accepted),
            "unmatched": [
                {"fid": f["fid"], "title": f["title"], "accepted": f["fid"] in accepted}
                for f in live
                if f["fid"] not in matched
            ],
            "rejected": [
                {"fid": f["fid"], "title": f["title"], "reason": f.get("rejected_reason") or ""}
                for f in findings
                if f.get("rejected")
            ],
        },
        "effort": _effort(session),
    }


def score_workspace(workspace: Workspace) -> list[dict]:
    """Every session in the workspace that targets planted tables, scored."""
    from grayson.core.session import Session

    truth = ground_truth()
    out = []
    for sid in workspace.list_session_ids():
        try:
            s = Session(workspace, sid)
            if not problems_for(s.targets):
                continue
            out.append(score_session(s, truth))
        except (FileNotFoundError, ValueError, OSError):
            continue
    return out


# -- text --------------------------------------------------------------------


def _tick(v: bool) -> str:
    return "✓" if v else "·"


def _n(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def render_score(result: dict) -> str:
    sc = result["score"]
    lines = [
        f"Sandbox score — session {result['session']}"
        + (f" ({result['title']})" if result["title"] else ""),
        f"{result['workflow']} · stage {result['stage']}"
        + (f" · outcome {result['outcome']}" if result["outcome"] else ""),
        f"{sc['points']} / {sc['possible']} points (identified · explained · quantified, "
        "one each per planted problem)",
        "",
    ]
    for p in result["problems"]:
        marks = f"{_tick(p['identified'])} {_tick(p['explained'])} {_tick(p['quantified'])}"
        who = p["by"] or "—"
        if p["by"] and not p["accepted"]:
            who += " (not accepted)"
        lines.append(f"{marks}  {p['points']}/3  {p['title']}  ← {who}")
        if p["identified"] and not p["explained"]:
            lines.append(f"         explained needs: {p['looked_for']['explained']}")
        if p["identified"] and not p["quantified"]:
            lines.append(
                f"         quantified needs: {p['looked_for']['quantified']} "
                f"(planted: {p['looked_for']['expected']}, ±{int(TOLERANCE * 100)}%)"
            )
        if p["also"]:
            lines.append(f"         also named by: {', '.join(p['also'])}")
    f = result["findings"]
    lines.append("")
    lines.append(
        f"findings: {f['total']} recorded, {f['accepted']} accepted, "
        f"{len(f['unmatched'])} matched no planted problem, {len(f['rejected'])} rejected"
    )
    for u in f["unmatched"]:
        flag = "accepted" if u["accepted"] else "not accepted"
        lines.append(f'  ? {u["fid"]} "{u["title"]}" ({flag}) — a false positive, or a real find?')
    for r in f["rejected"]:
        lines.append(f'  ✗ {r["fid"]} "{r["title"]}" rejected: {r["reason"]}')
    e = result["effort"]
    cost = [
        f"{_n(e['queries_executed'], 'query', 'queries')} executed",
        f"{e['queries_rejected']} rejected by the guard",
        f"budget {e['budget_used']}",
        _n(e["interventions"], "intervention"),
        f"checkpoints {e['checkpoints_complete']} complete / {e['checkpoints_waived']} waived / "
        f"{e['checkpoints_open']} open",
        _n(e["charts"], "chart"),
    ]
    if e["elapsed_seconds"] is not None:
        cost.append(f"{e['elapsed_seconds']}s first to last query")
    lines.append("effort: " + " · ".join(cost))
    return "\n".join(lines)


def render_leaderboard(results: list[dict]) -> str:
    """Sessions side by side: the comparison the sandbox exists for."""
    if not results:
        return "no sessions target the planted sandbox tables yet"
    lines = ["session                 score   workflow           queries  budget  interv  title"]
    ordered = sorted(
        results,
        key=lambda r: (-r["score"]["points"] / max(r["score"]["possible"], 1), r["session"]),
    )
    for r in ordered:
        e = r["effort"]
        lines.append(
            f"{r['session']:<23} {r['score']['points']:>2}/{r['score']['possible']:<3}  "
            f"{r['workflow']:<18} {e['queries_executed']:>7}  {e['budget_used']:>6}  "
            f"{e['interventions']:>6}  {r['title']}"
        )
    return "\n".join(lines)
