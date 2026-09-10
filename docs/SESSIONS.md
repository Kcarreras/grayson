# Running sessions

Harness setup, the session loop, profiling, charts, reports, and the guard
settings that bound it all. For what the rails guarantee and their limits:
[SPEC.md](SPEC.md), [SECURITY.md](SECURITY.md).

## Navigating the console and command line

Use **Go to…** (`Ctrl K` or `Cmd K`) to find a page or section. Press `/` to
focus the current page's list search. Filter choices in the same category add
matches: selecting both **executed** and **rejected** shows either status.
Different categories narrow the results. **Clear filters** restores the list.
Attention and evidence links reveal their destination even if it was filtered
out. Proposal folds remember your choice across live updates.

Live updates pause for unsaved edits, keyboard review, open dialogs, and selected
text. **Pause** keeps the page still until **Resume**. If the console has restarted
and an old link no longer works, open the new link printed by `grayson ui serve`.

CLI commands that accept `--json` also accept a UTF-8 `--file` or piped JSON.
Use one input source; payloads for findings, proposals, intervention requests,
and knowledge profiles must be JSON objects. Empty input, unreadable files, and
invalid JSON produce a JSON error on stderr with a nonzero exit code. UTF-8
files with a byte-order mark, as saved by some Windows editors, are supported.
Success-criteria, comparison, and project specification files also accept YAML.

`latest`, `last`, and `.` select the newest session in CLI and MCP calls. When
working on several sessions, list them with `grayson session list` or MCP
`session_list` and retain the exact ID returned when starting each one.

## Choosing how fixes are delivered

Use **Fix delivery** on the session page to choose **Update a local file**,
**SQL to copy and run**, or **Let the agent choose**. This preference is exposed
in `session_status` and `session_brief` for agents and newly joined workers.
It applies to future drafting; existing proposals are not converted.

For changes without a local SQL file, or when you prefer to run the code yourself,
the agent uses `proposal_add` with `kind: "ddl_snippet"`. The payload contains
`ddl` (the SQL), `run_target` (the intended database/schema/editor), `rationale`,
and optionally `success_criteria`. No local source file is required.

Review the highlighted SQL and approve it, then use **Copy SQL** or **Download
.sql** and run it in your own editor. These controls transfer the exact proposal
text, without executing SQL or changing proposal status. If the proposal changes
since the page was loaded, reload before copying or downloading.

After execution, click **I've applied this**, or tell the agent you ran the SQL so
it can call `proposal_applied`. This records reported external execution; it does
not claim verification passed. Success criteria can be verified separately where
Grayson's configured connection can access the affected objects. Without an original
definition, the review shows highlighted SQL rather than a before/after diff.

## Local file fixes

Keep source unchanged until the user approves the proposal. For an existing file,
read its source and call MCP `proposal_file_snapshot` for its SHA-256. Send all
changes together to `proposal_draft_edits` with `expected_source_sha256`, a
`request_id`, and `edits: [{"old_text": "exact original text", "new_text": "replacement"}]`.
Each old text must match exactly once in the original snapshot; edits cannot
overlap. Include unchanged context for insertions. The server builds the complete
replacement, preserving untouched bytes. A failed edit creates no proposal.

Repeat identical calls with the same request ID to retrieve the existing proposal.
For a revision, use a new request ID and `supersedes: "p_001"`. The earlier pending
or approved draft becomes superseded and cannot be applied; approval does not
transfer. Applied fixes cannot be superseded. Distinct changes to the same file
are not automatically merged or replaced.

Use `proposal_draft_file` only for new files or small **complete** replacements.
Never send separate chunks as replacement proposals. Emptying a nonempty file,
or shrinking a file by more than half its lines (at least 20 original lines) or
bytes (at least 4,096 original bytes), is blocked on the full-replacement path.
The same check blocks approval/application of older truncated drafts. Intentional
bulk deletion must use exact targeted edits and requires a deletion acknowledgement
at approval. This heuristic is an additional check, not a proof of completeness.

The console shows coloured additions/removals, old/new line numbers, and file size
changes. Pending and approved proposals open for review; older decisions collapse
into history. Use the status filters or search to find a file. Approval leaves the
source unchanged; **Apply approved file fix** performs the write separately.

CLI equivalents:

```bash
grayson proposal file-snapshot <sid> --target models/orders.sql
grayson proposal draft-edits <sid> --target models/orders.sql --source-sha256 <hash> --edits-file <json-file> --request-id orders-fix-1 --title "Fix duplicate orders"
grayson proposal draft-file <sid> --target models/new.sql --content-file <scratch-file> --title "New model"
grayson proposal apply <sid> <pid>   # only after UI approval
```

The session screen offers **Apply approved file fix** after approval. That
action, or MCP `proposal_apply`, checks that the source still matches the
reviewed proposal, writes the approved content, and records application.
Direct editor/shell edits and the older `proposal applied` status command
are not part of this managed flow. With the Cursor guard installed, use
Grayson MCP during open investigations; shell execution and native writes
are blocked. See [Cursor file protection](SECURITY.md#local-file-fixes-in-cursor).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/session_dark.png">
  <img src="img/session_light.png" alt="A bug-hunter session in the grayson console: analysis charts built by the agent, an open intervention awaiting a human answer, checkpoint progress with evidence">
</picture>

*A bug-hunter session, live: the agent's charts (each traceable to a query
id), an open intervention, evidence-gated checkpoints. The page refreshes
itself while agents work.*

## Defined success criteria

Agents can draft success criteria for review instead of leaving the user to fill
an empty form. MCP `criteria_set` attaches them to a pending fix; managed file
proposals also accept `success_criteria` in `proposal_draft_file`. Each criterion
names an outcome, an executed baseline query, and a numeric or no-rows rule:

```json
{
  "format": 1,
  "criteria": [{
    "name": "Duplicate IDs reach zero",
    "source_qid": "q_0007",
    "expectation": {"kind": "scalar", "column": "DUPLICATE_IDS", "operator": "eq", "value": 0}
  }]
}
```

IDs are generated when omitted. The console prepopulates an editable ID and
explains the fields through hover and keyboard-focus info widgets. Open
**Review success criteria** on the session's fix to inspect and edit the draft,
including the exact SQL, historical observation, and expected result. Approval
binds the fix and criteria together; saving a draft never approves or applies it.

The query picker starts with the current session. Browse another session by
name or choose **All sessions** to search across them, current session first.
Search matches query names, IDs, SQL, and tables; **Load more queries** pages
through longer histories. MCP `criteria_queries` provides the same lookup.
To use another session's baseline, include its `source_session` beside
`source_qid`. Both sessions must use the same connection. Grayson preserves the
source session and timestamp in review and verification evidence; choosing a
historical query does not run SQL or copy its results into the current session.

Selecting a query that reads additional tables flags **Scope approval needed**.
Saving the criteria opens a scope request for those tables. Only the user can
grant scope, using the existing intervention review. Declining or granting only
some tables leaves fix approval blocked until every required table is in scope.
After the response, the console returns to the criteria review. Granting scope
does not approve the fix; the user still reviews and approves the fix and its
criteria before application. Finding assertions retain their existing requirement
for current-session evidence touching the investigation's target tables.

After the approved fix is applied, `criteria_run` reruns the stored SQL through
the current session's guard and computes the verdict. A tolerance such as
`relative_percent: 0.1` freezes bounds within ±0.1% of the historical baseline
before approval; a zero tolerance requires exact equality.

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

The flow: session start snapshots table metadata, briefs the agent with the
relevant knowledge — ranked and capped per table, each fact carrying its
status, its role under the knowledge policy's trust, and its standing
(whether what it rests on still holds; [LIBRARY.md](LIBRARY.md#standing-pruning-and-the-knowledge-policy)) —
reports where the recorded column list has drifted from the warehouse
(`knowledge_drift`), checks QA-view coverage, and
surfaces failing external checks as pre-vetted leads ([CHECKS.md](CHECKS.md)). Checkpoints close only by citing executed
queries that touched the tables under investigation. Judgment calls go to a
human via interventions, answered in the console. Findings validate against
the workflow's schema; the human accepts or rejects each, approves proposed
fixes, and the agent proves an applied fix with a before/after comparison of
re-run queries.

**Waiting for an answer.** No harness wakes an idle agent: once it ends its
turn, only your next chat message restarts it. So "listening" for an
intervention answer is a tool call that has not returned yet —
`grayson intervention await <sid> <iid> --timeout 600` on the CLI, or the
`intervention_await` MCP tool, which blocks up to 300 seconds per call (MCP
clients time out long tool calls) and returns `waiting: true` when the
question is still open, at which point the protocol tells the agent to call
it again. If an agent stops with "waiting for your answer" and needs a nudge
after you respond in the console, it ended its turn instead of awaiting;
check it is reading the current protocol file (`grayson harness init` rewrites
it). Cursor also caps tool calls per turn and asks you to continue past that,
which a long wait can hit — a Cursor limit, not a grayson one.

**Setup inputs** are the questions a human answers before analysis starts —
the agent collects them in chat and records them with `--input key="answer"`
(MCP: `inputs`), so the session documents why it was started. A human driving
by hand can use `--interactive` prompts instead (terminal only).

**Reports** have two layers. Facts — checkpoints with evidence, findings,
proposals, charts, query stats — render deterministically; every figure
cites a query id. Charts travel with the report as their terminal rendering
by default; a profile's `charts: svg` (or `both`) writes each chart as an
SVG beside the report — `records/<sid>/charts/<id>.svg` when the close
publishes it, `charts/` beside the file for `session report --out`
(`--charts svg|both` overrides the profile) — and embeds it as an image, so
a teammate reading the library on the git host sees the picture. The
session that drew it stays local to the workspace that ran it, which is why
the option exists; on the machine that ran the session, the console's
download already has it. Presentation is a *report profile* in the library
(`--profile <name>`; see [LIBRARY.md](LIBRARY.md)). The agent's `narrate`
text renders in its own labeled section above the facts. On close, the full
report publishes into the library's `records/`.

## Resuming a session

A session outlives any agent's context window. When a harness picks a session
up cold — a new chat, a compacted window, a second worker joining late — one
call replaces re-deriving the state from six list commands, or re-running
queries whose results are already cached:

```bash
grayson session brief <sid>        # MCP: session_brief
```

The brief is assembled from the record, never from prose: identity and scope,
the guard budget used, the setup answers, every checkpoint with its evidence
(waived ones with their reason), every finding with the user's verdict and a
rejection's reason, every intervention with the user's answer, proposals with
their verification, the newest twenty executed queries with labels and
tables, the charts, the narrative draft, and readiness's next action. `text`
is the readable form; the JSON around it is the same content typed. The
protocol tells agents to read it first and to re-ask nothing it records — the
intervention answers in particular are facts a restart would otherwise lose.

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
offers it as the *Abandon* tab of the session actions bar at the foot of the
page, beside *Close*; the closed list shows it as `abandoned`.

```bash
grayson session abandon <sid> --reason "wrong target table; restarted as 2026…"
```

**Deleting** a session removes it from the workspace altogether — audit
trail, cache, charts. Records the session already published to the library
(accepted findings, verified fixes, its report) are a separate matter: they
stay unless you remove them too, which is the author's or a library admin's
call ([LIBRARY.md](LIBRARY.md#removing-records)). The *Delete* tab of the
session actions bar offers both together; from a terminal:

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

Kinds: `bar`, `line`, `scatter`, `histogram`, `correlation`; up to three
series (the palette is validated colorblind-safe at three — more dimensions
means more charts). `grayson chart render --out chart.svg` exports SVGs.

**Correlation.** A **scatter** reports Pearson *r* over every row of the
artifact and draws the least-squares line, dashed, with `r = 0.83 · n = 1200`
in the corner; below thirty usable pairs it draws the dots and no line. A
**correlation** matrix takes the numeric columns to compare (`-c col`,
repeated, two to eight; omit it to compare every numeric column of the
artifact, which is the usual shape over a `SAMPLE` artifact) and draws the
pairwise coefficients as a heatmap — hue for the sign, saturation for |r|,
the number in every cell — with `--method spearman` for ranks when outliers
or a curved relationship would fool Pearson. Both are computed locally over
the cached rows, the same trade `profile correlate` makes: the artifact's
query id is evidence, the coefficient is arithmetic grayson did on it
afterwards, and every rendering says so. The terminal form is the matrix as
a table, notable pairs called out:

```
How the measures move together  [correlation · q_0009]
              amount   quantity  discount
      amount       ·      +0.91     -0.12
    quantity   +0.91         ·     -0.08
    discount   -0.12     -0.08         ·
notable: amount × quantity r=+0.91 (n=1999)
pearson · 1999 rows · computed locally over the cached artifact, not by the warehouse
```

**Required charts.** Charting is the agent's call except where the workflow
says otherwise: a checkpoint whose content is a shape can require a chart of
given kinds ([WORKFLOWS.md](WORKFLOWS.md#required-charts)). `checkpoint list`
shows it as `requires_charts`, the console marks the open checkpoint "needs
chart", and the gate refuses to close without one built from a cited query.

A **histogram** takes the raw values of one numeric column and bins them
locally — no `GROUP BY`, no `--y`: `chart add <sid> --artifact q_0009 --kind
histogram -x amount --title "Order amounts"`. The bin count defaults from the
row count (Sturges' rule, five to thirty bins) and `--bins N` overrides it;
either way the edges are rounded to widths a reader can hold (1, 2, 2.5, 5 ×
a power of ten), so the count is near the ask rather than on it. The artifact
is whatever the query returned, so `SELECT amount FROM … SAMPLE (10000 ROWS)`
is the usual shape, and the terminal rendering states how many values were
binned with their min, median, mean, and max:

```
Order amounts  [histogram · q_0009]
          50–100 │███▋ 46
         100–150 │███████████▍ 144
         150–200 │█████████████████████████ 318
         200–250 │████████████████████████████████████ 460
 …
1999 values · 12 bins of 50 · min -2 · median 251.8 · mean 251.5 · max 503.5
```

Counts of categories are a bar chart, not a histogram; values you already
bucketed in SQL (`FLOOR(amount / 50) * 50`) are a bar chart with a numeric
x, which stays vertical.

Any chart tile enlarges in a lightbox on click (← → walk the charts in the
order the agent made them), and `⤢` opens the chart's own page: full size,
the plotted rows, the source query, and a **Download SVG** for a slide or a
ticket. The session page's live refresh waits while a chart is enlarged or a
field has focus, so a half-typed note is never lost.

Axis labels never collide and never hide what varies. How many category
labels are drawn, and how long, is computed from the plot width: labels that
would not fit are skipped, never overlapped. Whatever every label shares — a
date prefix, a schema path, a constant time part — comes off and is printed
once as a caption (`2026-08-…T00:00:00`, with `01 03 … 29` on the ticks);
dotted names shorten from the front so the table name survives
(`…PAGE_EVENTS`, not `ANALYTICS.W…`); and a label that is still cut carries
its full text inside the SVG (a `<title>`, so an exported file explains
itself) and shows it in the console on hover, tap, or keyboard focus. The
lightbox and the chart page render a *detail* size — a wider canvas with a
larger label budget — and **Download SVG** there (or
`grayson chart render --detail`) matches what was on screen.

**Bar charts lay themselves out.** Many categories (more than eight) or long
names (past twelve characters) render as **horizontal bars**: one row per
category, the labels on the y axis where they have room. Dates and numbers
read as an ordered scale and stay vertical. `--orientation vertical|horizontal`
(MCP: `orientation`) forces either. The tile shows the rows that fit and says
how many more there are (`+44 more rows`); the lightbox and the chart page
grow to hold every row. The protocol still tells agents to keep bars to a
ranked top-N in SQL — sixty bars is a table, not a picture — and the plotted
data fold has every row either way.

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
