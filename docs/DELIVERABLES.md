# Session deliverables and historical context

Agents can call `session_deliverable(session_id, title="", presentation={...})` at any stage.
It writes a new snapshot under the local session's `deliverables/<version>/`
directory and returns paths to `index.html`, `report.md`, and `report.json`.
Share the whole directory to retain its companion links.

The agent designs the presentation: freeform HTML, CSS, JavaScript, SVG and
canvas, including bespoke charts, tabs, filters, tooltips and linked controls.
There is no prescribed chart grammar or session-report layout. Inline library
code is supported; scripts that require external assets or dynamic evaluation
must be bundled/adapted for the offline content policy.

The `presentation` object has these fields:

| Field | Contract |
| --- | --- |
| `html` | Body markup, including one uniquely identified container per declared visual |
| `css` | Styles for the custom presentation |
| `javascript` | Code run after the markup; no warehouse credentials or query execution |
| `summary` | Curated Markdown interpretation for posterity |
| `datasets` | Named bindings: `{ "sales": { "qid": "q_0001", "columns": ["MONTH", "REVENUE"], "max_rows": 10000 } }` |
| `visuals` | Container IDs and evidence associations: `{ "trend": { "title": "Revenue trend", "datasets": ["sales"], "note": "Optional explanation" } }` |

Grayson resolves the input values itself. Dataset bindings cannot contain rows
or invented columns. Queries must have executed successfully in the session,
with matching SQL and available cached results. Missing/purged results are an
error rather than a silently empty chart. Projection and export limits are
explicit; the default limit is 10,000 rows per dataset, with a 50,000 row maximum
and a 20 MB total serialized evidence limit. An empty result is valid evidence;
the current cache does not retain column metadata for empty results, so omit
`columns` for those bindings.

The synchronous JavaScript API is:

```javascript
const rows = grayson.data("sales");          // recursively frozen rows
const source = grayson.evidence("sales");    // SQL, session ID, query ID, timestamp
const dataset = grayson.dataset("sales");   // columns, rows, limits, provenance, SHA-256
const visual = grayson.visual("trend");     // declared title and dataset associations
```

Perform analytical aggregations and transformations in guarded SQL, then bind
the resulting query. Use JavaScript for display, sorting, filtering, selection
and exploration. Browser-derived scenarios should be labelled as assumptions
or derived interpretation, not verified SQL observations. Use `textContent`
when displaying string values from rows. Every custom visual needs a static
container ID in `html`; its contents can be created dynamically.

The authored page runs inside an iframe with `sandbox="allow-scripts"` and no
same-origin permission. Its content policy blocks fetch and external scripts,
styles and images. This isolates the surrounding evidence inspector from the
custom page. It is not a general-purpose containment system for hostile code:
authors must not add outbound navigation or links carrying data. Agent HTML,
CSS and JavaScript are executable presentation source and should be reviewed
as such before sharing.

An independent Grayson inspector lists each declared visual's datasets, SQL,
source time, row limits and data fingerprint outside that iframe. This verifies
the supplied data path, **not** arbitrary code's calculations, labels or marks.
JavaScript can still draw incorrect or invented values or ignore its binding;
binding declarations and frozen inputs cannot prove visual truth. The fingerprint
identifies the exported row content, not a signed attestation of warehouse truth
or protection against someone editing files on disk. Stronger certification
would require a constrained renderer and a validated transformation grammar.

Authored exports include `presentation.json` (editable source), `evidence.json`
(resolved rows and provenance), curated `report.md`, and `session-report.md`
(the full deterministic archive). The HTML embeds its data and works offline.
Exported rows travel with it: select only the columns and scope intended for
the deliverable's audience.

See [the worked revenue explorer](examples/revenue_deliverable.py) for a custom
SVG chart with a month slider, plan toggle, hover values and a detail panel.
It is an example, not a required visual template.

Omit `presentation` to retain the basic session snapshot. For that mode,
`session_narrate` supplies the interpretation. After feedback or new evidence,
edit the presentation/summary or narrative and export another version.
Open sessions are labelled working drafts. Export does not close the session,
approve findings, publish library records, or commit files. The existing
close-time Markdown publication to the repository remains in place.
`session_report` also works before close and returns structured report data.

Reports are historical snapshots, not maintained knowledge. New Markdown,
JSON and HTML exports say this explicitly. Record search and retrieval label
reports as historical, including old reports; search ranks them below current
records. Rejected and superseded findings retain those labels in Markdown.

Knowledge curation separately derives fact standing from its anchors and
policy. Briefings omit stale and retired facts and label unverified facts.
That mechanism does **not** revalidate free-form report narratives or rewrite
old Markdown when a schema or business definition changes. Citations establish
provenance, not continued truth. Reuse a report as an investigation lead;
check current knowledge and rerun relevant evidence before treating its claims
as current. Arbitrary repository readers can still ingest old Markdown without
observing these rules: these labels reduce confusion but are not a freshness
guarantee or an automatic curation pass over prose.

Recording verification while in fixes advances to verification through the
normal evidence gates. Both passing and failing results count as verification
activity; the timeline displays the number checked and the number passed.
It does not advance closed sessions or bypass incomplete checkpoints.
