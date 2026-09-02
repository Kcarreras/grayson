"""Harness skill/instruction generators.

The session protocol lives in one canonical template so every harness stays in
sync. `generate_harness` writes the harness-specific file (Cursor rule, CLAUDE.md
section, Codex AGENTS.md section, or Copilot .github/copilot-instructions.md
section) that teaches an agent how to drive grayson.
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
- **Finding nothing is a result.** If the checks clear and nothing is worth acting on,
  do not manufacture a finding to get past a gate: ask the user to close the session as
  a *clean* result (`session readiness` tells you when that is the available route).
  Likewise, if a required checkpoint genuinely does not apply to this target, do not
  close it with a query picked to satisfy the evidence test — file an intervention
  asking the user to **waive** it, and say why. Closing sessions and waiving checks are
  user actions; you ask, they decide.

## Workflow
1. Discover: `grayson workflow list` (and `grayson workflow preview <name>` — the
   standard human-readable rendering of a template; paste its `text` to the user
   whenever they should choose between workflows or sign off on one). Read the
   knowledge library for the target tables
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
2b. Profile first: `grayson profile table <sid> DB.SCHEMA.TABLE` returns a table's
   whole descriptive battery — per-column nulls, cardinality, ranges, key candidates,
   value frequencies — in three or four guarded queries whose ids are evidence. Do
   NOT hand-roll forty single-column queries; it burns the budget and produces ids
   that differ every run. Its `observations` are leads, not verdicts. For quantiles
   and correlations use `grayson profile stats|correlate <sid> <sample-qid>`: those
   compute locally over the cached sample, so cite the sample's qid AND say the
   statistic was computed locally rather than verified by the warehouse.
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
4b. Breadth: `workflow show <name>` lists **suggested checks** alongside the required
   ones. They gate nothing — they are the fundamentals the workflow expects you to
   consider. Do the ones that apply to these tables and close them like any other
   checkpoint (`checkpoint complete` accepts them by key); skip the rest and say
   which in your findings. Required checks may declare `depends_on`: close them in
   that order, it is part of the method.
5. Findings: record them against the workflow schema, each citing evidence:
   `grayson finding add <sid> --json '{...}'`. Calibrate severity against
   `grayson finding rubric` — critical means wrong data is already being used for
   decisions, info means it is not a defect at all. Two rungs cost specificity and
   are enforced: `confidence: high` needs a `reproduction` (if nobody else can go
   and see it, it is not high confidence), and `severity: critical|high` needs
   `affected_objects`. Downgrading to dodge those is worse than either — say what
   you found and let the user judge. Findings are immutable — never try
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
8b. Narrate: before the user closes, write the report narrative —
   `grayson session narrate <sid> --text "..."` — your story of the investigation,
   citing executed query ids. It becomes the clearly-labeled agent-written section
   of the published report; the deterministic sections render from the record and
   are not yours to shape.
9. Close: the user closes the session, from the console or their own terminal —
   either on accepted findings, or as a **clean** result when the checks cleared and
   nothing turned up. `grayson session readiness <sid>` reports which route applies
   (`clean_close_available`, `next_action`); tell the user what you found, or that you
   found nothing, and let them close it. A session that is broken, was started by
   mistake, or stopped mattering is the user's to **abandon** or **delete** — say so
   and ask; never abandon, delete, or remove a session's published records yourself.

Run `grayson --help` (and `grayson <group> --help`) for the full command surface.
"""

MCP_NOTE = """\

## MCP
grayson also ships an MCP server (`grayson mcp serve`, stdio) whose tools mirror these
commands one-to-one. If it is configured, prefer the typed tools; the protocol is
identical.
"""

#: the interactive workflow-authoring skill, one canonical text. Written in the
#: SKILL.md open format to each harness's native skills directory (Claude Code,
#: Cursor >=2.1, and VS Code Copilot all read the same format); Codex, which has
#: no skills mechanism, gets it as an AGENTS.md section. Versioned with grayson
#: so the interview can never drift from what `workflow lint` enforces.
WORKFLOW_AUTHOR_DESCRIPTION = (
    "Design a grayson workflow template interactively with the user: interview, "
    "draft the YAML, lint, preview for sign-off, then store it in the team library. "
    "Use when the user wants a new investigation workflow or to adapt an existing one."
)

WORKFLOW_AUTHOR = """\
# Authoring a grayson workflow with the user

A workflow template defines the shape of an investigation: the setup inputs a
human provides, the evidence-gated checkpoints a session must clear, and the
findings schema every claim validates against. You draft it; grayson validates
and stores it; the user signs off at each step. Never hand-write files into the
library's workflows/ directory — every step below goes through the CLI (or its
MCP mirror), which enforces validation and ownership server-side.

## 1. Interview — one topic at a time, in this order

- **Purpose.** What decision does this workflow serve, and who reads its
  findings? Write the answer into `description` — agents pick workflows by it.
- **Fork or fresh.** `grayson workflow list`, then `grayson workflow preview
  <name>` for the closest existing template. If one is 80% right, fork it
  (lineage is recorded); only start blank when nothing fits.
- **Setup inputs.** What must a human tell the agent before work starts —
  expectations, locators, thresholds? Each becomes a `setup_inputs` entry, and
  each should be read by some check via `uses_inputs` (lint flags an answer
  nothing reads: a question asked of the user and then ignored).
- **Required checks.** Ask: what is this investigation MEANINGLESS without?
  Only those gate — four to six is the shape of the core set. Use `depends_on`
  only for genuine ordering (bug-hunter: no cause-hunting until the anomaly
  reproduces). Write each `description` as intent — agents close checkpoints
  better when the point is explicit.
- **Suggested checks.** Everything worth doing where it applies but not
  everywhere goes here — breadth without gates. A required check that does not
  apply to the table in front of the agent gets closed hollow, which is exactly
  the evidence-laundering the rail exists to prevent. When in doubt, suggest.
- **Findings schema.** `standard_v1` unless the workflow's claims need more
  structure; name the known schemas from `grayson workflow show` on a core
  template if the user wants options.

## 2. Draft -> 3. Lint -> 4. Preview -> 5. Store

```
grayson workflow new <name> [--fork <base>]   # scaffold; then edit the YAML
grayson workflow lint                          # repeat until clean
grayson workflow preview <name>                # paste `text` to the user
grayson library push                           # after the user signs off
```

Treat every lint WARNING as design feedback, not noise — each one encodes a
rule from this interview (missing description, an input no check uses, a
depends_on naming a check that does not exist). Iterate the interview -> edit ->
lint -> preview loop until the user confirms the preview; do not push before
they do.

## Rules that are enforced, not advisory

- Core templates are canonical: you cannot edit or shadow them — fork.
- A colleague's workflow edits only under their user id — fork under the
  user's own name instead (`created_by` is stamped automatically).
- Renames are forks, never in-place edits.
"""

HARNESSES = {"cursor", "claude-code", "codex", "copilot"}

#: where each harness reads SKILL.md-format skills from (Codex: none)
_SKILL_DIRS = {
    "claude-code": Path(".claude") / "skills",
    "cursor": Path(".cursor") / "skills",
    "copilot": Path(".github") / "skills",
}

_SKILL_NAME = "grayson-workflow-author"


def _write_workflow_author_skill(root: Path, harness: str) -> str:
    """The workflow-authoring skill, at the harness's native skills location."""
    if harness in _SKILL_DIRS:
        target = root / _SKILL_DIRS[harness] / _SKILL_NAME / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        front = f"---\nname: {_SKILL_NAME}\ndescription: {WORKFLOW_AUTHOR_DESCRIPTION}\n---\n\n"
        target.write_text(front + WORKFLOW_AUTHOR, encoding="utf-8")
        return str(target.relative_to(root))
    # codex has no skills mechanism: a second marked section in AGENTS.md
    return _append_section(
        root / "AGENTS.md", WORKFLOW_AUTHOR, root, mark="grayson-workflow-author"
    )


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
        written.append(_append_section(target, body, root))
    elif harness == "copilot":
        # read by every Copilot surface: VS Code agent mode, Chat, coding
        # agent, code review
        target = root / ".github" / "copilot-instructions.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        written.append(_append_section(target, body, root))
    else:  # codex
        target = root / "AGENTS.md"
        written.append(_append_section(target, body, root))

    written.append(_write_workflow_author_skill(root, harness))

    # standalone copies for reference regardless of harness
    ref_dir = root / ".grayson"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "PROTOCOL.md").write_text(body, encoding="utf-8")
    written.append(str((ref_dir / "PROTOCOL.md").relative_to(root)))
    (ref_dir / "WORKFLOW_AUTHOR.md").write_text(WORKFLOW_AUTHOR, encoding="utf-8")
    written.append(str((ref_dir / "WORKFLOW_AUTHOR.md").relative_to(root)))
    return {"harness": harness, "written": written}


def _append_section(target: Path, body: str, root: Path, mark: str = "grayson") -> str:
    """Write/replace one marker-delimited section, leaving the rest of the file
    (other grayson sections included) untouched."""
    start, end = f"<!-- {mark}:start -->", f"<!-- {mark}:end -->"
    section = f"{start}\n{body}\n{end}\n"
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    if start in existing and end in existing:
        pre = existing.split(start)[0]
        post = existing.split(end, 1)[1]
        target.write_text(pre + section + post, encoding="utf-8")
    else:
        joined = (existing.rstrip() + "\n\n" if existing.strip() else "") + section
        target.write_text(joined, encoding="utf-8")
    return str(target.relative_to(root))
