"""Harness skill/instruction generators.

The session protocol lives in one canonical template so every harness stays in
sync. `generate_harness` writes the harness-specific file (Cursor rule, CLAUDE.md
section, or Codex AGENTS.md section) that teaches an agent how to drive seekql.
"""

from __future__ import annotations

from pathlib import Path

PROTOCOL = """\
# seekql — agentic SQL QA protocol

seekql is deterministic infrastructure for QA and investigation over SQL tables
(Snowflake). You supply the analysis; seekql enforces guardrails, tracks evidence,
and stores results. Drive it via the `seekql` CLI (JSON output) or its MCP tools —
they are equivalent. Never query Snowflake except through seekql.

## Golden rules
- Only read statements run: SELECT / SHOW / DESCRIBE / EXPLAIN. DML/DDL are blocked.
  You never get warehouse write rights — to change a table definition, write a *fix
  proposal* for the user to apply.
- Every checkpoint and finding must cite **evidence**: the ids of queries you actually
  executed (`q_0001`, ...). seekql rejects claims without real evidence.
- Check cached data and the knowledge library before re-querying.
- When a judgement needs a human (semantic correctness, ambiguous rules), file an
  intervention and wait for the answer instead of guessing.

## Workflow
1. Discover: `seekql workflow list`; read the knowledge library for the target tables
   (`seekql knowledge show DB.SCHEMA.TABLE`). Ask the user for anything the data can't
   tell you, and save durable answers with `seekql knowledge add`. If a target has no
   recorded knowledge at all, settle grain/semantics with the user early — or run the
   `table-onboarding` workflow first to build the semantic record.
2. Start: `seekql session start --workflow <name> --table DB.SCHEMA.TABLE ...`. Review
   the returned view coverage — reuse existing QA views, ask the user to create/refresh
   any the setup flags. Note the session id; use it in every later command.
3. Analyze: run guarded queries (`seekql query run <sid> --sql "..."`). Before querying,
   check the cache (`seekql cache find <sid> --table ... --check-freshness`). Complete
   each required checkpoint with evidence:
   `seekql checkpoint complete <sid> <key> --evidence q_0003,q_0007 --note "..."`.
4. Human input when needed: `seekql intervention request <sid> --kind label_sample ...`,
   then `seekql intervention await <sid> <iid> --timeout 600`. The user answers in the
   web console (`seekql ui serve`). When an answer settles a durable fact about a table
   (its grain, a semantic rule, an expectation), persist it for future sessions:
   `seekql knowledge add <table> --fact "..." --evidence <iid>` — the user confirms it
   from the console later.
5. Findings: record them against the workflow schema, each citing evidence:
   `seekql finding add <sid> --json '{...}'`.
6. Fixes: draft proposals linked to findings
   (`seekql proposal add <sid> --kind file_diff|ddl_snippet ...`). After the user
   approves, apply file diffs yourself with your editing tools, mark them applied
   (`seekql proposal applied <sid> <pid>`), and ask the user to rerun the definitions.
7. Verify: re-run the anomaly/parity query and record before/after evidence:
   `seekql proposal verify <sid> <pid> --before q_0003 --after q_0050 --verdict pass`.
8. Advance stages as you go (`seekql session advance <sid> --to review`); gates enforce
   that evidence exists before review/fixes.

Run `seekql --help` (and `seekql <group> --help`) for the full command surface.
"""

MCP_NOTE = """\

## MCP
seekql also ships an MCP server (`seekql mcp serve`, stdio) whose tools mirror these
commands one-to-one. If it is configured, prefer the typed tools; the protocol is
identical.
"""

HARNESSES = {"cursor", "claude-code", "codex"}


def generate_harness(root: Path, harness: str, with_mcp: bool = True) -> dict:
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness '{harness}' (known: {', '.join(sorted(HARNESSES))})")
    body = PROTOCOL + (MCP_NOTE if with_mcp else "")
    written: list[str] = []

    if harness == "cursor":
        target = root / ".cursor" / "rules" / "seekql.mdc"
        target.parent.mkdir(parents=True, exist_ok=True)
        front = (
            "---\ndescription: seekql agentic SQL QA protocol\nglobs:\nalwaysApply: false\n---\n\n"
        )
        target.write_text(front + body, encoding="utf-8")
        written.append(str(target.relative_to(root)))
    elif harness == "claude-code":
        target = root / "CLAUDE.md"
        written.append(_append_section(target, body))
    else:  # codex
        target = root / "AGENTS.md"
        written.append(_append_section(target, body))

    # a standalone copy for reference regardless of harness
    ref = root / ".seekql" / "PROTOCOL.md"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(body, encoding="utf-8")
    written.append(str(ref.relative_to(root)))
    return {"harness": harness, "written": written}


_MARK_START = "<!-- seekql:start -->"
_MARK_END = "<!-- seekql:end -->"


def _append_section(target: Path, body: str) -> str:
    section = f"{_MARK_START}\n{body}\n{_MARK_END}\n"
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    if _MARK_START in existing and _MARK_END in existing:
        pre = existing.split(_MARK_START)[0]
        post = existing.split(_MARK_END, 1)[1]
        target.write_text(pre + section + post, encoding="utf-8")
    else:
        joined = (existing.rstrip() + "\n\n" if existing.strip() else "") + section
        target.write_text(joined, encoding="utf-8")
    return str(target.name)
