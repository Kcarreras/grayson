# Security posture & review log

grayson runs agent-authored SQL against a production warehouse under the user's own
Snowflake role. The query guard is designed to be the airtight wall even when no
read-only role is available. This document records the threat model and the adversarial
reviews the code has passed.

## Threat model

- **Adversary**: an agent (or prompt-injected content steering one) that submits SQL
  through grayson, trying to write, run DDL, cause side effects, read outside the declared
  scope, exfiltrate data, or exhaust host resources.
- **Trust boundary**: only the user, via chat, issues authoritative instructions. All SQL
  and all tool-observed content is data, never commands.
- **grayson never handles credentials** — authentication is entirely Snowflake CLI's
  responsibility (SSO/external-browser, key-pair, etc.).

## Layered defenses

1. **Statement guard** (`guard/rules.py`) — parses every statement with sqlglot
   (Snowflake dialect); default-deny outside `SELECT`/`SHOW`/`DESCRIBE`/`EXPLAIN`;
   single-statement only; forbidden-node walk blocks DML/DDL nested anywhere; function
   denylist blocks `SYSTEM$*` and `RESULT_SCAN`; UDTF row sources blocked in strict mode;
   unqualified names blocked in strict mode; per-statement cost caps (auto-LIMIT,
   timeout, budget).
2. **Snowflake role** (defense in depth) — a read-only role is preferred when available;
   the guard is built to hold under the user's normal role regardless.
3. **Local analysis** (`cache/local.py`) — cached-result queries run on a read-only
   SQLite connection (`mode=ro`), single SELECT only, artifact tables only, a function
   denylist, `trusted_schema=OFF`, and a wall-clock interrupt watchdog.
4. **Append-only audit** — every statement (accepted or rejected) is recorded with a
   guard verdict; executor exceptions and cache failures still record a terminal status,
   never a stranded `pending` row.

## Review log

### 2026-08-21 — Phase 1 adversarial review (18-agent workflow, find → verify)

A multi-agent review red-teamed the guard, executor, cache, and session state; each
finding was independently verified against the source. Ten issues were confirmed and all
were fixed in the same phase (regression tests in `tests/test_guard_hardening.py` and
`tests/test_hardening_misc.py`):

| # | Severity | Issue | Fix |
|---|---|---|---|
| 1 | high | `SYSTEM$` side-effect functions callable inside SELECT | Function denylist by `SYSTEM$` prefix |
| 2 | high | Unqualified table name bypassed strict-scope | Strict mode blocks unverifiable names |
| 3 | medium | UDTF/`TABLE(fn())` row source bypassed scope | Blocked in strict mode, warned otherwise |
| 4 | medium | `RESULT_SCAN` read arbitrary prior results | Added to function denylist (both forms) |
| 5 | medium | Local-analysis query had no compute/time cap | Interrupt watchdog + function denylist |
| 6 | medium | Executor/cache raise stranded a `pending` audit row | Wrapped; terminal status always recorded |
| 7 | low | Identifier regex `$` accepted a trailing newline | Anchored with `\Z` |
| 8 | low | Auth-error markers too broad (`sso`) | Tightened to specific phrases/codes |
| 9 | low | `load_extension` not blocked in guard (only SQLite default) | Explicit denylist + `trusted_schema=OFF` |
| 10 | low | Budget cap racy under concurrent workers (TOCTOU) | In-flight `pending` rows count toward budget |

Two candidate findings were verified as **false positives** (the `GRAYSON_SNOW_CMD` env
override is a documented test hook reading only from the environment; `ensure_within`
path containment is sound) and one as a non-issue (guard depends on sqlglot/SQLite
parsing, but the read-only connection and artifact allowlist make divergence
non-exploitable).

### 2026-08-21 — Phase 2-6 adversarial review (26-agent workflow, find → verify)

A second multi-agent review targeted the state-mutating surfaces added after Phase 1:
the evidence engine, proposals/verification, interventions, the web console, and the MCP
server. Because grayson's whole value is keeping a *semi-trusted agent* honest, integrity
gaps in the gates are treated as first-class security issues. 17 findings were confirmed
and fixed (regressions in `tests/test_integrity_hardening.py` and additions to
`tests/test_cache.py`, `tests/test_engine.py`, `tests/test_ui.py`):

| # | Severity | Issue | Fix |
|---|---|---|---|
| 1 | critical | Stage gates skippable by jumping straight to `verification`/`closed` | Gates are cumulative by target index, not keyed to one target stage |
| 2 | high | Agent could self-authorize a gate bypass via `force` | `force` honored only for the `user` actor; removed from the MCP tool |
| 3 | high | Evidence checked for existence but not relevance (`SELECT 1` passed) | Evidence must touch a table in the session scope |
| 4 | high | `verify()` stamped un-approved/rejected proposals `verified` | Verification requires an approved/applied proposal |
| 5 | high | file_diff: reviewer saw `diff` but `new_content` was applied | Console renders `new_content` explicitly as what will be written |
| 6 | high | Agent could forge `user_confirmed` knowledge provenance | `add_fact` refuses `user_confirmed`; only `confirm_fact` (a user action) sets it |
| 7 | medium | fixes gate accepted any recorded finding | Requires a user-accepted finding |
| 8 | medium | Verification accepted `before_qid == after_qid` | Rejected; the two must differ |
| 9 | medium | `compare_artifacts` ignored the truncation flag | Truncated sets can't be certified identical; flag surfaced |
| 10 | medium | Comparison trusted tamperable sidecar `row_count` | Counts read from the actual cached table |
| 11 | medium | Access token rode in every URL | Token also accepted via httponly `SameSite=Strict` cookie, set on first load |
| 12 | medium | Path traversal via unvalidated view name | View name validated as a SQL identifier |
| 13 | medium | `extract_library` followed symlinks (secret exfiltration) | Symlinks skipped, `follow_symlinks=False` |
| 14 | low | Token compared non-constant-time | `secrets.compare_digest` |
| 15 | low | No Host/Origin check (DNS-rebinding) | Host-header allowlist (loopback names only) |
| 16 | low | `_FQN_PART` accepted a trailing newline | Anchored with `\Z` |
| 17 | low | MCP exposed `force` (dup of #2) | Removed from the MCP surface |

### 2026-08-27 — Core workflow review: incomplete gate fixes and a gate with no honest exit

Reviewing the core workflow templates surfaced three integrity gaps, two of them
residue from the fixes above (regressions in `tests/test_cli.py` and
`tests/test_engine.py`):

| # | Severity | Issue | Fix |
|---|---|---|---|
| 18 | high | #2 was only half-fixed: `force` was honored for the `user` actor, but the CLI *defaulted* `--actor` to `user` and let any caller pass it, so a plain `session advance --force` from a shell-out cleared every gate | `--force` now also requires an interactive terminal and an explicit confirmation. Friction, not containment — an agent shelling out non-interactively cannot claim the user's authority, and harness deny rules can pattern-match the flag as a second layer |
| 19 | medium | `session close` bypassed the engine entirely — it called `set_stage` directly, skipping both the checks-complete and accepted-finding gates, and logged the change under the default actor `user`, misattributing an agent's bypass to the human in the audit trail | Closing routes through `engine.close_session` and is a user action (interactive terminal). CLI actor now derives from whether a human is at the prompt, so agent-driven changes are recorded as the agent's. An explicit `--actor user` from a non-interactive caller is refused for the same reason — the flag can narrow claimed authority, never widen it |
| 20 | medium | A required checkpoint that did not apply to the target had no honest exit — the only route past the gate was a query chosen to satisfy the relevance test | Checkpoints can be **waived** by a user with a mandatory reason. Agents request via intervention; a waived gate is reported separately from a closed one everywhere |
| 21 | high | The terminal gate covered `close`, `waive` and `--force` but not the other human boundaries: `finding accept`, `proposal approve`/`reject`, `intervention respond` and `knowledge confirm` ran fine from a shell-out — an agent could accept its own finding, approve its own fix, answer its own intervention | Every command documented as a user action now requires an interactive terminal. `intervention respond` loses its non-interactive stdin path |

A fourth issue was integrity-adjacent rather than a bypass: a session that found
nothing could not close, because every stage from `fixes` on required an accepted
finding. The pressure that creates — invent a finding or abandon the session —
pointed the wrong way, so a clean run now closes as a first-class `clean` outcome
that a human confirms. A *forced* close never earns that label.

### 2026-08-27 — Guard rules covered the binary, not the credentials

`grayson harness guard apply` denied `Bash(snow:*)` and reads of `.grayson/**`,
but nothing covered `~/.snowflake/` — where the `snow` CLI keeps connection
details and, under key-pair auth, the private key beside them. An agent with
shell did not need to evade the `snow` pattern at all: read the connection
file, connect with the Python connector or the SQL REST API, and no matched
command ever appears. The deny set now includes `Read(~/.snowflake/**)` and
`Read(~/.snowsql/**)`, and the Cursor and manual guidance name the same path.

Workspaces guarded before this ship will show the new rules as missing in
`grayson harness guard status`; re-running `apply` adds them.

A private key stored somewhere other than `~/.snowflake` is still uncovered and
cannot be covered — its location is the user's to choose. This is the ordinary
shape of the layer: it stops the direct path, not a determined one.

## Residual risks (accepted)

- A pre-existing malicious UDF with an external-access integration could exfiltrate
  *in-scope* data when invoked from a SELECT. Creating such a UDF requires privileged
  account setup that the guard already blocks; scope-limiting reduces blast radius. Use a
  read-only role for the strongest guarantee.
- The interactive-terminal check on user-only actions (`--force`, `session close`,
  `checkpoint waive`, `finding accept`, `proposal approve`/`reject`,
  `intervention respond`, `knowledge confirm`) is friction, not containment: an agent that can allocate a pty
  can defeat it. It removes the accidental and the casual path, keeps the audit trail
  honest about who acted, and gives harness guard rules a stable pattern to deny. The
  console remains the surface where a human unambiguously is the human.
- The guard's correctness depends on sqlglot parsing SQL the way Snowflake executes it.
  sqlglot is version-pinned; dialect updates are reviewed before bumping.

## Bypass and containment (where the guard's authority ends)

The guard is airtight for statements that pass **through** grayson. It is not a
sandbox around the agent: the agent runs under the user's OS account, and the
Snowflake CLI's named connection (credentials, cached tokens, key files) is
reachable by any process running as that user. An agent with unrestricted shell
access could call `snow` directly and skip the guard, the audit trail, and the
workflow entirely. grayson's protocol files tell agents not to — and prompting
is exactly what this project does not accept as a guarantee.

Containment therefore comes from layers *around* grayson, each honest about
what it provides:

| Layer | Provides | Survives full bypass? |
|---|---|---|
| Read-only Snowflake role on the agent's connection | Warehouse-enforced write prevention | **Yes** — the only layer that does |
| Harness guard permissions (`grayson harness init --guard-permissions`, `grayson harness guard`) | Deny rules in the harness config: direct `snow` use and `.grayson/` state access hit a human-visible permission prompt | No — friction and visibility, harness-dependent |
| Credential isolation (`grayson mcp serve --http`) | The MCP server runs where the credentials live (service account, container); the agent's machine holds only a URL and bearer token | Yes for credentials — there is nothing on the agent's machine to steal |
| Evidence gates | Work done outside grayson yields nothing citable: checkpoints, findings, and verifications close only on queries that executed through grayson | Removes the incentive, not the ability |
| Audit reconciliation (`grayson audit reconcile`) | Diffs warehouse `QUERY_HISTORY` against grayson's audit trail; unmatched statements are a bypass review list, optionally recorded as an external check (verdict only) | Detection after the fact, not prevention |

Two deliberate design choices in support of this:

- **History is one-directional.** `QUERY_HISTORY`/`LOGIN_HISTORY` table
  functions and the `SNOWFLAKE.ACCOUNT_USAGE` history views are on the guard's
  denylist: warehouse history is how the *human* audits the agent, never data
  the agent reads (past statements can carry sensitive literals). The
  reconciliation command has no MCP twin for the same reason; agents see only
  the ingested pass/warn verdict.
- **Harness config writes are consent-based — all of them.** Deny rules and
  MCP server entries alike are offered during `harness init` and managed by
  `harness guard status|apply|remove` and `harness mcp status|apply|remove` —
  shown before written, reversible after, never applied silently.

Every supported harness has a real mechanism for blocking the bypass paths;
they differ in who writes the config and how hard the wall is:

| Harness | Mechanism | How it's set up |
|---|---|---|
| Claude Code | Deny rules in `.claude/settings.json`: `Bash(snow:*)` and `.grayson/**` file access hit a permission prompt | grayson writes them on consent (`harness guard apply`) |
| Cursor (IDE agent) | **Hooks** — `beforeShellExecution`/`beforeReadFile` in `.cursor/hooks.json` **hard-deny** `snow` and `.grayson/` access (stronger than a prompt; needs a recent Cursor and an executable hook script, so POSIX; fails open on malformed events). Alternative: the agent **command denylist** in Cursor's app settings (direct `snow` never auto-runs — a human sees the prompt) | Choice at `harness init cursor`: grayson writes the hook + script on consent (`harness guard apply --harness cursor`), or declining prints the copy/paste denylist steps |
| Cursor CLI (`cursor-agent`) | The IDE denylist/hooks do **not** apply; the CLI has its own permission config — set its allow/deny rules to block `snow`, and prefer MCP as the interface (the CLI shares the project's rules and MCP config) | Human-configured, separately from the IDE |
| Codex | The **OS-level sandbox**: default `workspace-write` mode blocks network egress from shell commands, so direct `snow` cannot reach the warehouse at all. Register grayson as an MCP server in `~/.codex/config.toml` — MCP servers run outside the sandbox, so the guarded path works while the bypass path doesn't (this makes MCP, not the CLI, the warehouse path under Codex) | Human-configured; `harness init codex` and `harness guard status --harness codex` print the steps |
| GitHub Copilot (VS Code agent mode) | Deny entries in `chat.tools.terminal.autoApprove` (`.vscode/settings.json`): `snow` and commands touching `.grayson/` are never auto-approved — a human sees the prompt. **Terminal only**: Copilot's file tools are not governed by this setting, so `.grayson/` reads via the editor rely on the protocol + audit reconciliation | grayson writes them on consent (`harness guard apply --harness copilot`) |
| GitHub Copilot coding agent (cloud) | Runs in an ephemeral GitHub Actions environment behind a default-deny egress **firewall**: direct `snow` cannot reach the warehouse unless a human allowlists it. No local console for interventions — pair it with the served, knowledge-only deployment ([DEPLOYMENT.md](DEPLOYMENT.md)); its MCP config lives in the repo's Copilot settings on github.com, not a repo file | Human-configured on github.com |

The recommended baseline for production use: a dedicated read-only role for
agent connections, the harness guard configured per the table above, and
periodic `grayson audit reconcile --ingest`.
