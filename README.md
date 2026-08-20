# seekql

Agentic, open-ended QA and investigation over SQL tables and data.

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
