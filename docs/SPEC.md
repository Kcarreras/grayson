# grayson — Specification v1

Agentic, open-ended QA and investigation over SQL tables and data (Snowflake-first).

grayson is **deterministic infrastructure for agent-driven data QA**. It provides guarded
Snowflake access, session state, evidence enforcement, cached data with freshness tracking,
a team-shareable knowledge library, and a human-in-the-loop web console. All reasoning is
done by agents in the user's harness (Cursor, Claude Code, Codex, …); grayson itself never
calls an LLM.

---

## 1. Core principles

1. **Deterministic core.** grayson holds guardrails, state, storage, and UI. Intelligence
   lives in harness agents steered by thin per-harness skill files that grayson generates.
2. **Harness-agnostic.** Primary interface is a CLI (`grayson …`) returning structured
   output; an MCP server exposes the same operations as typed tools. Both are thin
   wrappers over one shared core — behavior is identical.
3. **Guarded, not hobbled.** Agents get real analytical freedom (arbitrary read queries,
   CTEs, aggregates, sampling) inside airtight walls (no writes, scoped objects, cost
   caps). The open-ended stages are where agents deliver value; guards protect the
   warehouse, not the agent's reasoning.
4. **Evidence or it didn't happen.** Checkpoints and findings cannot close without linked,
   executed-query evidence. This is the deterministic "QA of the QA."
5. **Human approval at the boundaries.** DDL execution, fix application to definition
   files, and session sign-off are always user actions.
6. **Everything inspectable.** Sessions, findings, knowledge, and query logs are plain
   files in the workspace, readable in any IDE.

## 2. Decisions

| Decision | Choice |
|---|---|
| Form factor | Python package (uv-managed) + CLI + MCP server + localhost web UI |
| Intelligence | Purely deterministic; zero LLM calls in grayson |
| Snowflake access | Via `snow` CLI named connections; SSO/external-browser auth; grayson never stores credentials |
| Roles | Must work under user's normal role today (parser is the only wall → airtight); read-only role supported/preferred when available |
| Writes to warehouse | None by agents. QA views come from a **view library**; missing views are **proposed** by agents and **executed by the user**, front-loaded at session setup |
| Table definitions | Mixed sources: git-repo SQL files (fix = file diff) and Snowflake-resident logic (fix = standalone DDL snippet) |
| Fix application | User approves in UI → **harness agent** edits work-repo files with its own tools; grayson never writes outside its workspace |
| Parallelism | Up to ~3 concurrent sessions; optional multi-agent fan-out **within** a session, toggleable per session |
| Cost guards | Three independent toggles (auto-LIMIT, timeout, query budget) combined into named, user-saveable guard profiles; selected at session start, suggested by workflow type and prior usage |
| Result caching | Freely cached locally, gitignored, timestamped, freshness-checkable |
| Workflows v1 | Bug Hunter, Pipeline/Transform QA, Single-Table Health, Semantic Rule QA, Migration/Parity |
| QA of QA | Deterministic evidence enforcement (state machine + schemas) |
| Knowledge library | Team-shared design from day one: provenance on every fact, merge-friendly files |
| UI | Full session console: interventions, checkpoints, query log, findings, approvals |
| Work environment | No known constraints; keep dependencies lean and pinned anyway |

## 3. Architecture

```
┌─────────────────────────────┐        ┌──────────────────────────┐
│ Harness agents               │        │ User                     │
│ (Cursor / Claude Code / …)   │        │ (browser)                │
└──────┬───────────┬──────────┘        └───────────┬──────────────┘
       │ CLI       │ MCP (stdio)                    │ http (127.0.0.1)
┌──────▼───────────▼────────────────────────────────▼──────────────┐
│                        grayson core (Python)                       │
│  sessions · guard · executor · cache · workflows · checkpoints    │
│  interventions · findings · proposals · knowledge · views         │
├──────────────────────────────────────────────────────────────────┤
│  workspace files (.grayson/)          │  snow CLI (subprocess)     │
│  state: SQLite (WAL) per session     │  → Snowflake               │
│  artifacts: sqlite/markdown/yaml     │                            │
└──────────────────────────────────────────────────────────────────┘
```

**Components** (all in one package, `src/grayson/`):

- `core/` — session state machine, workspace management, locking
- `guard/` — SQL validation (sqlglot, Snowflake dialect), guard profiles, audit log
- `executor/` — snow CLI subprocess wrapper, auth-state detection, result ingestion
- `cache/` — result storage (SQLite + JSON sidecar), freshness metadata, lookup
- `workflows/` — workflow templates, checkpoint definitions, findings schemas
- `knowledge/` — knowledge library read/propose/confirm, provenance
- `views/` — QA view library registry, coverage check, view proposals
- `checks/` — external check results: validation, latest/summary, ingestion (§11b)
- `charts/` — chart specs over cached artifacts + stdlib SVG rendering (§8a)
- `interventions/` — structured human-input tasks (replaces CSV round-trips)
- `proposals/` — fix proposals (file diffs and DDL snippets), approval state
- `cli/` — Typer CLI (agent- and human-facing)
- `mcp/` — MCP server (official `mcp` SDK, stdio transport) wrapping the same core
- `ui/` — FastAPI app + server-rendered console, bound to 127.0.0.1
- `harness/` — generators for per-harness skill/instruction files

**Dependencies** (pinned via uv.lock): `sqlglot`, `pydantic`, `typer`, `fastapi`,
`uvicorn`, `mcp`, `jinja2` (UI templates). Result storage and local analysis use
stdlib `sqlite3` — no native DLLs beyond Python itself, so grayson runs under
locked-down Windows Application Control policies common on work machines (duckdb
was evaluated and is blocked by such policies).
Snowflake CLI (`snow`) is an external prerequisite, not a Python dependency.

## 4. Workspace layout

A grayson **workspace** is a directory (typically its own repo, or a folder inside one)
opened in the IDE alongside the user's SQL repos:

```
<workspace>/
├── grayson.toml                 # connection, defaults, scopes, guard profiles, [library] pointer
├── knowledge/                  # LIBRARY ASSET — team-shareable knowledge
│   ├── glossary.md
│   └── <db>/<schema>/<table>.md
├── views/                      # LIBRARY ASSET — QA view library
│   ├── registry.yaml           # view name → purpose, source tables, base files, DDL path, created_at
│   └── ddl/*.sql
├── workflows/                  # LIBRARY ASSET — workflow template overrides/custom types
├── checks/                     # LIBRARY ASSET — external check results (Airflow, dbt, …)
└── .grayson/                    # sessions & data
    └── sessions/<id>/
        ├── state.db            # SQLite (WAL): state machine, event log, locks
        ├── session.md          # human-readable session brief & status (generated)
        ├── queries/            # every executed statement: sql + result metadata
        ├── data/               # cached results (results.db + sidecars)  [gitignored]
        ├── charts/             # chart specs built from cached artifacts (§8a)
        ├── interventions/      # tasks + structured responses
        ├── findings/           # findings docs (schema-validated)
        └── proposals/          # fix proposals + approval state
```

`.grayson/sessions/*/data/` is gitignored; everything meant to compound over time
(knowledge, views, workflows, checks) is committed and merge-friendly (one file per table/view,
provenance inline).

**Library assets** live in the workspace by default (**solo mode**). In **team mode**,
`grayson.toml` declares a `[library]` pointer to a local clone of a shared library repo,
and grayson resolves `knowledge/`, `views/`, `workflows/`, `checks/`, and shared guard
profiles from
there instead — see §11a.

## 5. Session lifecycle (state machine)

```
setup → analysis → synthesis → review → fixes → verification → closed
```

1. **setup** — user (or agent relaying user input) declares: workflow type, target
   tables, guard profile, parallelism (worker count), connection. grayson verifies snow
   auth, snapshots table metadata (columns, row counts, `last_altered`), loads relevant
   knowledge, runs the **view coverage check** (see §9a): existing library views
   relevant to the target tables **enter the session's query scope automatically**
   (reported as `views_in_scope`), stale ones are flagged for refresh, and gaps
   become new-view DDL proposals assembled from the registry's base-file pointers —
   DDL is executed by the user, front-loaded so analysis isn't interrupted. Coverage
   is informational; it does not gate the setup stage.
2. **analysis** — the open-ended core. Agents (1..N workers) run guarded queries, cache
   results, log observations, request interventions when human judgment is needed.
   Workflow-defined **required checks** must each be completed with evidence; beyond
   those, agents are free.
3. **synthesis** — findings drafted against the workflow's findings schema; every claim
   must cite query evidence. grayson validates structure + evidence links.
4. **review** — evidence gate: all required checkpoints closed, all findings validated,
   all interventions resolved. Presented to user in UI; user accepts findings, or
   rejects them with a required reason the agent continues from. Findings are
   immutable: a corrected finding may carry `supersedes: f_00X`, but that is a
   proposal — the supersession executes only inside the user's accept of the new
   finding (agents cannot perform it), the superseded finding stops counting as
   accepted for every gate, and the full chain stays visible as history.
5. **fixes** — agents write proposals (`file_diff` or `ddl_snippet`, each linked to the
   finding it addresses and payload-validated). User approves/rejects per proposal in
   UI/CLI. Approved file-diffs are applied by the harness agent in the work repo (grayson
   never writes outside its workspace); the agent marks the proposal `applied`; user
   reruns definitions.
6. **verification** — the agent re-runs the anomaly/parity query post-fix and records a
   verification on the proposal citing before and after query ids. grayson computes the
   before/after comparison deterministically (`compare_artifacts`: row-count delta,
   emptiness, value identity for small sets) and enforces that both ids were actually
   executed; the pass/fail verdict rides on that evidence (`verified` /
   `verification_failed`).
7. **closed** — session summary generated; durable learnings promoted to the knowledge
   library (user-confirmed facts marked as such). Closing records an **outcome**:
   `findings` (closed on accepted findings) or `clean` (required checks cleared and
   nothing found worth acting on). A clean close is a user action — a human vouching
   for a negative result — and exists so that a run which finds nothing has a way to
   finish that is not "invent a finding". A forced close records no outcome at all.

Any stage can loop back (verification failure → fixes/analysis). All transitions are
recorded in the event log with actor (user / agent worker id) and timestamp.

**Who moves the marker.** `setup → analysis` happens automatically when the first
statement executes (actor `system`) — the stage strip tracks reality without relying
on the agent. Every later transition is declared via `session advance`; the gates in
§enforcement (checkpoints before review, an accepted finding before fixes) are checked
on the *target* index, so declaring a late stage cannot skip them and agents cannot
`--force` past them.

**Parallelism.** Sessions are isolated by directory. Within a session, workers register
(`grayson worker join`) and get an id; state mutations go through SQLite (WAL +
busy-timeout) so concurrent workers never corrupt state. Queries, observations, and
findings are tagged by worker. Worker count is declared at setup and changeable by the
user mid-session.

## 6. Query guarding

The guard sees **every** statement before execution; there is no unguarded path.

- **Parse, don't pattern-match.** sqlglot with the Snowflake dialect. Unparseable →
  rejected with the parse error (agent can fix and retry). Multi-statement → rejected.
- **Statement allowlist**: `SELECT` (incl. CTEs, set ops, sampling), `SHOW`, `DESCRIBE`,
  `EXPLAIN`. Everything else — DML, DDL, `CALL`, `COPY`, `USE`, session/account
  alterations — rejected. Rejections name the rule and suggest the compliant path
  (e.g. "propose a view instead").
- **Function denylist**: side-effecting/scope-bypassing functions are rejected even
  inside a legal SELECT — the whole `SYSTEM$*` family (session/query mutation:
  `ABORT_SESSION`, `CANCEL_QUERY`, `WAIT`, …) and `RESULT_SCAN` (reads arbitrary prior
  results by id, bypassing scope). UDTF/table-function row sources (`TABLE(udtf(...))`)
  are scope-invisible, so they are blocked in strict mode and warned otherwise; built-in
  table functions (`GENERATOR`, `FLATTEN`, …) are allowed. Ordinary analytical scalar
  functions are unaffected.
- **Strict-scope completeness**: in strict mode, *unqualified* table names (which
  Snowflake would resolve against the connection's current namespace) are blocked, not
  just warned — an unverifiable reference is treated as out-of-scope.
- **Object scoping**: referenced objects are extracted and checked against the session's
  registered scope (target tables, view-library views, `INFORMATION_SCHEMA`, and
  scopes whitelisted in `grayson.toml`). Out-of-scope reads produce a *warning* by
  default (logged, surfaced in UI) and a hard block only if the session was started
  with `--strict-scope` — this is the "don't get agents in trouble for overly tight
  rules" dial.
- **Guard settings** are three *independent* controls, each individually toggleable and
  tunable — any combination is valid:

  | Control | Options |
  |---|---|
  | `auto_limit` | off, or N rows injected on unbounded raw-row SELECTs (aggregate-only queries bypass it) |
  | `timeout` | off, or N seconds via `STATEMENT_TIMEOUT_IN_SECONDS` per statement |
  | `query_budget` | off, warn-at-N, or hard-cap-at-N per session (hard cap user-extendable from the UI) |

- **Guard profiles** are named, saved combinations of those settings, defined in
  `grayson.toml` (committed, so profiles travel with the workspace). grayson ships
  starter profiles (`strict`, `moderate`, `generous`) the user can edit, clone, or
  replace. Selection at session setup is one pick — `--guard-profile <name>` or a
  dropdown in the UI — with per-setting overrides allowed on top
  (`--timeout 300`). **Default selection**: workflow templates suggest a profile, but
  if the session's target tables/views were used in a previous session, grayson defaults
  to the profile used there (last-used wins), shown as "suggested" so the pick is
  one keystroke to accept or change.
- **Audit log**: every statement — accepted or rejected — is recorded with hash,
  worker id, timestamp, guard verdict, and execution stats.
- **Defense in depth**: designed to be the only wall (normal role today) but pairs with
  a read-only Snowflake role when available (`grayson.toml` records which is in play).

## 7. Execution & auth

- Executor invokes `snow sql` as a subprocess (argument lists, never `shell=True`) with
  the configured named connection; results ingested from JSON output.
- **Auth detection**: auth/token errors are recognized and surfaced as a distinct
  `AUTH_REQUIRED` state — in the CLI/MCP response (so agents pause and say so, instead
  of retrying into MFA fatigue) and as a banner in the UI. The user re-auths via
  Snowflake CLI's browser flow; its token caching keeps SSO pop-ups rare even with
  parallel workers.
- grayson never reads, stores, or transmits credentials; that surface belongs entirely
  to Snowflake CLI.

## 8. Result cache & freshness

Every executed query's results are stored automatically:

- **Format**: rows land as table `q_XXXX` in the session's `results.db` (stdlib
  SQLite), plus a JSON sidecar per artifact:
  `{query_hash, sql, executed_at, worker, source_tables, row_count,
  truncated, source_last_altered: {table: ts}}`.
- **Freshness**: `grayson cache find --tables …` (and the MCP equivalent) returns matching
  cached artifacts with a computed staleness verdict — current `last_altered` (one cheap
  metadata query) vs. the value captured at execution time → `fresh` / `stale` /
  `unknown`. Agents are instructed (via skills) to check cache before querying; the
  decision to reuse stays with the agent.
- **Local analysis**: cached artifacts are queryable locally (`grayson cache query`,
  table names = artifact ids), letting agents re-slice already-fetched data without
  warehouse round-trips. Same guard posture: single SELECT only, artifact tables only,
  and the connection is opened read-only (SQLite `mode=ro`) as a second wall.

## 8a. Analysis charts

Agents make their analytical process *visible* as it happens. `grayson chart add`
(MCP: `chart_add`) builds a chart — `bar`, `line`, or `scatter` — from a cached
artifact: the spec (artifact id, column mapping, title, one-line read) is validated
against the artifact's real shape (columns exist, measures are numeric) and stored in
the session. The console renders charts server-side as dependency-free SVG on its
live-refreshing session page, so the user watches the investigation's visual
narrative build in near real time — and because the artifact is an executed query,
every picture is traceable evidence (chart card shows the q_XXXX chip; a "plotted
data" fold shows the exact rows drawn).

Deterministic by construction: grayson draws exactly what the cited query returned —
agents shape the data with SQL (`query run` / `cache query`), then chart the result.
Up to 3 series per chart (the categorical palette validates colorblind-safe at three
slots in both console themes); more dimensions means more charts, not more colors.
`grayson chart render --out chart.svg` exports any chart standalone.

Axis labels never hide what varies: the affix every category label shares is
stripped and captioned once under the axis, dotted identifiers shorten from the
front, and any label still cut carries its full text in the markup (`<title>` +
`data-full`) for the console's hover tip and for exported files. Two layouts
render from the same data — the tile (session page, exports) and a *detail* size
with a larger label budget (chart page, lightbox, `chart render --detail`).

**Terminal rendering.** Harness chats (Cursor, Claude Code, Codex) display text, not
images, so `chart add` / `chart_add` responses also carry `text`: a Unicode rendering
of the same points — labeled block bars, per-series sparklines with min/max/last, a
dot grid for scatter — and the protocol tells agents to paste it into their chat reply
in a code block. The user sees the shape in the conversation immediately; the console
shows the full chart on its live refresh; both cite the same artifact.

## 9. Workflows & checkpoints

Workflow templates are data (YAML), not code — shipped defaults, overridable/extendable
in `workflows/`:

```yaml
name: bug-hunter
description: Replicate a user-reported anomaly and isolate its source.
suggested_guard_profile: moderate
setup_inputs: [anomaly_description, example_rows_or_filter, affected_tables]
required_checks:            # checkpoints that must close WITH evidence
  - replicate_anomaly       # reproduce the phenomenon in a query
  - scope_blast_radius      # how widespread; which partitions/dates/keys
  - upstream_trace          # walk lineage until source isolated
    depends_on: [replicate_anomaly]   # no cause-hunting until it reproduces
  - rule_out_alternatives   # ≥2 competing explanations tested
suggested_checks:           # breadth, surfaced but never gated
  - onset_dating            # when the anomaly first appears
findings_schema: bug_hunter_v1   # closed-ended output structure
```

Analysis is the open stage: beyond the required checks, agents are unconstrained.
Required checks gate; **suggested checks** carry breadth without gating, so a workflow
can name thirty fundamentals without demanding all thirty close with evidence on a
five-column lookup table. `depends_on` expresses the rare genuine ordering.

v1 templates: `bug-hunter`, `pipeline-qa`, `table-health`, `semantic-rule-qa`,
`migration-parity`, `table-onboarding`, `feature-readiness`. Each defines setup inputs, required checks, intervention patterns,
and a findings schema. `migration-parity` doubles as the built-in verification stage for
every other workflow.

**Checkpoints** close only via `grayson checkpoint complete <name> --evidence q_017,q_023`
— grayson verifies the cited queries exist, succeeded, and touched relevant objects.
A check that genuinely does not apply to the target is **waived** by a user with a
mandatory reason (agents request one via an intervention); waived satisfies the gate but
is reported as its own status, never as complete. Without that exit, an inapplicable
required check leaves the agent only one route past the gate — a query chosen to satisfy
the relevance test — which is precisely the laundering the gate exists to stop.
**Findings** are pydantic-validated documents: summary, severity, affected objects,
evidence (query ids), reproduction, proposed remediation, confidence + open questions.

## 9b. Profiling primitive

The descriptive battery is identical on every table, so it is generated rather than
hand-written: `grayson profile table` emits DESCRIBE, one wide aggregate SELECT per
batch of columns, one UNION ALL of value frequencies for low-cardinality columns, and
one sample — three or four statements where the naive shape is one query per column
per statistic. Everything runs the ordinary guarded path, so the artifacts are evidence
and their query ids close checkpoints; grayson computes the numbers and states flat
`observations`, and interprets nothing.

Quantiles and pairwise correlation are computed locally over a cached sample instead
(`profile stats`, `profile correlate`): portable SQL cannot express them, and pairwise
over N columns is quadratic in warehouse cost. The trade is explicit in the response —
`computed: "local"`, a confidence ceiling, and a caveat — because the sample's query id
is audited evidence while the statistic derived from it is not, and a correlation looks
like a measurement of the table when it is a measurement of the sample.

## 9a. QA view library

The view library is how agents get analysis-ready surfaces without ever holding DDL
rights. `views/registry.yaml` records, per view: name, purpose, source tables,
**base files** (paths/globs into the user's work repos where the underlying definition
logic lives), the DDL file in `views/ddl/`, created_at, and the source tables'
`last_altered` at creation time.

**At session setup** the coverage check produces a three-part picture:

1. **Reuse** — library views matching the session's target tables. These enter the
   session's query scope automatically (`views_in_scope`): querying them passes the
   guard — including strict-scope mode — and evidence touching them counts toward
   checkpoints and findings. Mid-session, `grayson views use <sid> <name>` (MCP:
   `views_use`) scopes in additional *registered* views; arbitrary unregistered
   names are refused, so scope only ever widens to user-curated surfaces.
2. **Refresh** — stale views: a source table's `last_altered` has moved past the
   baseline captured when the view was registered. The baseline is captured at
   registration (`views register`, one cheap metadata query; `--no-snapshot` opts
   out) and by the automatic registration path below. Detection runs at session
   start and on demand via `views check --check-freshness`.
3. **Create** — for gaps, agents assemble proposed DDL. The registry's base-file
   pointers (plus per-table `definition_files` entries in the knowledge library) tell
   agents exactly which work-repo files to read when deriving new view logic.
   Proposals are queued for user execution.

`grayson views list|show|check|register|use` (and the MCP `views_check`/`views_use`)
expose the same operations mid-session. **Registration closes the loop
automatically**: a `ddl_snippet` proposal that declares `view_name` (plus
`source_tables`, `base_files`, `purpose`) is registered into the library — DDL,
sources, and staleness baseline — the moment it is marked `applied`, which can only
follow user approval; the new view also joins the session scope so verification
queries against it count as evidence.

## 10. Interventions (human-in-the-loop)

Structured tasks replacing the CSV round-trip:

- Agent files an intervention: type (`label_sample`, `confirm_semantics`, `choose`,
  `free_response`), payload (e.g. sample rows + label options), and what it will do with
  the answer.
- UI renders it as an interactive task (tabular labeling with keyboard flow, option
  pickers, text). Responses are stored as structured JSON the agent reads back.
- CLI/MCP: `grayson intervention await` (poll/block) so agents in any harness can wait on
  human input; parallel workers continue on other angles meanwhile.

## 11. Knowledge library

Team-shared from day one. One markdown file per table (merge-friendly), YAML-front-
mattered facts with **provenance**:

```markdown
# analytics.web.page_events

## facts
- id: url_category_source
  fact: "`url_category` is assigned by regex rules in categorize_urls.sql; 'other' is the fallback, expected <5%."
  status: user_confirmed          # user_confirmed | data_inferred | proposed
  confirmed_by: kane
  confirmed_at: 2026-08-20
  evidence: [session 0142 q_031]
```

- Agents **must** load relevant knowledge at setup and **propose** new facts when they
  infer or are told something durable. `data_inferred` facts carry evidence links;
  promotion to `user_confirmed` happens via the UI or `grayson knowledge confirm`.
- Agents are instructed to ask (via interventions) when semantics can't be derived from
  data — answers become knowledge, so every session makes the next one faster.
- Search: `grayson knowledge search <term>` (and MCP tool) over facts + glossary.

## 11a. Team library & distribution model

Collaboration needs no server; it rides on git. Three kinds of repo, kept separate:

1. **The grayson tool** (this repo) — installed per user, e.g.
   `uv tool install grayson-sql` or `uvx --from git+https://github.com/Kcarreras/grayson grayson`.
   Never cloned into a workspace; it's software, not data.
2. **A team library repo** — one per team, holding the compounding assets:
   `knowledge/`, `views/`, `workflows/`, `checks/`, and shared guard profiles.
   `grayson library init` scaffolds a fresh one ready to push to the team's git host.
   Another team starting out scaffolds their own — or forks an existing team's library
   to seed from their knowledge.
3. **Personal workspaces** — one per user (sessions, cached data, local config), each
   linked to a local clone of the team library:

   ```toml
   [library]
   path = "~/work/data-qa-library"   # local clone of the team library repo
   ```

**Bootstrap**: the team repo starts as an *empty* repo on the git host.
`grayson library link <git-url>` clones it, scaffolds the asset structure, commits,
and pushes that first commit automatically (only for clones grayson itself made —
linking an existing local directory never auto-commits). Teammates then run the
same command and receive the structure.

**Resolution**: with `[library]` set, grayson reads/writes knowledge, views, workflows,
checks,
and shared profiles in the library clone; session state and cached data stay in the
personal workspace. Solo mode (no `[library]`) keeps everything in the workspace, and
`grayson library extract` can later split the assets out into a new library repo when a
team forms.

**Freshness**: at session setup grayson checks the library clone against its remote and
warns if it is behind ("library is 12 commits behind origin — pull before starting?")
or has uncommitted local changes. `grayson library status|pull` wrap the corresponding
git operations; commits/PRs to the library go through normal git tooling — a PR is the
team-scale version of the fact-confirmation flow, giving proposed knowledge a review
step for free.

**Known limits (accepted for v1)**: knowledge propagates at pull cadence, not real
time; there is no cross-user live session visibility or central query audit (each
user's audit log is local; closed-session summaries may be committed to the library if
a team wants shared history); simultaneous view registration by two users reconciles
at merge time. If those ever become must-haves, a central service can be added behind
the same file formats without reworking this architecture.

## 11b. External checks library

Teams already run scheduled deterministic checks outside grayson — Airflow DAGs, dbt
tests, data-quality jobs. The `checks/` library asset makes those results agent
context with zero coupling: automation dumps JSON files anywhere under `checks/`
(single result, list, or `{"results": [...]}`; format documented in the scaffolded
`checks/README.md`), and grayson reads, validates, and reports them. grayson never
runs the checks; malformed entries are reported per-file without hiding the rest.

- **Session-start surfacing** — `session start` returns `external_checks` for the
  target tables: latest result per check, with failing checks carried in full
  (details, metrics, and the check's own SQL) plus a hint telling the agent to
  *replicate the failing checks first* — deterministic findings become pre-vetted
  leads for the open-ended investigation (e.g. a bug-hunter session starts from
  what the Airflow suite already caught).
- **Overdue detection** — a result may declare `ttl_hours` (its expected cadence);
  a latest run older than that is flagged overdue, so silently-stopped automation
  is itself surfaced.
- **Ingestion** — automation can write files directly, or pipe through
  `grayson checks ingest <file|dir> [--source airflow]`, which validates, fills in
  the source, folds results into `checks/ingested/<check_id>.json` (idempotent per
  (check, run_at), history bounded), and auto-pushes when the library is configured
  for it. With a linked team library, one teammate's check drops brief everyone's
  agents at pull cadence.
- **Surface** — `grayson checks status|list|show|ingest`, MCP `checks_status` /
  `checks_show`, a Checks tab in the console, and per-table checks on each
  knowledge page.

## 12. Web console (UI)

FastAPI + server-rendered pages (Jinja2; no Node build chain), `127.0.0.1` only, with a
per-launch session token in the URL. v1 views:

- **Sessions dashboard** — active/recent sessions, stage, checkpoint progress, pending
  items, auth status.
- **Session setup panel** — guard-profile dropdown (suggested default pre-selected,
  per-setting overrides), view pick-list with refresh flags, pending DDL to execute,
  base-file pointers.
- **Session detail** — checkpoints w/ evidence, live query log (statement, verdict,
  rows, duration, worker), cached artifacts, event timeline, and the analysis-chart
  gallery (§8a) refreshed on the page's live cycle.
- **Interventions inbox** — pending tasks; interactive labeling/confirmation forms.
- **Findings review** — rendered findings with evidence drill-down; accept per finding.
- **Proposals** — diffs/DDL rendered side-by-side with the finding they fix;
  approve/reject; verification results after rerun.
- **Workflows** — the catalog: core + team templates, create-or-fork, per-workflow
  detail pages.
- **Knowledge** — browse/search; confirm or edit proposed facts.
- **Checks** — latest external check results (§11b): failures first with details
  and check SQL, overdue automation flagged, per-table checks on knowledge pages.
- **Settings** — edit the workspace rails (`grayson.toml`): connection, default
   guard profile, strict scope, allowed scopes, per-profile guard controls, and
   team-library controls (auto-push, pull/push, sync state). Settings mutation is
   a *human* surface: this page and the `grayson config` CLI are the only writers;
   the MCP tool surface exposes configuration read-only (`config_show`) so agents
   cannot loosen the guards they run inside. Light/dark theme is per-browser (a
   nav toggle backed by localStorage), not workspace state.

## 13. Harness integration

- `grayson harness init cursor|claude-code|codex|copilot` generates the skill/instruction
  files (e.g. `.cursor/rules/`, `CLAUDE.md` section, `.github/copilot-instructions.md`
  section, or skills) that teach that harness the
  session protocol: check knowledge → setup → cache-before-query → evidence discipline →
  interventions → findings → proposals. The protocol lives in one canonical template so
  all harnesses stay in sync.
- MCP server (`grayson mcp serve`, stdio) for harnesses that prefer typed tools; tool set
  mirrors the CLI 1:1. Where the harness keeps MCP config in a project file (Claude Code
  `.mcp.json`, Cursor `.cursor/mcp.json`, Copilot `.vscode/mcp.json`), grayson can write
  the server entry on explicit consent (`harness init` offer or `harness mcp apply`).
- Everything an agent can do via MCP it can do via CLI — harnesses without MCP support
  lose nothing.

## 14. Security posture

- No credential storage or handling (delegated to Snowflake CLI).
- Subprocess calls use argument vectors; no shell interpolation of agent input.
- Guard is default-deny by statement type; audit log is append-only.
- UI binds to loopback only; token-gated; no remote assets (works offline). Its
  one asset bundle - Cytoscape.js + ELK for the relationship canvas - is vendored
  under `ui/static/vendor` and served from loopback; no CDN, no build step.
- Workspace writes are confined to the workspace; grayson never edits files outside it.
- All file reads/writes validate paths against the workspace root (no traversal).
- Dependencies pinned via `uv.lock`; small, well-known set.
- Cached warehouse data is gitignored by generated `.gitignore`; a `grayson session
  scrub` command deletes a session's cached data on demand.

## 15. Status

All planned phases are implemented and tested — core/guard/executor/cache,
workflows + evidence enforcement, the console + interventions, proposals +
verification, knowledge/view/checks libraries, records + reports, and the
MCP server + harness generators. The suite includes an adversarial guard
suite and the multi-agent security reviews logged in
[SECURITY.md](SECURITY.md).

One divergence from the original spec: result storage uses stdlib SQLite
rather than duckdb/Parquet — duckdb's native DLL is blocked by common
Windows Application Control policies (§8).

---

*Converged 2026-08-20. Deliberately deferred: read-only role adoption, any
central collaboration service (the git-based library model in §11a is v1).*

## Repository layout

```
grayson/
├── src/grayson/
│   ├── guard/        # SQL statement guard (the airtight wall)
│   ├── executor/     # snow CLI execution + auth detection
│   ├── cache/        # result storage, freshness, guarded local analysis
│   ├── core/         # session state machine, evidence engine, proposals
│   ├── workflows/    # YAML workflow templates, registry, lint, authoring
│   ├── findings/     # findings schemas
│   ├── interventions/# human-in-the-loop task types
│   ├── knowledge/    # team knowledge store
│   ├── views/        # QA view library + coverage checks
│   ├── checks/       # external check results (Airflow, dbt, …)
│   ├── charts/       # chart specs + SVG/terminal renderers
│   ├── ui/           # FastAPI + Jinja2 local console (loopback, token-gated)
│   ├── mcp/          # MCP servers (full + knowledge-only; tools mirror the CLI)
│   ├── harness/      # per-harness protocol files + guard permissions
│   ├── library.py    # team library repo: linking, sync, freshness
│   ├── records.py    # cross-session records + library publication
│   ├── identity.py   # per-user id for attribution
│   └── audit.py      # warehouse-history reconciliation
├── docker/           # knowledge-appliance image + compose
├── tests/            # pytest suite
└── docs/             # SESSIONS, WORKFLOWS, LIBRARY, CHECKS, DEPLOYMENT, SECURITY, SPEC
```
