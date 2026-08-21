# seekql

Agentic, open-ended QA and investigation over SQL tables and data (Snowflake-first).

Deterministic infrastructure for agent-driven data QA: guarded warehouse access, session
state with evidence enforcement, cached results with freshness tracking, a team-shareable
knowledge library, and a human-in-the-loop web console. Agent harnesses (Cursor, Claude
Code, Codex, …) supply the intelligence; seekql supplies the rails. See
[docs/SPEC.md](docs/SPEC.md) for the full specification.

## How it works

seekql never calls an LLM. Your agent (in Cursor, Claude Code, Codex, …) does the
analysis and drives seekql through a CLI or an equivalent MCP server. seekql enforces the
rails:

- **Guarded execution** — every statement is parsed and default-denied to read-only
  (`SELECT`/`SHOW`/`DESCRIBE`/`EXPLAIN`); side-effecting functions and scope escapes are
  blocked. Agents never get warehouse write rights; they propose fixes for you to apply.
- **Evidence enforcement** — checkpoints and findings can't close without citing queries
  that actually executed. This is the deterministic "QA of the QA".
- **Structured workflows** — Bug Hunter, Pipeline QA, Table Health, Semantic Rule QA, and
  Migration/Parity, each with required checks and a findings schema.
- **Result cache with freshness** — results are stored locally with source `LAST_ALTERED`
  captured, so agents know when to re-query.
- **Human-in-the-loop console** — a localhost web UI for labeling samples, answering
  agent questions, reviewing findings, and approving fixes.
- **Team libraries** — knowledge (table semantics with provenance) and QA views live in a
  git-shareable library so each session compounds on the last.

See [docs/SPEC.md](docs/SPEC.md) for the full design and [docs/SECURITY.md](docs/SECURITY.md)
for the guard's threat model and review history.

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages Python and all dependencies)
- Python 3.12+ (uv installs one automatically if missing)
- [Snowflake CLI](https://docs.snowflake.com/en/developer-guide/snowflake-cli) (`snow`)
  with a configured named connection — seekql delegates all auth to it and never handles
  credentials.

## Install & set up

```bash
uv tool install git+https://github.com/Kcarreras/seekql   # or: git clone && uv sync
cd your-data-repo
seekql init .                    # scaffold a workspace (seekql.toml, .seekql/, libraries)
seekql doctor                    # verify snow CLI + connection
seekql harness init cursor       # teach your agent the protocol (or claude-code | codex)
```

## Try it without Snowflake (sandbox)

No warehouse handy? `seekql sandbox init` scaffolds a demo workspace backed by a
local mock warehouse (SQLite behind the same guarded executor path) seeded with
mock retail data containing planted, workflow-matched problems — a join fan-out
bug for `bug-hunter`, an email-NULL regression plus duplicate keys for
`table-health`, and dropped/drifted rows for `migration-parity`:

```bash
seekql sandbox init my-demo && cd my-demo
seekql doctor                        # checks the sandbox warehouse, not snow
seekql harness init claude-code      # teach your agent the protocol
```

`SANDBOX_ANSWER_KEY.md` holds the exact ground truth (counts, root causes, and a
scoring rubric) — keep it away from the agent and use it to grade the findings it
produces. `seekql sandbox reset` re-seeds the data.

## Typical session (driven by your agent)

```bash
seekql session start --workflow bug-hunter --table ANALYTICS.WEB.PAGE_EVENTS
seekql query run <sid> --sql "SELECT ... "          # guarded; results cached as q_0001…
seekql checkpoint complete <sid> replicate_anomaly --evidence q_0003
seekql finding add <sid> --json '{...}'             # schema + evidence validated
seekql ui serve                                     # localhost console for interventions & approvals
seekql session report <sid> --out report.md         # shareable session report (also plain JSON)
seekql cache export <sid> q_0003 --out rows.csv     # export a cached result set (csv/json)
seekql query rerun <sid> q_0003                     # re-run a prior query for a freshness re-check
```

The MCP server (`seekql mcp serve`, stdio) exposes the same operations as typed tools.

## Development

```bash
uv run pytest        # run the test suite
uv run ruff check .  # lint
uv run ruff format . # format
```

## Project layout

```
seekql/
├── src/seekql/
│   ├── guard/        # SQL statement guard (the airtight wall)
│   ├── executor/     # snow CLI execution + auth detection
│   ├── cache/        # result storage, freshness, guarded local analysis
│   ├── core/         # session state machine, evidence engine, proposals
│   ├── workflows/    # YAML workflow templates + registry
│   ├── findings/     # findings schemas
│   ├── interventions/# human-in-the-loop task types
│   ├── knowledge/    # team knowledge library
│   ├── views/        # QA view library + coverage checks
│   ├── ui/           # FastAPI + Jinja2 local console
│   ├── mcp/          # MCP server
│   └── harness/      # per-harness skill-file generators
├── tests/            # pytest suite
└── docs/             # SPEC.md, SECURITY.md
```
