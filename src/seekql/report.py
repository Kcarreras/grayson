"""Session report: one structured document summarizing a QA session.

Pulls the session's summary, checkpoints, findings, proposals (with
verification), intervention counts, and query statistics into a single dict,
with an optional markdown rendering for sharing outside the tool.
"""

from __future__ import annotations

from pathlib import Path

from seekql.core import engine
from seekql.core.session import Session
from seekql.util import utcnow


def build_report(session: Session, overrides_dir: Path | None = None) -> dict:
    interventions = session.interventions()
    return {
        "generated_at": utcnow(),
        "session": session.summary(),
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
        f"# seekql session report — {s['id']}",
        "",
        f"- **Title:** {s['title'] or '(untitled)'}",
        f"- **Workflow:** {s['workflow']}",
        f"- **Stage:** {s['stage']}",
        f"- **Targets:** {', '.join(s['targets']) or '(none)'}",
        f"- **Created:** {s['created_at']}",
        f"- **Generated:** {report['generated_at']}",
        "",
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
        mark = "x" if c["status"] == "complete" else " "
        evidence = f" — evidence: {', '.join(c['evidence'])}" if c["evidence"] else ""
        note = f" ({c['note']})" if c.get("note") else ""
        lines.append(f"- [{mark}] **{c['key']}** — {c['title']}{evidence}{note}")
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
    ]
    return "\n".join(lines)
