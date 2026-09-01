"""Session report: one structured document summarizing a QA session.

Two layers, deliberately separate:

- **Facts** (`build_report`) are deterministic aggregation of the session
  record — checkpoints with evidence, findings, proposals with verification,
  charts, query statistics. Every figure cites an executed query id. This
  layer is not configurable; it is the consistency guarantee.
- **Presentation** (`render_markdown` + `ReportProfile`) is where a team's
  preferences live: section order and inclusion, audience, header/footer.
  Profiles are small YAML files in the library's `reports/` directory, shared
  by git like workflows — preference is declared data, never renderer forks.

An agent may add a *narrative* (`grayson session narrate`) — it renders in its
own clearly labeled section, must cite executed query ids, and can never alter
the deterministic sections below it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from grayson.core import engine
from grayson.core.session import Session
from grayson.util import utcnow


class ReportError(ValueError):
    """A report profile is missing or malformed; message names the file."""


#: the sections a profile may order/include. "summary" (the identity block at
#: the top) always renders and is not listed here.
REPORT_SECTIONS = (
    "narrative",
    "setup_inputs",
    "queries",
    "charts",
    "checkpoints",
    "findings",
    "proposals",
    "interventions",
)


class ReportProfile(BaseModel):
    """How reports render for a team. Additive-only, like every library format:
    unknown fields are tolerated (a newer grayson's profile still loads here)."""

    model_config = ConfigDict(extra="allow")

    name: str = "default"
    #: engineering keeps full evidence detail inline; stakeholder keeps every
    #: number but summarizes query-id noise (ids stay in the JSON report)
    audience: Literal["engineering", "stakeholder"] = "engineering"
    sections: list[str] = Field(default_factory=lambda: list(REPORT_SECTIONS))
    header: str = ""  #: markdown prepended above the title (branding, routing)
    footer: str = ""  #: markdown appended after the standard provenance line

    @field_validator("sections")
    @classmethod
    def _sections_are_strings(cls, v: list[str]) -> list[str]:
        # Unknown section names load fine and are skipped at render time — a
        # newer grayson's default.yaml (with a section this version doesn't
        # have) must not make the profile un-loadable here. unknown_sections()
        # surfaces them so a typo is visible, not an error.
        bad = [s for s in v if not isinstance(s, str)]
        if bad:
            raise ValueError(f"sections must be strings, got {bad}")
        return v

    def unknown_sections(self) -> list[str]:
        """Sections this grayson doesn't render — a newer profile's additions,
        or typos. Skipped at render; surfaced by callers as a warning."""
        return [s for s in self.sections if s not in REPORT_SECTIONS]


def load_profile(reports_dir: Path, name: str = "default") -> ReportProfile:
    """A profile from the library's reports/ dir. `default` falls back to the
    built-in defaults when no file exists; any other missing name is an error
    naming what is available."""
    path = reports_dir / f"{name}.yaml"
    if not path.is_file():
        if name == "default":
            return ReportProfile()
        available = (
            sorted(p.stem for p in reports_dir.glob("*.yaml")) if reports_dir.is_dir() else []
        )
        raise ReportError(
            f"no report profile '{name}' in {reports_dir} "
            f"(available: {', '.join(available) or 'default (built-in) only'})"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ReportProfile.model_validate({**data, "name": name})
    except (yaml.YAMLError, ValueError) as e:
        raise ReportError(f"report profile {path.name} is malformed: {e}") from e


DEFAULT_PROFILE_YAML = """\
# grayson report profile — how session reports render for this team.
# Shared by git like everything else in the library; `grayson session report
# --profile <name>` picks another file in this directory.
#
# audience: engineering keeps full evidence detail inline; stakeholder keeps
#   every number but summarizes query-id lists (ids stay in the JSON report).
# sections render in this order — remove one to drop it. Known sections:
#   narrative, setup_inputs, queries, charts, checkpoints, findings,
#   proposals, interventions.
# header/footer: markdown placed above the title / after the provenance line.
audience: engineering
sections:
  - narrative
  - setup_inputs
  - queries
  - charts
  - checkpoints
  - findings
  - proposals
  - interventions
header: ""
footer: ""
"""


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
        "charts": _collect_charts(session),
        "narrative": session.get_meta("report_narrative", "") or "",
        "interventions": {
            "total": len(interventions),
            "open": sum(1 for i in interventions if i["status"] == "open"),
            "answered": sum(1 for i in interventions if i["status"] == "answered"),
        },
        "query_stats": session.query_stats(),
    }


def _collect_charts(session: Session) -> list[dict]:
    """The session's charts with their text renderings — evidence-traceable
    figures belong in the report, not only in the live console."""
    from grayson.charts import chart_data, list_charts, render_text

    out = []
    for spec in list_charts(session):
        entry = {
            "id": spec.get("chart_id"),
            "qid": spec.get("qid"),
            "kind": spec.get("kind"),
            "title": spec.get("title", ""),
            "note": spec.get("note", ""),
        }
        try:
            entry["text"] = render_text(spec, chart_data(session, spec))
        except (KeyError, ValueError, OSError) as e:
            entry["error"] = f"chart could not be rendered from its cached artifact: {e}"
        out.append(entry)
    return out


# -- markdown rendering ---------------------------------------------------


def render_markdown(report: dict, profile: ReportProfile | None = None) -> str:
    profile = profile or ReportProfile()
    lines: list[str] = []
    if profile.header.strip():
        lines += [profile.header.strip(), ""]
    lines += _identity_block(report)
    for section in profile.sections:
        renderer = _SECTION_RENDERERS.get(section)
        if renderer is not None:  # unknown sections skip — see unknown_sections()
            lines += renderer(report, profile)
    ready = report["readiness"]
    lines += [
        f"Open checks remaining: {', '.join(ready['open_checks']) or 'none'}",
        "",
        "---",
        "",
        "*grayson — guarded SQL rails for agentic data investigation. "
        "Every figure above cites an executed query id from this session's audit log.*",
    ]
    if profile.footer.strip():
        lines += ["", profile.footer.strip()]
    lines.append("")
    return "\n".join(lines)


def _identity_block(report: dict) -> list[str]:
    s = report["session"]
    return [
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


def _sec_narrative(report: dict, profile: ReportProfile) -> list[str]:
    text = (report.get("narrative") or "").strip()
    if not text:
        return []
    return [
        "## Narrative (agent-written)",
        "",
        text,
        "",
        "*Written by the investigating agent. The sections below are rendered "
        "deterministically from the session record; query ids cite the audit log.*",
        "",
    ]


def _sec_setup_inputs(report: dict, profile: ReportProfile) -> list[str]:
    if not report.get("setup_inputs"):
        return []
    lines = ["## Setup inputs", ""]
    lines += [f"- **{k}:** {v}" for k, v in report["setup_inputs"].items()]
    return [*lines, ""]


def _sec_queries(report: dict, profile: ReportProfile) -> list[str]:
    stats = report["query_stats"]
    return [
        "## Queries",
        "",
        f"- Total: {stats['total']} "
        + " ".join(f"({status}: {n})" for status, n in sorted(stats["by_status"].items())),
        f"- Executed rows fetched: {stats['executed_rows']}, "
        f"warehouse time: {stats['executed_duration_ms']} ms",
        "",
    ]


def _sec_charts(report: dict, profile: ReportProfile) -> list[str]:
    charts = report.get("charts") or []
    if not charts:
        return []
    lines = ["## Charts", ""]
    for c in charts:
        if c.get("error"):
            lines += [f"- **{c['id']}** {c['title']} — {c['error']}", ""]
            continue
        lines += [f"### {c['title']}  ({c['id']} · from {c['qid']})", ""]
        if c.get("note"):
            lines += [c["note"], ""]
        lines += ["```text", c["text"], "```", ""]
    return lines


def _evidence_phrase(evidence: list[str], profile: ReportProfile) -> str:
    if not evidence:
        return ""
    if profile.audience == "stakeholder":
        n = len(evidence)
        return f" — evidence: {n} executed quer{'y' if n == 1 else 'ies'} cited"
    return f" — evidence: {', '.join(evidence)}"


def _sec_checkpoints(report: dict, profile: ReportProfile) -> list[str]:
    lines = ["## Checkpoints", ""]
    for c in report["checkpoints"]:
        # a waived check is cleared, not open — rendering it unticked would read as
        # unfinished work, and rendering it ticked would hide that it was skipped
        waived = c["status"] == "waived"
        mark = "x" if c["status"] == "complete" else "~" if waived else " "
        evidence = _evidence_phrase(c["evidence"], profile)
        off = c.get("evidence_off_scope") or []
        if off and evidence:
            # the reader judging this checkpoint should see how much of the
            # citation list actually touched the tables under investigation
            in_scope = len(c["evidence"]) - len(off)
            detail = (
                f"off-scope: {', '.join(off)}"
                if profile.audience == "engineering"
                else "the rest were lineage probes"
            )
            evidence += f" ({in_scope} of {len(c['evidence'])} touched scope; {detail})"
        note = f" ({c['note']})" if c.get("note") else ""
        suffix = f" — **waived**{note}" if waived else f"{evidence}{note}"
        lines.append(f"- [{mark}] **{c['key']}** — {c['title']}{suffix}")
    if not report["checkpoints"]:
        lines.append("- (none)")
    return [*lines, ""]


def _sec_findings(report: dict, profile: ReportProfile) -> list[str]:
    lines = ["## Findings", ""]
    for f in report["findings"]:
        accepted = "accepted" if f["accepted"] else "not accepted"
        lines += [
            f"### {f['fid']}: {f['title']}",
            "",
            f"- **Severity:** {f['severity']} | **Confidence:** {f['confidence']} "
            f"| **Status:** {accepted}",
        ]
        evidence = f["payload"].get("evidence", [])
        if profile.audience == "engineering":
            lines.append(f"- **Evidence:** {', '.join(evidence)}")
        else:
            lines.append(f"- **Evidence:** {len(evidence)} executed queries (ids in the record)")
        lines += [f"- **Summary:** {f['payload'].get('summary', '')}", ""]
    if not report["findings"]:
        lines += ["(none)", ""]
    return lines


def _sec_proposals(report: dict, profile: ReportProfile) -> list[str]:
    lines = ["## Proposals", ""]
    for p in report["proposals"]:
        verdict = (p.get("verification") or {}).get("verdict")
        verified = f" — verification: {verdict}" if verdict else ""
        linked = f" (fixes {p['finding_fid']})" if p.get("finding_fid") else ""
        lines.append(f"- **{p['pid']}** [{p['status']}] {p['title']}{linked}{verified}")
    if not report["proposals"]:
        lines.append("(none)")
    return [*lines, ""]


def _sec_interventions(report: dict, profile: ReportProfile) -> list[str]:
    iv = report["interventions"]
    return [
        "## Interventions",
        "",
        f"- Total: {iv['total']} (open: {iv['open']}, answered: {iv['answered']})",
        "",
    ]


_SECTION_RENDERERS = {
    "narrative": _sec_narrative,
    "setup_inputs": _sec_setup_inputs,
    "queries": _sec_queries,
    "charts": _sec_charts,
    "checkpoints": _sec_checkpoints,
    "findings": _sec_findings,
    "proposals": _sec_proposals,
    "interventions": _sec_interventions,
}


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
