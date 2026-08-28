"""Session report: one structured document summarizing a QA session.

Pulls the session's summary, checkpoints, findings, proposals (with
verification), intervention counts, and query statistics into a single dict,
with an optional markdown rendering for sharing outside the tool.
"""

from __future__ import annotations

from pathlib import Path

from grayson.core import engine
from grayson.core.session import Session
from grayson.util import utcnow


def build_report(session: Session, overrides_dir: Path | None = None) -> dict:
    interventions = session.interventions()
    return {
        "generated_at": utcnow(),
        "session": session.summary(),
        "setup_inputs": session.setup_inputs(),
        "readiness": engine.readiness(session, overrides_dir),
        "checkpoints": session.checkpoints(),
        "findings": session.findings(),
        "proposals": session.proposals(),
        "interventions": {
            "total": len(interventions),
            "open": sum(1 for i in interventions if i["status"] == "open"),
            "answered": sum(1 for i in interventions if i["status"] == "answered"),
        },
        "query_stats": session.query_stats(),
    }


def render_markdown(report: dict) -> str:
    s = report["session"]
    ready = report["readiness"]
    stats = report["query_stats"]
    lines = [
        f"# grayson session report — {s['id']}",
        "",
        f"- **Title:** {s['title'] or '(untitled)'}",
        f"- **Workflow:** {s['workflow']}",
        f"- **Stage:** {s['stage']}" + (f" ({_outcome_line(s)})" if s.get("outcome") else ""),
        f"- **Targets:** {', '.join(s['targets']) or '(none)'}",
        f"- **Created:** {s['created_at']}",
        f"- **Generated:** {report['generated_at']}",
        "",
    ]
    if report.get("setup_inputs"):
        lines += ["## Setup inputs", ""]
        lines += [f"- **{k}:** {v}" for k, v in report["setup_inputs"].items()]
        lines += [""]
    lines += [
        "## Queries",
        "",
        f"- Total: {stats['total']} "
        + " ".join(f"({status}: {n})" for status, n in sorted(stats["by_status"].items())),
        f"- Executed rows fetched: {stats['executed_rows']}, "
        f"warehouse time: {stats['executed_duration_ms']} ms",
        "",
        "## Checkpoints",
        "",
    ]
    for c in report["checkpoints"]:
        # a waived check is cleared, not open — rendering it unticked would read as
        # unfinished work, and rendering it ticked would hide that it was skipped
        waived = c["status"] == "waived"
        mark = "x" if c["status"] == "complete" else "~" if waived else " "
        evidence = f" — evidence: {', '.join(c['evidence'])}" if c["evidence"] else ""
        off = c.get("evidence_off_scope") or []
        if off:
            # the reader judging this checkpoint should see how much of the
            # citation list actually touched the tables under investigation
            in_scope = len(c["evidence"]) - len(off)
            evidence += (
                f" ({in_scope} of {len(c['evidence'])} cited queries touched scope; "
                f"off-scope: {', '.join(off)})"
            )
        note = f" ({c['note']})" if c.get("note") else ""
        suffix = f" — **waived**{note}" if waived else f"{evidence}{note}"
        lines.append(f"- [{mark}] **{c['key']}** — {c['title']}{suffix}")
    if not report["checkpoints"]:
        lines.append("- (none)")
    lines += ["", "## Findings", ""]
    for f in report["findings"]:
        accepted = "accepted" if f["accepted"] else "not accepted"
        lines += [
            f"### {f['fid']}: {f['title']}",
            "",
            f"- **Severity:** {f['severity']} | **Confidence:** {f['confidence']} "
            f"| **Status:** {accepted}",
            f"- **Evidence:** {', '.join(f['payload'].get('evidence', []))}",
            f"- **Summary:** {f['payload'].get('summary', '')}",
            "",
        ]
    if not report["findings"]:
        lines += ["(none)", ""]
    lines += ["## Proposals", ""]
    for p in report["proposals"]:
        verdict = (p.get("verification") or {}).get("verdict")
        verified = f" — verification: {verdict}" if verdict else ""
        linked = f" (fixes {p['finding_fid']})" if p.get("finding_fid") else ""
        lines.append(f"- **{p['pid']}** [{p['status']}] {p['title']}{linked}{verified}")
    if not report["proposals"]:
        lines.append("(none)")
    iv = report["interventions"]
    lines += [
        "",
        "## Interventions",
        "",
        f"- Total: {iv['total']} (open: {iv['open']}, answered: {iv['answered']})",
        "",
        f"Open checks remaining: {', '.join(ready['open_checks']) or 'none'}",
        "",
        "---",
        "",
        "*grayson — guarded SQL rails for agentic data investigation. "
        "Every figure above cites an executed query id from this session's audit log.*",
        "",
    ]
    return "\n".join(lines)


def _outcome_line(summary: dict) -> str:
    """How a closed session ended, for the shareable report.

    A clean run has no findings, so without this the report of a genuine
    all-clear is indistinguishable from the report of a session that gave up.
    """
    outcome = summary.get("outcome")
    note = (summary.get("outcome_note") or "").strip()
    if outcome == "clean":
        text = "clean — checks cleared, nothing found worth acting on"
    elif outcome == "findings":
        text = "closed on accepted findings"
    else:
        return outcome or ""
    return f"{text}{': ' + note if note else ''}"
