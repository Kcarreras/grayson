# Running sessions

How an investigation actually runs: teaching your harness the protocol, the
session loop in detail, charts, and the guard settings that bound it all.
For what the rails guarantee (and their limits), see [SPEC.md](SPEC.md) and
[SECURITY.md](SECURITY.md).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/session_dark.png">
  <img src="img/session_light.png" alt="A bug-hunter session in the grayson console: analysis charts built by the agent, an open intervention awaiting a human answer, checkpoint progress with evidence">
</picture>

*A bug-hunter session, live: the agent's charts (each traceable to an executed
query id), an intervention awaiting a human answer, and evidence-gated
checkpoints. The page refreshes itself while agents work.*

## Teaching your harness the protocol

```bash
grayson harness init cursor        # or claude-code | codex | copilot
```

This writes the protocol file for your harness (a Cursor rule, a `CLAUDE.md`
section, an `AGENTS.md` section, or a `.github/copilot-instructions.md`
section) — and offers two more writes, each behind its own explicit yes:

- **Guard permissions** — deny rules so the agent calling `snow` directly, or
  reading `.grayson/` state, is blocked or prompted instead of silently
  working around the guard. Machine-written for Claude Code
  (`.claude/settings.json`), Copilot/VS Code (`.vscode/settings.json`), and
  Cursor (a hard-deny hook: `.cursor/hooks.json` + an executable
  `.cursor/hooks/grayson-guard.py`; recent Cursor IDE only) — declining the
  Cursor write prints the manual command-denylist steps to copy/paste or
  adapt instead. Codex gets human steps (its OS sandbox is the layer).
  Reversible: `grayson harness guard status|apply|remove`.
- **MCP config** — registers `grayson mcp serve` (stdio) in the harness's
  project MCP file: `.mcp.json` (Claude Code), `.cursor/mcp.json` (Cursor),
  `.vscode/mcp.json` (Copilot). Codex keeps MCP config in the user-global
  `~/.codex/config.toml`, so grayson prints the snippet instead of writing
  it. Only the `grayson` entry is ever touched. Reversible:
  `grayson harness mcp status|apply|remove`.

Harnesses that prefer typed tools use the MCP server — it mirrors the CLI
one-to-one; the served variants are in [DEPLOYMENT.md](DEPLOYMENT.md).
`grayson status` tells you where you are and what needs attention; `latest`
works anywhere a session id is expected.

Note on Copilot: `harness init copilot` targets **VS Code agent mode**
(local, human present). The cloud **Copilot coding agent** has no local
console for interventions and holds no analyst credentials — point it at a
served knowledge-only endpoint instead ([DEPLOYMENT.md](DEPLOYMENT.md)).

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
grayson session report <sid> --out report.md              # shareable report
```

Each workflow defines **setup inputs** — the questions a human answers before
analysis starts. The agent collects them in chat and records them with
`--input key="answer"` (MCP: the `inputs` dict), so the session itself
documents why it was started; they appear in the console and the report. A
human driving a session by hand can instead pass `--interactive` to be walked
through them as prompts (terminal only — agents never use it).

Session start snapshots table metadata, loads the relevant knowledge, checks
QA-view coverage, and surfaces failing external checks on the target tables as
pre-vetted leads ([CHECKS.md](CHECKS.md)). Checkpoints close only by citing
executed queries that touched the tables under investigation. When a judgment
call needs a human — labeling samples, confirming semantics — the agent files
an intervention and waits; the answer arrives from the console. Findings
validate against the workflow's schema; the human accepts or rejects each one,
approves proposed fixes, and the agent proves an applied fix with a
deterministic before/after comparison of re-run queries.

## Honest endings

Two things a gate must allow, or it starts manufacturing the evidence it exists
to demand.

**A check that does not apply.** Freshness on a static reference table, error
patterns when there were no errors — the agent's only other route past the gate
is a query picked to satisfy the relevance test rather than to learn anything.
So a checkpoint can be **waived**: the agent files an intervention saying why it
does not apply, and a human waives it with a reason on the record. Waived is not
complete — it shows as its own status, with its reason and the name of whoever
granted it, everywhere checkpoints are reported.

```bash
grayson checkpoint waive <sid> freshness --reason "static reference table"
```

**A run that finds nothing.** Every stage from `fixes` onward needs an accepted
finding, which is right for a session that found something and wrong for one
that did not — it leaves "invent a finding" as the only way to finish. A clean
run instead closes as a **clean outcome**: required checks cleared, nothing
accepted, nothing left for the user to judge. `grayson session readiness <sid>`
reports when that is the available route (`clean_close_available`,
`next_action`), and the console offers the button.

```bash
grayson session close <sid> --clean --note "all four checks came back sound"
```

Every human boundary is a **user** action: accepting or rejecting a finding,
approving a fix, answering an intervention, confirming a knowledge fact, waiving
a check, forcing a gate, and closing the session. The agent asks; the human
decides. Because the CLI is genuinely both interfaces and cannot see who is
calling it, all of these require an interactive terminal — an agent shelling out
is refused by every one of them — and the audit trail attributes each action to
whoever actually took it rather than assuming the human.
Recording a clean result is the point of the ceremony: "we looked and it was
fine" is knowledge the next session should start with.

## Profiling

The descriptive battery — per-column nulls, cardinality, ranges, key candidates,
value frequencies — is the same on every table, and hand-rolling it is both
expensive and unreproducible: forty single-column queries burn the budget, and
their ids differ every run.

```bash
grayson profile table <sid> DB.SCHEMA.TABLE
```

Aggregates compose, so this costs three or four statements rather than forty:
one `DESCRIBE`, one wide `SELECT` carrying every column's aggregates, one
`UNION ALL` of value frequencies for the low-cardinality columns, and one
sample. Each runs the ordinary guarded path, so the returned `q_XXXX` ids are
evidence like any other and close checkpoints directly. The response's
`observations` are mechanical leads — "nearly unique but not quite, 3 rows
beyond the distinct count", "null in 8.6% of rows" — never verdicts. Whether a
sparse column is a defect depends on what it is for, which grayson does not know
and will not guess.

Two statistics do not fit in portable SQL, and one of them is quadratic:

```bash
grayson profile stats <sid> <sample-qid>       # mean, stdev, quantiles
grayson profile correlate <sid> <sample-qid>   # pairwise, pearson | spearman
```

Both compute locally over a cached artifact, which is why they are cheap:
pairwise correlation across 30 columns is 435 pairs, and asking the warehouse
would cost hundreds of queries to answer what one cached sample already
contains. **The evidence chain is weaker here and says so.** A warehouse query
is audited end to end; a local statistic is "this artifact, plus arithmetic
grayson did afterwards", and it describes the sample rather than the table. Both
responses carry `computed: "local"`, a confidence ceiling, and a caveat to pass
on — cite the sample's query id, say the number was computed locally, and
confirm anything decisive against the warehouse before resting a
high-confidence finding on it.

## Analysis charts

Agents chart cached query results as they work — `grayson chart add` (MCP:
`chart_add`) validates the column mapping against the artifact's real shape and
the console renders the chart on its live refresh. Every chart carries the
`q_XXXX` id of the query behind it and a "plotted data" fold with the exact
rows drawn.

The same chart comes back as a Unicode rendering the agent pastes into its chat
reply, so the shape shows up in the conversation too:

```
NULL email rate by day  [line · q_0007]
null_rate ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁█████  min 0.007 · max 0.1027 · last 0.0973
       x: 08-01 → 08-24

NULL emails by order source  [bar · q_0008]
      mobile_app │████████████████████████████████████ 702
    web_checkout │████▍ 84
      pos_import │█▎ 22
     api_partner │▎ 4
```

Kinds: `bar`, `line`, `scatter`; up to three series (the palette is validated
colorblind-safe at three slots in both console themes — more dimensions means
more charts, not more colors). `grayson chart render --out chart.svg` exports
standalone SVGs.

## Settings and guard profiles

Workspace configuration lives in `grayson.toml` — committed, reviewable,
diffable. Two surfaces change it, both human: `grayson config` and the
console's Settings page. The MCP surface exposes configuration **read-only**
by design — an agent that can loosen its own guards has no guards.

```bash
grayson config show
grayson config set defaults.guard_profile=strict scopes.strict=true
grayson config profile overnight --auto-limit 0 --budget-cap 500
```

Guard profiles bundle three independent cost controls — auto-`LIMIT`,
per-statement timeout, per-session query budget — selected per session,
editable per workspace. Scope is per session: out-of-scope reads warn by
default; `strict` mode blocks them.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/settings_dark.png">
  <img src="img/settings_light.png" alt="The Settings page: connection, default guard profile, strict scope, editable guard profiles, team library controls">
</picture>

Light/dark theme is per-browser (the toggle in the console's top bar), not a
workspace setting.

## Auditing what ran

Every statement — accepted or rejected — lands in the session's audit log with
hash, worker, verdict, and stats. `grayson audit reconcile` (human-only; no MCP
twin) diffs the warehouse's own query history against that trail and flags
statements that ran around grayson; `--ingest` records the verdict as an
external check. Details: [SECURITY.md](SECURITY.md).
