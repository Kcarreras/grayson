# seekql — Specification v1

Agentic, open-ended QA and investigation over SQL tables and data (Snowflake-first).

seekql is **deterministic infrastructure for agent-driven data QA**. It provides guarded
Snowflake access, session state, evidence enforcement, cached data with freshness tracking,
a team-shareable knowledge library, and a human-in-the-loop web console. All reasoning is
done by agents in the user's harness (Cursor, Claude Code, Codex, …); seekql itself never
calls an LLM.

---

## 1. Core principles

1. **Deterministic core.** seekql holds guardrails, state, storage, and UI. Intelligence
   lives in harness agents steered by thin per-harness skill files that seekql generates.
2. **Harness-agnostic.** Primary interface is a CLI (`seekql …`) returning structured
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

## 2. Confirmed requirements (from spec sessions)

| Decision | Choice |
|---|---|
| Form factor | Python package (uv-managed) + CLI + MCP server + localhost web UI |
| Intelligence | Purely deterministic; zero LLM calls in seekql |
| Snowflake access | Via `snow` CLI named connections; SSO/external-browser auth; seekql never stores credentials |
| Roles | Must work under user's normal role today (parser is the only wall → airtight); read-only role supported/preferred when available |
| Writes to warehouse | None by agents. QA views come from a **view library**; missing views are **proposed** by agents and **executed by the user**, front-loaded at session setup |
| Table definitions | Mixed sources: git-repo SQL files (fix = file diff) and Snowflake-resident logic (fix = standalone DDL snippet) |
| Fix application | User approves in UI → **harness agent** edits work-repo files with its own tools; seekql never writes outside its workspace |
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
│                        seekql core (Python)                       │
│  sessions · guard · executor · cache · workflows · checkpoints    │
│  interventions · findings · proposals · knowledge · views         │
├──────────────────────────────────────────────────────────────────┤
│  workspace files (.seekql/)          │  snow CLI (subprocess)     │
│  state: SQLite (WAL) per session     │  → Snowflake               │
│  artifacts: sqlite/markdown/yaml     │                            │
└──────────────────────────────────────────────────────────────────┘
```

**Components** (all in one package, `src/seekql/`):

- `core/` — session state machine, workspace management, locking
- `guard/` — SQL validation (sqlglot, Snowflake dialect), guard profiles, audit log
- `executor/` — snow CLI subprocess wrapper, auth-state detection, result ingestion
- `cache/` — result storage (SQLite + JSON sidecar), freshness metadata, lookup
- `workflows/` — workflow templates, checkpoint definitions, findings schemas
- `knowledge/` — knowledge library read/propose/confirm, provenance
- `views/` — QA view library registry, coverage check, view proposals
- `interventions/` — structured human-input tasks (replaces CSV round-trips)
- `proposals/` — fix proposals (file diffs and DDL snippets), approval state
- `cli/` — Typer CLI (agent- and human-facing)
- `mcp/` — MCP server (official `mcp` SDK, stdio transport) wrapping the same core
- `ui/` — FastAPI app + server-rendered console, bound to 127.0.0.1
- `harness/` — generators for per-harness skill/instruction files

**Dependencies** (pinned via uv.lock): `sqlglot`, `pydantic`, `typer`, `fastapi`,
`uvicorn`, `mcp`, `jinja2` (UI templates). Result storage and local analysis use
stdlib `sqlite3` — no native DLLs beyond Python itself, so seekql runs under
locked-down Windows Application Control policies common on work machines (duckdb
was evaluated and is blocked by such policies).
Snowflake CLI (`snow`) is an external prerequisite, not a Python dependency.

## 4. Workspace layout

A seekql **workspace** is a directory (typically its own repo, or a folder inside one)
opened in the IDE alongside the user's SQL repos:

```
<workspace>/
├── seekql.toml                 # connection, defaults, scopes, guard profiles, [library] pointer
├── knowledge/                  # LIBRARY ASSET — team-shareable knowledge
│   ├── glossary.md
│   └── <db>/<schema>/<table>.md
├── views/                      # LIBRARY ASSET — QA view library
│   ├── registry.yaml           # view name → purpose, source tables, base files, DDL path, created_at
│   └── ddl/*.sql
├── workflows/                  # LIBRARY ASSET — workflow template overrides/custom types
└── .seekql/                    # sessions & data
    └── sessions/<id>/
        ├── state.db            # SQLite (WAL): state machine, event log, locks
        ├── session.md          # human-readable session brief & status (generated)
        ├── queries/            # every executed statement: sql + result metadata
        ├── data/               # cached results (results.db + sidecars)  [gitignored]
        ├── interventions/      # tasks + structured responses
        ├── findings/           # findings docs (schema-validated)
        └── proposals/          # fix proposals + approval state
```

`.seekql/sessions/*/data/` is gitignored; everything meant to compound over time
(knowledge, views, workflows) is committed and merge-friendly (one file per table/view,
provenance inline).

**Library assets** live in the workspace by default (**solo mode**). In **team mode**,
`seekql.toml` declares a `[library]` pointer to a local clone of a shared library repo,
and seekql resolves `knowledge/`, `views/`, `workflows/`, and shared guard profiles from
there instead — see §11a.

## 5. Session lifecycle (state machine)

```
setup → analysis → synthesis → review → fixes → verification → closed
```

1. **setup** — user (or agent relaying user input) declares: workflow type, target
   tables, guard profile, parallelism (worker count), connection. seekql verifies snow
   auth, snapshots table metadata (columns, row counts, `last_altered`), loads relevant
   knowledge, runs the **view coverage check** (see §9a): existing library views
   relevant to the target tables are presented for the user to pick from, stale ones
   are flagged with a refresh proposal, and gaps become new-view DDL proposals
   assembled from the registry's base-file pointers — all executed by the user now
   (front-loaded so analysis isn't interrupted). Setup checkpoint cannot close until
   coverage is confirmed.
2. **analysis** — the open-ended core. Agents (1..N workers) run guarded queries, cache
   results, log observations, request interventions when human judgment is needed.
   Workflow-defined **required checks** must each be completed with evidence; beyond
   those, agents are free.
3. **synthesis** — findings drafted against the workflow's findings schema; every claim
   must cite query evidence. seekql validates structure + evidence links.
4. **review** — evidence gate: all required checkpoints closed, all findings validated,
   all interventions resolved. Presented to user in UI; user accepts findings.
5. **fixes** — agents write proposals (`file_diff` or `ddl_snippet`, each linked to the
   finding it addresses and payload-validated). User approves/rejects per proposal in
   UI/CLI. Approved file-diffs are applied by the harness agent in the work repo (seekql
   never writes outside its workspace); the agent marks the proposal `applied`; user
   reruns definitions.
6. **verification** — the agent re-runs the anomaly/parity query post-fix and records a
   verification on the proposal citing before and after query ids. seekql computes the
   before/after comparison deterministically (`compare_artifacts`: row-count delta,
   emptiness, value identity for small sets) and enforces that both ids were actually
   executed; the pass/fail verdict rides on that evidence (`verified` /
   `verification_failed`).
7. **closed** — session summary generated; durable learnings promoted to the knowledge
   library (user-confirmed facts marked as such).

Any stage can loop back (verification failure → fixes/analysis). All transitions are
recorded in the event log with actor (user / agent worker id) and timestamp.

**Parallelism.** Sessions are isolated by directory. Within a session, workers register
(`seekql worker join`) and get an id; state mutations go through SQLite (WAL +
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
  scopes whitelisted in `seekql.toml`). Out-of-scope reads produce a *warning* by
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
  `seekql.toml` (committed, so profiles travel with the workspace). seekql ships
  starter profiles (`strict`, `moderate`, `generous`) the user can edit, clone, or
  replace. Selection at session setup is one pick — `--guard-profile <name>` or a
  dropdown in the UI — with per-setting overrides allowed on top
  (`--timeout 300`). **Default selection**: workflow templates suggest a profile, but
  if the session's target tables/views were used in a previous session, seekql defaults
  to the profile used there (last-used wins), shown as "suggested" so the pick is
  one keystroke to accept or change.
- **Audit log**: every statement — accepted or rejected — is recorded with hash,
  worker id, timestamp, guard verdict, and execution stats.
- **Defense in depth**: designed to be the only wall (normal role today) but pairs with
  a read-only Snowflake role when available (`seekql.toml` records which is in play).

## 7. Execution & auth

- Executor invokes `snow sql` as a subprocess (argument lists, never `shell=True`) with
  the configured named connection; results ingested from JSON output.
- **Auth detection**: auth/token errors are recognized and surfaced as a distinct
  `AUTH_REQUIRED` state — in the CLI/MCP response (so agents pause and say so, instead
  of retrying into MFA fatigue) and as a banner in the UI. The user re-auths via
  Snowflake CLI's browser flow; its token caching keeps SSO pop-ups rare even with
  parallel workers.
- seekql never reads, stores, or transmits credentials; that surface belongs entirely
  to Snowflake CLI.

## 8. Result cache & freshness

Every executed query's results are stored automatically:

- **Format**: rows land as table `q_XXXX` in the session's `results.db` (stdlib
  SQLite), plus a JSON sidecar per artifact:
  `{query_hash, sql, executed_at, worker, source_tables, row_count,
  truncated, source_last_altered: {table: ts}}`.
- **Freshness**: `seekql cache find --tables …` (and the MCP equivalent) returns matching
  cached artifacts with a computed staleness verdict — current `last_altered` (one cheap
  metadata query) vs. the value captured at execution time → `fresh` / `stale` /
  `unknown`. Agents are instructed (via skills) to check cache before querying; the
  decision to reuse stays with the agent.
- **Local analysis**: cached artifacts are queryable locally (`seekql cache query`,
  table names = artifact ids), letting agents re-slice already-fetched data without
  warehouse round-trips. Same guard posture: single SELECT only, artifact tables only,
  and the connection is opened read-only (SQLite `mode=ro`) as a second wall.

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
  - rule_out_alternatives   # ≥2 competing explanations tested
open_stages: [analysis]     # unconstrained agent work happens here
findings_schema: bug_hunter_v1   # closed-ended output structure
```

v1 templates: `bug-hunter`, `pipeline-qa`, `table-health`, `semantic-rule-qa`,
`migration-parity`. Each defines setup inputs, required checks, intervention patterns,
and a findings schema. `migration-parity` doubles as the built-in verification stage for
every other workflow.

**Checkpoints** close only via `seekql checkpoint complete <name> --evidence q_017,q_023`
— seekql verifies the cited queries exist, succeeded, and touched relevant objects.
**Findings** are pydantic-validated documents: summary, severity, affected objects,
evidence (query ids), reproduction, proposed remediation, confidence + open questions.

## 9a. QA view library

The view library is how agents get analysis-ready surfaces without ever holding DDL
rights. `views/registry.yaml` records, per view: name, purpose, source tables,
**base files** (paths/globs into the user's work repos where the underlying definition
logic lives), the DDL file in `views/ddl/`, created_at, and the source tables'
`last_altered` at creation time.

**At session setup** the coverage check produces a three-part picture, resolved in one
sitting before analysis begins:

1. **Reuse** — library views matching the session's target tables, presented as a
   pick-list (UI checkboxes / CLI selection). Chosen views enter the session scope.
2. **Refresh** — seekql proactively flags stale views: source-table `last_altered` has
   moved past the view's snapshot, source schema changed (column drift detected from
   the metadata snapshot), or the registry DDL no longer matches what's deployed
   (checked via `SHOW VIEWS` / `GET_DDL`). Each flag comes with regenerated
   `CREATE OR REPLACE` DDL ready for the user to execute.
3. **Create** — for gaps, agents assemble proposed DDL. The registry's base-file
   pointers (plus per-table `definition_files` entries in the knowledge library) tell
   agents exactly which work-repo files to read when deriving new view logic — the
   user can also pass `--base-files <paths>` at setup to point agents at the right
   sources explicitly. Proposals are queued for user execution.

`seekql views list|check|propose|refresh` (and MCP equivalents) expose the same
operations mid-session for the rare case a need surfaces after setup. Executed views
are registered automatically (DDL, sources, base files, timestamps) so the library
compounds.

## 10. Interventions (human-in-the-loop)

Structured tasks replacing the CSV round-trip:

- Agent files an intervention: type (`label_sample`, `confirm_semantics`, `choose`,
  `free_response`), payload (e.g. sample rows + label options), and what it will do with
  the answer.
- UI renders it as an interactive task (tabular labeling with keyboard flow, option
  pickers, text). Responses are stored as structured JSON the agent reads back.
- CLI/MCP: `seekql intervention await` (poll/block) so agents in any harness can wait on
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
  promotion to `user_confirmed` happens via the UI or `seekql knowledge confirm`.
- Agents are instructed to ask (via interventions) when semantics can't be derived from
  data — answers become knowledge, so every session makes the next one faster.
- Search: `seekql knowledge search <term>` (and MCP tool) over facts + glossary.

## 11a. Team library & distribution model

Collaboration needs no server; it rides on git. Three kinds of repo, kept separate:

1. **The seekql tool** (this repo) — installed per user, e.g.
   `uv tool install seekql` or `uvx --from git+https://github.com/Kcarreras/seekql seekql`.
   Never cloned into a workspace; it's software, not data.
2. **A team library repo** — one per team, holding the compounding assets:
   `knowledge/`, `views/`, `workflows/`, and shared guard profiles.
   `seekql library init` scaffolds a fresh one ready to push to the team's git host.
   Another team starting out scaffolds their own — or forks an existing team's library
   to seed from their knowledge.
3. **Personal workspaces** — one per user (sessions, cached data, local config), each
   linked to a local clone of the team library:

   ```toml
   [library]
   path = "~/work/data-qa-library"   # local clone of the team library repo
   ```

**Resolution**: with `[library]` set, seekql reads/writes knowledge, views, workflows,
and shared profiles in the library clone; session state and cached data stay in the
personal workspace. Solo mode (no `[library]`) keeps everything in the workspace, and
`seekql library extract` can later split the assets out into a new library repo when a
team forms.

**Freshness**: at session setup seekql checks the library clone against its remote and
warns if it is behind ("library is 12 commits behind origin — pull before starting?")
or has uncommitted local changes. `seekql library status|pull` wrap the corresponding
git operations; commits/PRs to the library go through normal git tooling — a PR is the
team-scale version of the fact-confirmation flow, giving proposed knowledge a review
step for free.

**Known limits (accepted for v1)**: knowledge propagates at pull cadence, not real
time; there is no cross-user live session visibility or central query audit (each
user's audit log is local; closed-session summaries may be committed to the library if
a team wants shared history); simultaneous view registration by two users reconciles
at merge time. If those ever become must-haves, a central service can be added behind
the same file formats without reworking this architecture.

## 12. Web console (UI)

FastAPI + server-rendered pages (Jinja2; no Node build chain), `127.0.0.1` only, with a
per-launch session token in the URL. v1 views:

1. **Sessions dashboard** — active/recent sessions, stage, checkpoint progress, pending
   items, auth status.
1. **Session setup panel** — guard-profile dropdown (suggested default pre-selected,
   per-setting overrides), view pick-list with refresh flags, pending DDL to execute,
   base-file pointers.
2. **Session detail** — checkpoints w/ evidence, live query log (statement, verdict,
   rows, duration, worker), cached artifacts, event timeline.
3. **Interventions inbox** — pending tasks; interactive labeling/confirmation forms.
4. **Findings review** — rendered findings with evidence drill-down; accept per finding.
5. **Proposals** — diffs/DDL rendered side-by-side with the finding they fix;
   approve/reject; verification results after rerun.
6. **Knowledge** — browse/search; confirm or edit proposed facts.

## 13. Harness integration

- `seekql harness init cursor|claude-code|codex` generates the skill/instruction files
  (e.g. `.cursor/rules/`, `CLAUDE.md` section, or skills) that teach that harness the
  session protocol: check knowledge → setup → cache-before-query → evidence discipline →
  interventions → findings → proposals. The protocol lives in one canonical template so
  all harnesses stay in sync.
- MCP server (`seekql mcp serve`, stdio) for harnesses that prefer typed tools; tool set
  mirrors the CLI 1:1.
- Everything an agent can do via MCP it can do via CLI — harnesses without MCP support
  lose nothing.

## 14. Security posture

- No credential storage or handling (delegated to Snowflake CLI).
- Subprocess calls use argument vectors; no shell interpolation of agent input.
- Guard is default-deny by statement type; audit log is append-only.
- UI binds to loopback only; token-gated; no external assets (works offline).
- Workspace writes are confined to the workspace; seekql never edits files outside it.
- All file reads/writes validate paths against the workspace root (no traversal).
- Dependencies pinned via `uv.lock`; small, well-known set.
- Cached warehouse data is gitignored by generated `.gitignore`; a `seekql session
  scrub` command deletes a session's cached data on demand.

## 15. Build phases

1. **Core + guard + executor + cache** — workspace init, config, session basics, guarded
   `query run`, result caching with freshness, audit log. *(usable value immediately)*
2. **Workflows + checkpoints + findings** — templates, state machine, evidence
   enforcement, findings schemas.
3. **UI console + interventions** — dashboard, query log, intervention forms, findings
   review.
4. **Proposals + verification** — fix proposals, approval flow, parity verification.
5. **Knowledge + view libraries** — libraries, provenance, coverage check at setup.
6. **MCP server + harness generators** — typed tool surface, skill file generation,
   multi-worker registration polish.

Each phase lands tested (pytest; guard gets an adversarial test suite — injection
attempts, sneaky DDL, multi-statements, comment tricks) and lint-clean before the next.

---

*Spec converged 2026-08-20 from requirements interview. Open items intentionally
deferred: read-only role adoption, any central collaboration service (git-based
library model specified in §11a is v1), additional workflow templates.*
