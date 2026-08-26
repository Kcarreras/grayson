"""Harness skill/instruction generators.

The session protocol lives in one canonical template so every harness stays in
sync. `generate_harness` writes the harness-specific file (Cursor rule, CLAUDE.md
section, or Codex AGENTS.md section) that teaches an agent how to drive grayson.
"""

from __future__ import annotations

from pathlib import Path

PROTOCOL = """\
# grayson — agentic SQL QA protocol

grayson is deterministic infrastructure for QA and investigation over SQL tables
(Snowflake). You supply the analysis; grayson enforces guardrails, tracks evidence,
and stores results. Drive it via the `grayson` CLI (JSON output) or its MCP tools —
they are equivalent. Never query Snowflake except through grayson.

## Golden rules
- Only read statements run: SELECT / SHOW / DESCRIBE / EXPLAIN. DML/DDL are blocked.
  You never get warehouse write rights — to change a table definition, write a *fix
  proposal* for the user to apply.
- Access warehouse data ONLY through grayson (`query run`, `cache query`). Never open
  warehouse or `.grayson/` database/state files directly — including local or sandbox
  files. Direct reads bypass the audit trail, so nothing learned from them counts as
  evidence, and checkpoints/findings citing no executed queries will be rejected.
- Every checkpoint and finding must cite **evidence**: the ids of queries you actually
  executed (`q_0001`, ...). grayson rejects claims without real evidence.
- Check cached data and the knowledge library before re-querying. An empty cache,
  knowledge library, or view registry is normal in a fresh workspace — it is not a
  problem to fix or report; build them as you work.
- Setup and admin commands (`init`, `sandbox *`, `library *`, `harness *`, `ui serve`)
  belong to the user. If infrastructure looks missing or broken — no workspace, a
  missing warehouse, expired auth — pause and ask the user; never scaffold, reseed,
  or reconfigure it yourself.
- When a judgement needs a human (semantic correctness, ambiguous rules), file an
  intervention and wait for the answer instead of guessing.

## Workflow
1. Discover: `grayson workflow list`; read the knowledge library for the target tables
   (`grayson knowledge show DB.SCHEMA.TABLE` — its `completeness` report shows what is
   still undescribed). Ask the user for anything the data can't tell you. Save durable
   one-off answers with `grayson knowledge add`, and record the structured base
   descriptor (grain, column definitions, relationships, freshness) with
   `grayson knowledge set`. If a target has no recorded knowledge at all, settle
   grain/semantics with the user early — or run the `table-onboarding` workflow first.
   Before diagnosing from scratch, check `grayson records search <term>`: a similar
   problem may already have a diagnosed cause and a verified fix on record. Also
   check external deterministic checks (`grayson checks status --table ...`, echoed
   at session start): a failing Airflow/dbt check on a target table is a pre-vetted
   lead — replicate it with a guarded query first, then widen the investigation.
2. Start: `grayson session start --workflow <name> --table DB.SCHEMA.TABLE ...`.
   Record the user's answers to the workflow's setup inputs on the session with
   `--input key="answer"` (repeatable; MCP: the `inputs` dict) — the session then
   documents why it was started, not just the chat transcript. Review
   the returned view coverage — library views matching the targets are already in your
   query scope (`views_in_scope`), so query them directly; ask the user to create or
   refresh any the setup flags. Need another registered view later?
   `grayson views use <sid> <name>` brings it into scope. Note the session id; use it
   in every later command.
3. Analyze: run guarded queries (`grayson query run <sid> --sql "..." --label "why"`).
   Always pass `--label`: a short purpose note ("replicate: dup order ids") that shows
   up in the console's query log and the audit trail. Before querying,
   check the cache (`grayson cache find <sid> --table ... --check-freshness`). Complete
   each required checkpoint with evidence:
   `grayson checkpoint complete <sid> <key> --evidence q_0003,q_0007 --note "..."`.
   Make your reasoning visible as you go: whenever a cached result shows a trend,
   distribution, or comparison worth seeing, build a chart from it
   (`grayson chart add <sid> --artifact q_0007 --kind line -x day -y null_rate
   --title "..." --note "what this shows"`). Charts appear live in the user's
   console and are traceable to the executed query — narrate the investigation
   visually, especially for root-cause work. The response's `text` field is a
   terminal rendering of the same chart: paste it into your chat reply, inside a
   code block, so the user sees the shape right in the conversation.
4. Human input when needed: `grayson intervention request <sid> --kind label_sample ...`,
   then `grayson intervention await <sid> <iid> --timeout 600`. The user answers in the
   web console (`grayson ui serve`). When an answer settles a durable fact about a table
   (its grain, a semantic rule, an expectation), persist it for future sessions:
   `grayson knowledge add <table> --fact "..." --evidence <iid>` — the user confirms it
   from the console later.
5. Findings: record them against the workflow schema, each citing evidence:
   `grayson finding add <sid> --json '{...}'`. Findings are immutable — never try
   to edit one. If an accepted finding turns out to be wrong, record a corrected
   finding with `"supersedes": "f_00X"` in the payload: that is a proposal, and
   the old finding is replaced only if the user accepts the new one. If the user
   REJECTS a finding, its rejection reason appears in `grayson finding list` and
   `grayson session readiness` (findings_rejected) — read it, continue analysis
   in that direction, and record a corrected finding.
6. Fixes: draft proposals linked to findings
   (`grayson proposal add <sid> --kind file_diff|ddl_snippet ...`). After the user
   approves, apply file diffs yourself with your editing tools, mark them applied
   (`grayson proposal applied <sid> <pid>`), and ask the user to rerun the definitions.
   When a ddl_snippet CREATES A VIEW, include `view_name`, `source_tables`, and
   `purpose` in its payload: once the user has run the DDL and you mark the proposal
   applied, grayson registers the view in the library and adds it to your scope
   automatically — no separate registration step.
7. Verify: re-run the anomaly/parity query and record before/after evidence:
   `grayson proposal verify <sid> <pid> --before q_0003 --after q_0050 --verdict pass`.
8. Advance stages as you go (`grayson session advance <sid> --to review`) so the
   console's stage strip tracks your progress. Your first executed query moves
   setup to analysis automatically; every later transition is yours to declare,
   and gates enforce that evidence exists before review/fixes.

Run `grayson --help` (and `grayson <group> --help`) for the full command surface.
"""

MCP_NOTE = """\

## MCP
grayson also ships an MCP server (`grayson mcp serve`, stdio) whose tools mirror these
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
        target = root / ".cursor" / "rules" / "grayson.mdc"
        target.parent.mkdir(parents=True, exist_ok=True)
        front = (
            "---\ndescription: grayson agentic SQL QA protocol\nglobs:\nalwaysApply: false\n---\n\n"
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
    ref = root / ".grayson" / "PROTOCOL.md"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text(body, encoding="utf-8")
    written.append(str(ref.relative_to(root)))
    return {"harness": harness, "written": written}


_MARK_START = "<!-- grayson:start -->"
_MARK_END = "<!-- grayson:end -->"


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
