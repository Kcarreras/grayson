"""Portable offline snapshots. Exporting never closes or publishes a session."""

from __future__ import annotations

import json
from uuid import uuid4

from jinja2 import Environment, select_autoescape

from grayson.core.session import Session
from grayson.report import build_report, render_markdown
from grayson.util import atomic_write_text

_TEMPLATE = """<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy"
 content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{{ title }} — Grayson</title>
<style>
:root{font:16px/1.6 system-ui;color:#183047;background:#edf2f6}
body{max-width:1050px;margin:auto;padding:32px}header{padding:32px;background:#183047;
color:white;border-radius:18px}h1{line-height:1.15;font-size:36px;margin:12px 0}
.notice{padding:18px;border-left:4px solid #bb7900;background:#fff4d6}
section{background:white;padding:24px;border-radius:14px;margin:20px 0}
article{border-top:1px solid #dde5ed;padding:16px 0}summary{cursor:pointer}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.6 ui-monospace,monospace}
.prose{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#116d8a}
nav{display:flex;gap:20px;flex-wrap:wrap;margin:20px 0}
@media print{body{padding:0;background:white}nav{display:none}}
</style>
<header><span>GRAYSON / {{ 'CLOSED SESSION SNAPSHOT' if closed else 'WORKING DRAFT' }}</span>
<h1>{{ title }}</h1><div>{{ report.session.id }} · {{ report.session.workflow }}
 · {{ report.session.stage }}</div><div>Generated {{ report.generated_at }}</div></header>
<p class="notice">{{ report.context_notice }}</p>
<nav><a href="report.md">Markdown</a><a href="report.json">Structured evidence</a>
{% for name in groups %}<a href="#{{ name }}">{{ name|capitalize }}</a>{% endfor %}</nav>
{% if report.narrative %}<section><h2>Agent interpretation</h2>
<div class="prose">{{ report.narrative }}</div></section>{% endif %}
<section><h2>Readiness and open work</h2>
<p>{{ report.query_stats.total }} queries recorded · {{ report.findings|length }} findings
 · {{ report.proposals|length }} proposals</p>
<p>Required checks: {{ 'complete' if report.readiness.checks_complete else 'still open' }}.</p>
{% if report.readiness.open_checks %}<ul>{% for check in report.readiness.open_checks %}
<li>{{ check }}</li>{% endfor %}</ul>{% endif %}
<details><summary>Inspect all readiness gates</summary>
<pre>{{ readiness }}</pre></details></section>
{% for name, rows in groups.items() %}<section id="{{ name }}"><h2>{{ name|capitalize }}</h2>
{% for row in rows %}<article><h3>{{ row.title or row.key or row.pid or row.fid }}</h3>
<div>{{ row.status or '' }}{% if row.superseded_by %} · Superseded by {{ row.superseded_by }}
{% elif row.rejected %} · Rejected{% elif row.accepted %} · Accepted{% endif %}</div>
{% if row.payload %}<p class="prose">{{ row.payload.summary or row.payload.rationale or '' }}</p>
{% endif %}<details><summary>Inspect evidence and recorded details</summary>
<pre>{{ row|tojson(indent=2) }}</pre></details></article>
{% else %}<p>None recorded.</p>{% endfor %}</section>{% endfor %}
<section><details><summary>Complete report, including charts and comparisons</summary>
<pre>{{ markdown }}</pre></details></section></html>"""


def export_deliverable(session: Session, title: str = "", presentation: dict | None = None) -> dict:
    from grayson.deliverable_authoring import render_presentation, resolve_presentation

    report = build_report(session, session.workspace.workflows_dir)
    markdown = render_markdown(report)
    title = title.strip() or report["session"]["title"] or "Session investigation"
    authored = resolve_presentation(session, presentation) if presentation is not None else None
    template = Environment(autoescape=select_autoescape(default=True)).from_string(_TEMPLATE)
    html = template.render(
        title=title,
        closed=session.stage == "closed",
        report=report,
        markdown=markdown,
        readiness=json.dumps(report["readiness"], indent=2, default=str),
        groups={key: report[key] for key in ("checkpoints", "findings", "proposals")},
    )
    files = {
        "index.html": html,
        "report.md": markdown,
        "report.json": json.dumps(report, indent=2, default=str),
    }
    if authored:
        spec, manifest = authored
        manifest["generated_at"] = report["generated_at"]
        files["index.html"] = render_presentation(spec, manifest, report, title)
        files["presentation.json"] = spec.model_dump_json(indent=2)
        files["evidence.json"] = json.dumps(manifest, indent=2, allow_nan=False)
        files["session-report.md"] = markdown
        lines = [
            f"# {title}",
            "",
            report["context_notice"],
            "",
            f"Generated: {report['generated_at']} · Session: {session.id} · Stage: {session.stage}",
            "",
            "## Agent-authored interpretation",
            "",
            spec.summary,
            "",
            "## SQL evidence supplied by Grayson",
            "",
            manifest["assurance"],
            "",
        ]
        for name, dataset in manifest["datasets"].items():
            evidence = dataset["evidence"]
            lines.extend(
                [
                    f"- **{name}**: {session.id}/{evidence['qid']} at {evidence['ts']}; "
                    f"{dataset['exported_rows']}/{dataset['cached_rows']} cached rows; "
                    f"source truncated: {dataset['source_truncated']}; "
                    f"export limited: {dataset['export_limited']}",
                ]
            )
        lines += [
            "",
            "Full SQL and data: [evidence.json](evidence.json).",
            "Session archive: [session-report.md](session-report.md).",
            "",
        ]
        files["report.md"] = "\n".join(lines)
    folder = session.dir / "deliverables" / uuid4().hex
    folder.mkdir(parents=True)
    for name, content in files.items():
        atomic_write_text(folder / name, content)
    return {
        "session_id": session.id,
        "generated_at": report["generated_at"],
        "draft": session.stage != "closed",
        "mode": "authored" if authored else "snapshot",
        "html": str(folder / "index.html"),
        "markdown": str(folder / "report.md"),
        "json": str(folder / "report.json"),
        "context_notice": report["context_notice"],
        **(
            {
                "evidence": str(folder / "evidence.json"),
                "presentation": str(folder / "presentation.json"),
                "session_markdown": str(folder / "session-report.md"),
            }
            if authored
            else {}
        ),
    }
