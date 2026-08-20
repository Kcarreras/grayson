# seekql

Agentic, open-ended QA and investigation over SQL tables and data (Snowflake-first).

Deterministic infrastructure for agent-driven data QA: guarded warehouse access, session
state with evidence enforcement, cached results with freshness tracking, a team-shareable
knowledge library, and a human-in-the-loop web console. Agent harnesses (Cursor, Claude
Code, Codex, …) supply the intelligence; seekql supplies the rails. See
[docs/SPEC.md](docs/SPEC.md) for the full specification.

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages Python and all dependencies)
- Python 3.12+ (uv will install one automatically if missing)

## Setup

```bash
git clone https://github.com/Kcarreras/seekql.git
cd seekql
uv sync
```

## Usage

```bash
uv run seekql
```

## Development

```bash
uv run pytest        # run tests
uv run ruff check .  # lint
uv run ruff format . # format
```

## Project layout

```
seekql/
├── src/seekql/     # package source
├── tests/          # pytest suite
└── pyproject.toml  # project metadata, deps, tool config
```
