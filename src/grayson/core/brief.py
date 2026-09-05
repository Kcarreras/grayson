"""The session brief: everything a session already knows, in one read.

An agent's context window ends; the session does not. A harness restarted
mid-investigation — a new chat, a compacted context, a second worker joining
late — has the session id and nothing else, and the protocol it would
otherwise follow is to re-derive the state from six list commands, or worse,
to re-run the queries. The brief is the one call that replaces that: the
identity and setup answers, every checkpoint with its evidence, every
finding with the user's verdict, every intervention with the user's answer
(the durable facts an agent restart loses first), proposals with their
verification, the recent query log, the charts, the narrative draft, and
readiness's next action. It is assembled from the record, never from prose;
it is read-only; and it is the same for the CLI and MCP.
"""

from __future__ import annotations

import json
from pathlib import Path

from grayson.core import engine
from grayson.core.session import Session

#: executed queries listed in full (newest first); the count of the rest is stated
RECENT_QUERIES = 20
#: characters of a statement shown per query — enough to recognise it, not re-run it
SQL_CHARS = 160
#: characters of an intervention response shown inline
RESPONSE_CHARS = 400


def _finding_status(f: dict) -> str:
    if f.get("superseded_by"):
        return f"superseded by {f['superseded_by']}"
    if f.get("rejected"):
        return "rejected"
    if f.get("accepted"):
        return "accepted"
    return "pending the user's accept/reject"


def _compact(value: object, limit: int) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_brief(session: Session, workflows_dir: Path | None = None) -> dict:
    """Assemble the brief from the session record."""
    from grayson.charts import list_charts

    summary = session.summary()
    ready = engine.readiness(session, workflows_dir)
    guard = summary["guard"]
    consumed = session.budget_consumed_count()
    stats = session.query_stats()
    executed = session.query_log(limit=RECENT_QUERIES, status="executed")
    interventions = session.interventions()
    checkpoints = engine.checkpoints_view(session, workflows_dir)
    findings = session.findings()

    return {
        "id": session.id,
        "title": summary["title"],
        "workflow": summary["workflow"],
        "stage": summary["stage"],
        "outcome": summary["outcome"],
        "outcome_note": summary["outcome_note"],
        "created_at": summary["created_at"],
        "connection": summary["connection"],
        "targets": summary["targets"],
        "regression_runs": [e["payload"] for e in session.events(20, event_type="regression_run")],
        "scope_extra": summary["scope_extra"],
        "strict_scope": summary["strict_scope"],
        "guard": {
            "profile": summary["guard_profile"],
            "auto_limit": guard.get("auto_limit"),
            "timeout_seconds": guard.get("timeout_seconds"),
            "budget_used": consumed,
            "budget_warn": guard.get("budget_warn"),
            "budget_cap": guard.get("budget_cap"),
        },
        "workers": summary["workers"],
        "setup_inputs": session.setup_inputs(),
        "checkpoints": [
            {
                "key": c["key"],
                "title": c["title"],
                "status": c["status"],
                "evidence": c["evidence"],
                "evidence_off_scope": c["evidence_off_scope"],
                "charts": c.get("charts") or [],
                "requires_charts": c.get("requires_charts") or [],
                "note": c["note"] or "",
                "by": c["completed_by"],
            }
            for c in checkpoints
        ],
        "suggested_checks": ready["suggested_checks"],
        "findings": [
            {
                "fid": f["fid"],
                "title": f["title"],
                "severity": f["severity"],
                "confidence": f["confidence"],
                "status": _finding_status(f),
                "rejected_reason": f.get("rejected_reason") or "",
                "affected_objects": f["payload"].get("affected_objects") or [],
                "evidence": f["payload"].get("evidence") or [],
                "summary": f["payload"].get("summary") or "",
            }
            for f in findings
        ],
        "interventions": [
            {
                "iid": i["iid"],
                "kind": i["kind"],
                "status": i["status"],
                "title": i["title"],
                "prompt": i["prompt"],
                "response": i.get("response"),
                "responded_at": i.get("responded_at"),
            }
            for i in interventions
        ],
        "proposals": [
            {
                "pid": p["pid"],
                "kind": p["kind"],
                "title": p["title"],
                "status": p["status"],
                "finding": p.get("finding_fid"),
                "verification": (
                    {
                        "verdict": p["verification"].get("verdict"),
                        "before": p["verification"].get("before_qid"),
                        "after": p["verification"].get("after_qid"),
                    }
                    if p.get("verification")
                    else None
                ),
            }
            for p in session.proposals()
        ],
        "queries": {
            "executed": stats["by_status"].get("executed", 0),
            "rejected_by_guard": stats["by_status"].get("rejected", 0),
            "failed": stats["by_status"].get("failed", 0),
            "recent": [
                {
                    "qid": q["qid"],
                    "label": q["label"] or "",
                    "tables": json.loads(q["tables_json"]) if q.get("tables_json") else [],
                    "row_count": q["row_count"],
                    "truncated": bool(q.get("truncated")),
                    "sql": _compact(q["sql_raw"] or "", SQL_CHARS),
                }
                for q in executed[:RECENT_QUERIES]
            ],
        },
        "charts": [
            {"chart_id": c["chart_id"], "kind": c["kind"], "title": c["title"], "qid": c["qid"]}
            for c in list_charts(session)
        ],
        "narrative": session.get_meta("report_narrative", "") or "",
        "readiness": {
            "open_checks": ready["open_checks"],
            "waived_checks": [w["key"] for w in ready["waived_checks"]],
            "findings_pending": ready["findings_pending"],
            "clean_close_available": ready["clean_close_available"],
            "next_action": ready["next_action"],
        },
    }


# -- text -------------------------------------------------------------------

_CHECK_MARK = {"complete": "[x]", "waived": "[~]"}


def render_brief(brief: dict) -> str:
    """The brief as a page an agent (or a person) reads top to bottom."""
    g = brief["guard"]
    lines = [f"# Session {brief['id']}" + (f" — {brief['title']}" if brief["title"] else "")]
    head = [
        f"workflow {brief['workflow']}",
        f"stage {brief['stage']}",
        f"connection {brief['connection']}",
    ]
    if brief["outcome"]:
        head.append(f"outcome {brief['outcome']}")
    lines.append(" · ".join(head))
    scope = ", ".join(brief["targets"]) or "(none)"
    if brief["scope_extra"]:
        scope += f"  (+ granted scope: {', '.join(brief['scope_extra'])})"
    lines.append(f"targets: {scope}" + ("  · strict scope" if brief["strict_scope"] else ""))
    budget = f"{g['budget_used']} used"
    if g.get("budget_cap"):
        budget += f" of {g['budget_cap']} cap"
    elif g.get("budget_warn"):
        budget += f" (warn at {g['budget_warn']})"
    lines.append(
        f"guard: profile {g['profile'] or '-'} · auto-limit {g['auto_limit'] or 'off'} · "
        f"timeout {str(g['timeout_seconds']) + 's' if g['timeout_seconds'] else 'off'} · "
        f"budget {budget}"
    )
    if brief["workers"]:
        lines.append(
            "workers: "
            + ", ".join(
                f"{w['id']}{' (' + w['label'] + ')' if w['label'] else ''}"
                for w in brief["workers"]
            )
        )
    if brief["setup_inputs"]:
        lines += ["", "## Setup inputs (the user's answers)"]
        lines += [f"- {k}: {_compact(v, 300)}" for k, v in brief["setup_inputs"].items()]

    lines += ["", "## Checkpoints"]
    for c in brief["checkpoints"]:
        mark = _CHECK_MARK.get(c["status"], "[ ]")
        detail = c["status"]
        if c["evidence"]:
            detail += f" ({', '.join(c['evidence'])})"
        if c["evidence_off_scope"]:
            detail += f" off-scope: {', '.join(c['evidence_off_scope'])}"
        if c.get("charts"):
            detail += f" charts: {', '.join(c['charts'])}"
        if c["status"] == "open" and c.get("requires_charts"):
            detail += " — requires chart(s): " + "; ".join(c["requires_charts"])
        if c["note"]:
            detail += f" — {_compact(c['note'], 200)}"
        lines.append(f"- {mark} {c['key']}: {detail}")
    if brief["suggested_checks"]:
        done = [s["key"] for s in brief["suggested_checks"] if s["done"]]
        todo = [s["key"] for s in brief["suggested_checks"] if not s["done"]]
        lines.append(
            f"suggested (gate nothing): done {', '.join(done) or '-'} · "
            f"not done {', '.join(todo) or '-'}"
        )

    lines += ["", "## Findings"]
    if not brief["findings"]:
        lines.append("- none recorded yet")
    for f in brief["findings"]:
        line = f'- {f["fid"]} {f["severity"]}/{f["confidence"]} "{f["title"]}" — {f["status"]}'
        if f["rejected_reason"]:
            line += f": {_compact(f['rejected_reason'], 200)}"
        if f["evidence"]:
            line += f" (evidence {', '.join(f['evidence'])})"
        lines.append(line)

    lines += ["", "## Interventions (the user's answers are facts — do not re-ask)"]
    if not brief["interventions"]:
        lines.append("- none")
    for i in brief["interventions"]:
        if i["status"] == "answered":
            lines.append(
                f'- {i["iid"]} answered ({i["kind"]}) "{i["title"]}" → '
                f"{_compact(i['response'] or {}, RESPONSE_CHARS)}"
            )
        elif i["status"] == "open":
            lines.append(
                f'- {i["iid"]} OPEN ({i["kind"]}) "{i["title"]}" — awaiting the user: '
                f"{_compact(i['prompt'], 200)}"
            )
        else:
            lines.append(f'- {i["iid"]} {i["status"]} ({i["kind"]}) "{i["title"]}"')

    if brief["proposals"]:
        lines += ["", "## Proposals"]
        for p in brief["proposals"]:
            line = f'- {p["pid"]} {p["kind"]} "{p["title"]}" — {p["status"]}'
            if p["finding"]:
                line += f" (for {p['finding']})"
            if p["verification"]:
                v = p["verification"]
                line += f"; verified {v['verdict']} ({v['before']} → {v['after']})"
            lines.append(line)

    q = brief["queries"]
    shown = len(q["recent"])
    header = f"## Queries — {q['executed']} executed"
    extras = []
    if q["rejected_by_guard"]:
        extras.append(f"{q['rejected_by_guard']} rejected by the guard")
    if q["failed"]:
        extras.append(f"{q['failed']} failed")
    if extras:
        header += f", {', '.join(extras)}"
    if shown and shown < q["executed"]:
        header += f" (newest {shown} shown)"
    lines += ["", header]
    for r in q["recent"]:
        if r["row_count"] is None:
            rows = "- rows"
        else:
            rows = f"{r['row_count']}{'+' if r['truncated'] else ''} row"
            rows += "s" if r["row_count"] != 1 or r["truncated"] else ""
        label = f" [{r['label']}]" if r["label"] else ""
        tables = f" {', '.join(r['tables'])}" if r["tables"] else ""
        lines.append(f"- {r['qid']}{label} {rows}{tables} · {r['sql']}")

    if brief.get("regression_runs"):
        lines += ["", "## Regression checks (latest 20 replays)"]
        for result in brief["regression_runs"]:
            lines.append(
                f"- {result['name']}: {result['status']} ({result['qid']}) — {result['details']}"
            )
            if result.get("persistence_error"):
                lines.append(f"  Library result was not saved: {result['persistence_error']}")

    if brief["charts"]:
        lines += ["", "## Charts"]
        lines += [
            f'- {c["chart_id"]} {c["kind"]} "{c["title"]}" ({c["qid"]})' for c in brief["charts"]
        ]

    if brief["narrative"]:
        lines += ["", "## Narrative (draft on record)", _compact(brief["narrative"], 1200)]

    r = brief["readiness"]
    lines += ["", "## Next"]
    lines.append(r["next_action"])
    if r["waived_checks"]:
        lines.append(f"waived: {', '.join(r['waived_checks'])}")
    return "\n".join(lines)
