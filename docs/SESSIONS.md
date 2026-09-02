# Running sessions

Harness setup, the session loop, profiling, charts, reports, and the guard
settings that bound it all. For what the rails guarantee and their limits:
[SPEC.md](SPEC.md), [SECURITY.md](SECURITY.md).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/session_dark.png">
  <img src="img/session_light.png" alt="A bug-hunter session in the grayson console: analysis charts built by the agent, an open intervention awaiting a human answer, checkpoint progress with evidence">
</picture>

*A bug-hunter session, live: the agent's charts (each traceable to a query
id), an open intervention, evidence-gated checkpoints. The page refreshes
itself while agents work.*

## Teaching your harness the protocol

```bash
grayson harness init cursor        # or claude-code | codex | copilot
```

Writes the protocol file for your harness (Cursor rule, `CLAUDE.md` section,
`AGENTS.md` section, or `.github/copilot-instructions.md` section) plus the
workflow-author skill, and offers two more writes, each behind its own
explicit yes:

- **Guard permissions** — deny rules so an agent calling `snow` directly or
  reading `.grayson/` state is blocked or prompted. Machine-written for
  Claude Code (`.claude/settings.json`), Copilot/VS Code
  (`.vscode/settings.json`), and Cursor (a hard-deny hook in
  `.cursor/hooks.json` + `.cursor/hooks/grayson-guard.py`; recent Cursor
  only — declining prints manual steps). Codex gets human steps; its OS
  sandbox is the layer. Reversible: `grayson harness guard status|apply|remove`.
- **MCP config** — registers `grayson mcp serve` (stdio) in the harness's
  project MCP file (`.mcp.json`, `.cursor/mcp.json`, `.vscode/mcp.json`;
  Codex is user-global, so grayson prints the snippet). Only the `grayson`
  entry is touched. Reversible: `grayson harness mcp status|apply|remove`.

The MCP server mirrors the CLI one-to-one; served variants are in
[DEPLOYMENT.md](DEPLOYMENT.md). `grayson status` shows where you are and
what needs attention; `latest` works anywhere a session id is expected.

Copilot note: `harness init copilot` targets **VS Code agent mode** (local,
human present). The cloud Copilot coding agent has no local console and no
analyst credentials — point it at a served knowledge-only endpoint instead.

## A typical session (driven by the agent)

```bash
grayson session start --workflow bug-hunter --table ANALYTICS.WEB.PAGE_EVENTS \
  --input anomaly_description="revenue rows doubled since Tuesday"
grayson query run <sid> --sql "SELECT ..." --label "why"  # guarded; cached as q_0001…
grayson cache query <sid> -q "SELECT ... FROM q_0001"     # re-slice locally, no warehouse trip
grayson chart add <sid> -a q_0001 -k line -x day -y null_rate --title "..."
grayson checkpoint complete <sid> replicate_anomaly -e q_0003
grayson finding add <sid> --json '{...}'                  # schema + evidence validated
grayson ui serve                                          # the human console
grayson session narrate <sid> --text "... (q_0003)"       # agent's story; must cite qids
grayson session report <sid> --out report.md              # shareable report
```

The flow: session start snapshots table metadata, loads relevant knowledge,
reports where the recorded column list has drifted from the warehouse
(`knowledge_drift`, [LIBRARY.md](LIBRARY.md)), checks QA-view coverage, and
surfaces failing external checks as pre-vetted leads ([CHECKS.md](CHECKS.md)). Checkpoints close only by citing executed
queries that touched the tables under investigation. Judgment calls go to a
human via interventions, answered in the console. Findings validate against
the workflow's schema; the human accepts or rejects each, approves proposed
fixes, and the agent proves an applied fix with a before/after comparison of
re-run queries.

**Setup inputs** are the questions a human answers before analysis starts —
the agent collects them in chat and records them with `--input key="answer"`
(MCP: `inputs`), so the session documents why it was started. A human driving
by hand can use `--interactive` prompts instead (terminal only).

**Reports** have two layers. Facts — checkpoints with evidence, findings,
proposals, charts, query stats — render deterministically; every figure
cites a query id. Presentation is a *report profile* in the library
(`--profile <name>`; see [LIBRARY.md](LIBRARY.md)). The agent's `narrate`
text renders in its own labeled section above the facts. On close, the full
report publishes into the library's `records/`.

## Honest endings

Two exits a gate must allow, or it teaches agents to manufacture evidence:

**A required check that does not apply** (freshness on a static reference
table) can be **waived**: the agent files an intervention saying why; a human
waives with a reason. Waived is its own status — never rendered as complete.

```bash
grayson checkpoint waive <sid> freshness --reason "static reference table"
```

**A run that finds nothing** closes as a **clean outcome**: required checks
cleared, nothing accepted, nothing awaiting judgment, and at least one query
executed ("we looked and it was fine" requires having looked).
`grayson session readiness` says when this route applies
(`clean_close_available`, `next_action`); the console offers the button.

```bash
grayson session close <sid> --clean --note "all four checks came back sound"
```

**A session that is broken, was started by mistake, or stopped mattering**
is **abandoned**: the third ending, and the honest label for "no result". It
skips the gates on purpose, so it never reads as clean or as findings; the
reason is recorded, open interventions are cancelled so nothing sits in
"awaiting your input", and nothing publishes to the library. The console
offers it beside *Close this session*; the closed list shows it as
`abandoned`.

```bash
grayson session abandon <sid> --reason "wrong target table; restarted as 2026…"
```

**Deleting** a session removes it from the workspace altogether — audit
trail, cache, charts. Records the session already published to the library
(accepted findings, verified fixes, its report) are a separate matter: they
stay unless you remove them too, which is the author's or a library admin's
call ([LIBRARY.md](LIBRARY.md#removing-records)). The session page's *Delete
this session* fold offers both together; from a terminal:

```bash
grayson session delete <sid> --yes             # the local session only
grayson session delete <sid> --yes --library   # and its published records
```

Every human boundary — accept/reject a finding, approve a fix, answer an
intervention, confirm a fact, waive a check, force a gate, close, abandon, or
delete a session, remove published records — is a **user** action requiring
an interactive terminal. An agent shelling out is refused, and the audit
trail attributes each action to whoever actually took it.

## Profiling

```bash
grayson profile table <sid> DB.SCHEMA.TABLE
```

The full descriptive battery — per-column nulls, cardinality, ranges, key
candidates, value frequencies — in three or four guarded statements instead
of forty (one `DESCRIBE`, one wide aggregate `SELECT`, one frequencies
`UNION ALL`, one sample). The returned `q_XXXX` ids are evidence and close
checkpoints directly. `observations` are mechanical leads ("null in 8.6% of
rows"), never verdicts.

```bash
grayson profile stats <sid> <sample-qid>       # mean, stdev, quantiles
grayson profile correlate <sid> <sample-qid>   # pairwise, pearson | spearman
```

These compute locally over the cached sample (pairwise correlation on the
warehouse would be hundreds of queries). **The evidence chain is weaker here
and says so**: responses carry `computed: "local"`, a confidence ceiling,
and a caveat — cite the sample's qid, say the number was computed locally,
and confirm anything decisive against the warehouse.

## Analysis charts

`grayson chart add` (MCP: `chart_add`) validates the column mapping against
the cached artifact and the console renders the chart live. Every chart
carries its query id and a fold with the exact rows drawn. The same chart
comes back as a Unicode rendering to paste into chat:

```
NULL email rate by day  [line · q_0007]
null_rate ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁█████  min 0.007 · max 0.1027 · last 0.0973
       x: 08-01 → 08-24
```

Kinds: `bar`, `line`, `scatter`; up to three series (the palette is
validated colorblind-safe at three — more dimensions means more charts).
`grayson chart render --out chart.svg` exports SVGs.

Any chart tile enlarges in a lightbox on click (← → walk the charts in the
order the agent made them), and `⤢` opens the chart's own page: full size,
the plotted rows, the source query, and a **Download SVG** for a slide or a
ticket. The session page's live refresh waits while a chart is enlarged or a
field has focus, so a half-typed note is never lost.

Long category labels never hide what varies. Whatever every label shares — a
date prefix, a schema path, a constant time part — comes off and is printed
once under the axis (`2026-08-…T00:00:00`, with `01 … 29` on the ticks);
dotted names shorten from the front so the table name survives
(`…PAGE_EVENTS`, not `ANALYTICS.W…`); and a label that is still cut carries
its full text inside the SVG (a `<title>`, so an exported file explains
itself) and shows it in the console on hover, tap, or keyboard focus. The
lightbox and the chart page render a *detail* size — twice the labels at twice
the length on a wider canvas — and **Download SVG** there (or
`grayson chart render --detail`) matches what was on screen.

## Settings and guard profiles

Configuration lives in `grayson.toml` — committed, reviewable, diffable.
Two surfaces change it, both human: `grayson config` and the console's
Settings page. MCP exposes configuration **read-only** — an agent that can
loosen its own guards has no guards.

```bash
grayson config show
grayson config set defaults.guard_profile=strict scopes.strict=true
grayson config profile overnight --auto-limit 0 --budget-cap 500
grayson config workflow-defaults table-health --guard-profile strict --strict-scope
```

Guard profiles bundle three cost controls — auto-`LIMIT`, per-statement
timeout, per-session query budget — selected per session. Scope is per
session and is a wall around *rows*: listings and single-object metadata
(`SHOW TABLES`, `DESCRIBE`, `GET_DDL` of a table or view) are readable for
any table and name their object; reading an out-of-scope table's rows warns
by default, and `strict` blocks it.

Scope widens only by a human's decision. The agent asks with a
`scope_request` intervention naming the tables and why; ticking them in
the console grants exactly those, from the next statement. Or widen it
yourself:

```bash
grayson session scope <id>                        # show
grayson session scope <id> DB.SCHEMA.SIBLING      # widen (a user action)
```

Both land in the audit trail as `scope_changed` events naming who and how.

Defaults resolve per workflow: an explicit flag at session start always wins,
then the workspace's per-workflow defaults (above; also editable on the
Settings page), then the last-used profile on those tables or the template's
own suggestion. Bounded workflows can suggest strict scope themselves —
`table-onboarding` ships with it on, because an out-of-scope read there is a
wrong turn, not exploration.

A session **snapshots** its guard at start; settings changes apply to future
sessions only. Changing a live session is deliberate and logged — a user
action from the session's own page in the console, or:

```bash
grayson session guard <id> --guard-profile strict --strict-scope
grayson session budget <id> --cap 200
```

Both take effect from the next statement and land in the audit trail as
`guard_changed` / `budget_changed` events.

Two things can make a query time out sooner than the profile says:

- **The session predates the edit.** A profile saved on the Settings page
  reaches sessions started after the save (through the CLI and through a
  running MCP server alike — grayson re-reads `grayson.toml` when the file
  changes). A session already running keeps its snapshot until you move it
  with `grayson session guard`.
- **Snowflake enforces a lower limit.** The guard sets
  `STATEMENT_TIMEOUT_IN_SECONDS` at session level, but Snowflake applies the
  lowest non-zero value across the session and the warehouse (and the user
  and account levels above). A warehouse capped at 300s cancels at 300s
  whatever the profile says; only a Snowflake admin can raise that. The
  timeout error grayson returns names which of the two fired.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/settings_dark.png">
  <img src="img/settings_light.png" alt="The Settings page: connection, default guard profile, strict scope, editable guard profiles, team library controls">
</picture>

Light/dark theme is per-browser (top-bar toggle), not a workspace setting.

## Auditing what ran

Every statement — accepted or rejected — lands in the session's audit log
with hash, worker, verdict, and stats. `grayson audit reconcile` (human-only;
no MCP twin) diffs the warehouse's own query history against the trail and
flags statements that ran around grayson; `--ingest` records the verdict as
an external check. Details: [SECURITY.md](SECURITY.md).
