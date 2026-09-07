"""Freeform presentation over server-resolved SQL evidence snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from base64 import b64encode
from html.parser import HTMLParser

from jinja2 import Environment, select_autoescape
from pydantic import BaseModel, ConfigDict, Field

from grayson.core.session import Session
from grayson.records import evidence_snapshot
from grayson.util import sql_hash


class DatasetBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qid: str = Field(pattern=r"^q_[0-9]{4,}$")
    columns: list[str] | None = None
    max_rows: int = Field(default=10000, ge=1, le=50000)


class VisualBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    datasets: list[str] = Field(min_length=1)
    note: str = ""


class Presentation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    html: str = Field(min_length=1, max_length=500000)
    css: str = Field(default="", max_length=500000)
    javascript: str = Field(default="", max_length=2000000)
    summary: str = Field(min_length=1, max_length=100000)
    datasets: dict[str, DatasetBinding] = Field(min_length=1, max_length=20)
    visuals: dict[str, VisualBinding] = Field(min_length=1, max_length=100)


class _Anchors(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.ids.extend(value for key, value in attrs if key == "id" and value is not None)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _encode_rows(columns: list[str], rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep SQLite integers and binary values lossless across JSON.parse."""
    encoded = []
    encodings = []
    for index, row in enumerate(rows):
        output = {}
        for column in columns:
            value = row[column]
            encoding = None
            if isinstance(value, int) and abs(value) > 2**53 - 1:
                value, encoding = str(value), "integer-decimal"
            elif isinstance(value, bytes):
                value, encoding = b64encode(value).decode("ascii"), "binary-base64"
            output[column] = value
            if encoding:
                encodings.append({"row": index, "column": column, "encoding": encoding})
        encoded.append(output)
    return encoded, encodings


def resolve_presentation(session: Session, content: dict) -> tuple[Presentation, dict]:
    """Only Grayson reads rows; the author supplies references, never dataset values."""
    spec = Presentation.model_validate(content)
    for name in [*spec.datasets, *spec.visuals]:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
            raise ValueError(f"invalid dataset or visual name: {name}")
    anchors = _Anchors()
    anchors.feed(spec.html)
    for name, visual in spec.visuals.items():
        if anchors.ids.count(name) != 1:
            raise ValueError(f"visual {name} requires exactly one HTML element with that id")
        if set(visual.datasets) - spec.datasets.keys():
            raise ValueError(f"unknown dataset binding in visual {visual.title}")
    datasets = {}
    for name, binding in spec.datasets.items():
        query = session.query_row(binding.qid)
        sidecar = session.cache.get(binding.qid)
        if not query or query["status"] != "executed" or not sidecar:
            raise ValueError(f"{name}: requires a successfully executed query with cached evidence")
        sql = query.get("sql_executed") or query["sql_raw"]
        if sidecar.get("query_hash") != sql_hash(sql):
            raise ValueError(f"{name}: cache SQL does not match executed evidence; rerun the query")
        count = session.cache.row_count(binding.qid)
        if count is None and (sidecar.get("artifact") or query.get("row_count")):
            raise ValueError(f"{name}: cached evidence was removed; rerun the query")
        if (count or 0) != query.get("row_count"):
            raise ValueError(f"{name}: cached row count differs from query audit; rerun the query")
        columns, rows = session.cache.rows(binding.qid, limit=binding.max_rows)
        selected = binding.columns if binding.columns is not None else columns
        if len(set(selected)) != len(selected) or set(selected) - set(columns):
            raise ValueError(f"{name}: columns must be unique columns from {binding.qid}")
        indexes = [columns.index(column) for column in selected]
        values = [dict(zip(selected, [row[i] for i in indexes], strict=True)) for row in rows]
        values, encodings = _encode_rows(selected, values)
        evidence = evidence_snapshot(session, [binding.qid])[0]
        datasets[name] = {
            "columns": selected,
            "rows": values,
            "cell_encodings": encodings,
            "evidence": evidence,
            "exported_rows": len(values),
            "cached_rows": count or 0,
            "export_limited": (count or 0) > len(values),
            "source_truncated": bool(query.get("truncated")),
            "source_last_altered": sidecar.get("source_last_altered") or {},
            "sha256": hashlib.sha256(
                _json({"rows": values, "cell_encodings": encodings}).encode()
            ).hexdigest(),
        }
    manifest = {
        "format": 1,
        "session_id": session.id,
        "assurance": "SQL-backed input snapshots; agent-authored visuals and interpretation.",
        "datasets": datasets,
        "visuals": {name: v.model_dump() for name, v in spec.visuals.items()},
    }
    if len(_json(manifest).encode()) > 20_000_000:
        raise ValueError("deliverable data exceeds 20 MB; select fewer columns or aggregate in SQL")
    return spec, manifest


_RUNTIME = """
(() => {
  const source = JSON.parse(document.getElementById('grayson-evidence').textContent);
  const freeze = value => {
    if (value && typeof value === 'object') {
      Object.values(value).forEach(freeze); Object.freeze(value);
    }
    return value;
  };
  freeze(source);
  const dataset = name => {
    if (!Object.hasOwn(source.datasets, name)) throw new Error('Unknown dataset: ' + name);
    return source.datasets[name];
  };
  Object.defineProperty(window, 'grayson', {value: Object.freeze({
    data: name => dataset(name).rows,
    evidence: name => dataset(name).evidence,
    dataset,
    visual: name => {
      if (!Object.hasOwn(source.visuals, name)) throw new Error('Unknown visual: ' + name);
      return source.visuals[name];
    }
  }), writable: false, configurable: false});
})();
"""

_SHELL = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
 style-src 'unsafe-inline' data:; script-src 'unsafe-inline' data:; frame-src 'self';
 img-src data: blob:; font-src data:;
 base-uri 'none'; form-action 'none'">
<title>{{ title }}</title><style>
body{margin:0;font:15px/1.5 system-ui;color:#203247;background:#f5f7fa}
header,aside{padding:18px 28px}header{display:flex;gap:20px;flex-wrap:wrap;background:#fff}
iframe{width:100%;height:85vh;border:0;background:white}summary{cursor:pointer}
pre{white-space:pre-wrap;overflow-wrap:anywhere}article{padding:16px;border-top:1px solid #ccd4dc}
a{color:#096c8d}small{display:block}
</style><header><strong>{{ title }}</strong><span>{{ status }} · {{ generated }}</span>
<a href="#evidence">Inspect SQL evidence</a><a href="report.md">Markdown</a>
<a href="evidence.json">Data and provenance</a></header>
<iframe sandbox="allow-scripts" title="Agent-authored interactive presentation"
 srcdoc="{{ document }}"></iframe>
<aside id="evidence"><h2>Evidence supplied by Grayson</h2>
<p>{{ notice }} {{ manifest.assurance }} Bindings below are declared by the author;
they do not certify calculations, labels or marks drawn by presentation code.</p>
{% for name, visual in manifest.visuals.items() %}
<p><strong>{{ visual.title }}</strong> ({{ name }}) ← {{ visual.datasets|join(', ') }}
{% if visual.note %} · {{ visual.note }}{% endif %}</p>{% endfor %}
{% for name, dataset in manifest.datasets.items() %}<article><h3>{{ name }}</h3>
<p>{{ dataset.evidence.session_id }} / {{ dataset.evidence.qid }} ·
{{ dataset.evidence.ts }} · {{ dataset.exported_rows }} of {{ dataset.cached_rows }} cached rows</p>
{% if dataset.source_truncated %}<strong>Source query was limited/truncated.</strong>{% endif %}
{% if dataset.export_limited %}<strong>Export includes only a subset of cached rows.</strong>
{% endif %}
<details><summary>SQL, columns and fingerprint</summary>
<pre>{{ dataset.evidence.sql_executed or dataset.evidence.sql }}</pre>
<p>{{ dataset.columns|join(', ') }}</p><pre>SHA-256: {{ dataset.sha256 }}</pre></details>
{% if dataset.cell_encodings %}<p>Some cells use lossless string encodings for large integers
or binary values. See cell_encodings in the data and provenance file.</p>{% endif %}
</article>{% endfor %}</aside></html>"""


def render_presentation(spec: Presentation, manifest: dict, report: dict, title: str) -> str:
    # Escape the JSON script boundary, including hostile strings from SQL results.
    data = _json(manifest).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    # Raw-text HTML elements terminate even inside JS/CSS string literals.
    # Data URLs preserve arbitrary UTF-8 source without rewriting its syntax.
    css = b64encode(spec.css.encode("utf-8")).decode("ascii")
    javascript = b64encode(spec.javascript.encode("utf-8")).decode("ascii")
    document = (
        '<!doctype html><html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "script-src 'unsafe-inline' data:; style-src 'unsafe-inline' data:; "
        "img-src data: blob:; "
        "font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'\">"
        f'<script id="grayson-evidence" type="application/json">{data}</script>'
        f"<script>{_RUNTIME}</script>"
        f'<link rel="stylesheet" href="data:text/css;charset=utf-8;base64,{css}">'
        f'{spec.html}<script src="data:text/javascript;charset=utf-8;base64,{javascript}">'
        "</script></html>"
    )
    template = Environment(autoescape=select_autoescape(default=True)).from_string(_SHELL)
    return template.render(
        title=title,
        document=document,
        manifest=manifest,
        status="Working draft" if report["session"]["stage"] != "closed" else "Historical snapshot",
        generated=report["generated_at"],
        notice=report["context_notice"],
    )
