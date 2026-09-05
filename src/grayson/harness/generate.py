"""Harness skill/instruction generators.

The session protocol lives in one canonical template so every harness stays in
sync. `generate_harness` writes the harness-specific file (Cursor rule, CLAUDE.md
section, Codex AGENTS.md section, or Copilot .github/copilot-instructions.md
section) that teaches an agent how to drive grayson.
"""

from __future__ import annotations

import re
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
- Scope is a wall around rows, not names. Listings (`SHOW TABLES IN SCHEMA ...`,
  `INFORMATION_SCHEMA`) and single-object metadata (`DESCRIBE TABLE`, `SHOW COLUMNS`,
  `SELECT GET_DDL('TABLE', 'DB.S.T')`) are readable for any table — orient freely, and
  use GET_DDL when a check asks where a table is defined. Reading the *rows* of a table
  outside the session scope warns, or is blocked under strict scope. When you need a
  neighbour's rows, ask: file a `scope_request` intervention naming the tables and why;
  the user grants it from the console and those tables join your scope, logged. Never
  route around the wall — an out-of-scope read cited as evidence shows on the
  checkpoint as off-scope.
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
- **Resuming a session** you did not start in this context (a new chat, a compacted
  window, a worker joining late): run `grayson session brief <sid>` first. It carries
  the setup answers, every checkpoint's evidence, the user's verdicts on findings,
  the user's intervention answers, proposals, recent queries, and the next action.
  Re-derive nothing it records, and never re-ask a question it already answers.
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
   `grayson knowledge set`. Record a relationship as
   `{"to": "DB.SCHEMA.TABLE", "on": "THIS_COL = THAT_COL", "cardinality": "many-to-one"}`
   — `on` is this table's column first (just `"COL"` when both share the name,
   comma-separated for a composite key), cardinality is this table first
   (ORDERS → CUSTOMERS is many-to-one). The console's schema map draws exactly
   that; read the `warnings` the command returns and fix any shape it flags.
   If a target has no recorded knowledge at all, settle
   grain/semantics with the user early — or run the `table-onboarding` workflow first.
   Knowledge at session start is a briefing, not the whole doc: facts ranked and capped
   per table, each with a `status` (who vouches: proposed / data_inferred /
   user_confirmed), a `role` under the policy's trust (knowledge, or hypothesis — a lead,
   not a settled fact), and a `standing` (whether what it rests on still holds: current,
   or unverified / stale with the reason). `knowledge_briefing` carries what was left
   out, contested pairs, and recent agent actions; `grayson knowledge show` lists every
   fact. Knowledge has a lifecycle, and the user decides how much of it is yours
   (`grayson knowledge policy`): a fact the evidence shows wrong is superseded with
   `grayson knowledge supersede <table> <fact_id> --fact "..." -e q_0007` (always
   recorded; it executes when the policy lets you, otherwise it waits for the user's
   confirm) or retired with `grayson knowledge retire <table> <fact_id> -e q_0007`
   (evidence required). A contested pair you cannot settle is the user's: name it in
   your findings. A refusal names the setting to change — ask, never route around it.
   The recorded column list is a human artefact and falls behind the warehouse:
   session start reports `knowledge_drift` (columns added, dropped, or retyped since
   the library last looked). Treat drift as a lead, then run
   `grayson knowledge sync <table> --session <sid>` — it merges DESCRIBE into the doc,
   keeps every description, and the DESCRIBE's query id is its evidence. `knowledge show`
   also returns `definitions` (where the table is defined: dbt model, view, DDL) and any
   captured copy under `definition_snapshots` — read them before hypothesising about
   why a table looks the way it does.
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
2a. Test what the team already learned: session start lists `regression_checks`.
   Run its active checks with `grayson checks run <sid>` (MCP: `checks_run`) before
   diagnosing a known problem from scratch. Each approved SQL/expectation pair
   runs through the same guard and returns new query ids you can cite. A failing
   check means its expectation was violated, not that the old root cause returned;
   investigate it. An error (schema changed, guard refused, missing result) is never
   a pass. Tests do not complete checkpoints or accept findings for you.
   After fixing or diagnosing a repeatable issue, turn its evidence query into a
   proposed regression check: `grayson checks propose <sid> <qid> --id orders_no_dupes
   --name "Orders remain unique" --description "Catch recurrence of duplicate order ids"`.
   Default expectation: the query returns no violating rows. For a scalar metric,
   use `--expect scalar --column N --operator eq --value 0` (also lt/lte/gt/gte/ne,
   or between with --upper). Choose an explicit business expectation with the user;
   never adopt today's number as a correctness threshold just because it is on file.
   The user reviews SQL and expectation in the console's Checks page and activates
   it; agents can propose and replay checks, never activate or retire them.
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
   visually, especially for root-cause work. Keep bar charts to a ranked top-N in
   SQL (`ORDER BY n DESC LIMIT 15`): many categories or long names render as
   horizontal bars, but sixty bars is a table, not a picture. For a distribution,
   select the raw numeric column (SAMPLE if the table is large, no GROUP BY) and
   chart it as `--kind histogram -x amount` with no `-y`: grayson bins it locally,
   `--bins N` overrides the default. To show how measures move together, `--kind
   scatter` reports Pearson r with a fitted line, and `--kind correlation` draws a
   matrix over the numeric columns of a sample artifact (`-c col` to choose, 2-8;
   `--method spearman` for ranks) — both computed locally over the cached rows, so
   say so when you cite them. The response's `text` field is a
   terminal rendering of the same chart: paste it into your chat reply, inside a
   code block, so the user sees the shape right in the conversation.
   Some checkpoints **require** a chart: `grayson checkpoint list` shows
   `requires_charts` (allowed kinds and what the picture should show). Build it
   from a query you cite as evidence and close with
   `checkpoint complete <sid> <key> --evidence q_0007 --charts c_002` (MCP: `charts`);
   the gate refuses to close without it.
4. Human input when needed: `grayson intervention request <sid> --kind label_sample ...`,
   then `grayson intervention await <sid> <iid> --timeout 600` (MCP: `intervention_await`,
   which blocks up to 300s per call). The user answers in the web console
   (`grayson ui serve`). Nothing wakes an idle agent: the await call *is* how you
   listen, so while it returns `waiting: true` call it again — do not end your turn to
   "wait for the user", guess, or answer the question yourself. To read a table outside
   your scope, the ask is
   `--kind scope_request --json '{"tables": ["DB.S.T"], "reason": "..."}'`; the answer's
   `granted` list is already in scope when it comes back. When an answer settles a
   durable fact about a table (its grain, a semantic rule, an expectation), persist it
   for future sessions:
   `grayson knowledge add <table> --fact "..." --evidence <iid>` — the user confirms it
   from the console later.
   Where a table is defined is knowledge too: record the dbt model, view, or job with
   `grayson knowledge define <table> --path models/x.sql [--kind dbt_model] [-d "..."]`.
   Point it at the file in the work repo: the repo, commit, hash, and repo-relative
   path are observed and recorded, with who recorded it and when, so a teammate in
   another checkout can follow the pointer — a bare path only means something on this
   machine. Heed its `warnings` (no repo, unknown kind): pass `--repo`/`--kind`, or
   `--capture` to copy the file beside the doc for readers with no checkout. For a
   view, or when no definition repo exists,
   `grayson knowledge sync <table> --session <sid> --ddl` captures GET_DDL beside the doc as
   a dated snapshot. The user's dbt manifest fills these in bulk
   (`grayson knowledge ingest --manifest target/manifest.json`, a user command).
4b. Breadth: `workflow show <name>` lists **suggested checks** alongside the required
   ones. They gate nothing — they are the fundamentals the workflow expects you to
   consider. Do the ones that apply to these tables and close them like any other
   checkpoint (`checkpoint complete` accepts them by key); skip the rest and say
   which in your findings. Required checks may declare `depends_on`: close them in
   that order, it is part of the method.
5. Findings: record them against the workflow schema, each citing evidence:
   `grayson finding add <sid> --json '{...}'`. Read the schema before the first one:
   `grayson workflow show <name>` (and `session start`'s reply) carry
   `findings_schema_spec` — every base field with its rule, the `extra` fields this
   workflow requires (the schema's, built in or the team's own, plus the workflow's
   additions, some with a closed list of allowed values), the discriminator and its
   branches, and an example payload shaped to pass (`grayson schema show <name>` gives
   the same for any schema). Fill every required field; a closed list means one of those values,
   not a paraphrase. Calibrate severity against
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
    "Design a grayson workflow template — or a shared findings schema — interactively "
    "with the user: interview, draft the YAML, lint, preview for sign-off, then store it "
    "in the team library. Use when the user wants a new investigation workflow, to adapt "
    "an existing one, or to define what every finding of a kind must say."
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
- **Required charts.** Charting is otherwise the agent's judgment. Ask, per
  checkpoint: is its content a *shape* — a distribution, a trend, a
  stage-to-stage comparison, how measures move together — that a reader would
  want to see rather than be told about, on every target this workflow runs on?
  Only then add `charts:` to the check, with `kinds` bounding the choice
  (`bar`, `line`, `scatter`, `histogram`, `correlation`; a list of the ones that
  fit, or empty for any) and a `description` of what the picture should show.
  The gate then refuses to close without such a chart built from a cited
  query. Mandate sparingly: a required chart that cannot be drawn for a target
  is closed with whatever chart is to hand, the same hollow close as an
  inapplicable check. A suggested check can carry a requirement too — it
  applies only when the agent does that check.
- **Findings schema.** `standard_v1` unless the workflow's claims need more
  structure. `grayson schema list` names every schema — the built-ins and the
  team library's own — and `grayson schema show <name>` unpacks one;
  `grayson workflow show <name>` carries a workflow's effective schema as
  `findings_schema_spec`. Prefer a library schema the team already has over
  new fields. Then ask: what must every finding from THIS workflow say that
  the schema does not already demand — an owning team, a verdict, a ticket, a
  quantified measure? Each becomes a `findings_fields` entry (`key`,
  `description`, `required`, `choices`). Where the answer is a verdict, close
  the value set with `choices` so it cannot be hedged; use `required: false`
  to document a field without gating on it. A field named like one the schema
  already requires tightens that field (its description and choices) rather
  than duplicating it. The gate then refuses a finding without the required
  fields, exactly as it does for the built-in ones.
- **Tags.** `tags: [orders, finance]` — free labels the console's catalog
  filters by, so the workflow can be found once the library has grown. Ask
  for a domain and, if the team uses them, an owner or cadence.

## 2. Draft -> 3. Lint -> 4. Preview -> 5. Store

```
grayson workflow new <name> [--fork <base>]   # scaffold; then edit the YAML
grayson workflow lint                          # repeat until clean
grayson workflow preview <name>                # paste `text` to the user
grayson library push                           # after the user signs off
```

The console's Workflows tab offers the same loop without YAML: the user
edits a workflow one element at a time — its header, a setup input, a
checkpoint (charts included), a findings field — and every change, there
or in the YAML editor, stops at a review step (diff, preview, lint) before
it is saved. If the user prefers to make the edits themselves, point them
there; what they save is what `workflow preview` then shows you.

Treat every lint WARNING as design feedback, not noise — each one encodes a
rule from this interview (missing description, an input no check uses, a
depends_on naming a check that does not exist). Iterate the interview -> edit ->
lint -> preview loop until the user confirms the preview; do not push before
they do.

## Designing a shared findings schema

When the fields a workflow needs are what the team's findings all need — or
when the user asks for a schema outright — make it a library schema instead
of per-workflow fields. Same loop, one level up: interview, draft, lint,
preview, sign-off, push.

- **Start from what exists.** `grayson schema list` (built-ins, library
  schemas, and which workflows use each). If a workflow already carries the
  right `findings_fields`, promote them rather than retyping:
  `grayson workflow promote <workflow> --schema <name>` creates the schema
  from those fields and points the workflow at it. Otherwise
  `grayson schema new <name> --base <builtin>` (or `--fork <library schema>`).
- **Extend, never replace.** A schema names a built-in `base` and adds to it.
  The base fields, the calibration rules, and the base's required fields stay
  — a finding can never need less under a library schema than under the
  built-in. Ask which built-in is closest: `bug_hunter_v1` if findings are
  diagnoses, `parity_v1` if comparisons, `standard_v1` otherwise.
- **Fields.** For each: is it a fact the agent can state (`description`), is
  a finding meaningless without it (`required`), and can the answer be
  hedged (`choices` closes it — use them for any verdict).
- **A branch.** Ask: is there an honest partial result that needs a
  different shape — `deferred` needs a date and an owner, `withdrawn` needs
  nothing more, `fixed` needs the change reference? Only then set one
  required field with `choices` as the `discriminator` and give each value
  its `branches` entry (a value with no branch needs nothing further). One
  discriminator per schema, and only when the base does not branch already.
  A schema that only lists fields does not need one.
- **Name with a version suffix** (`orders_triage_v1`): findings on record
  cite the schema by name, so a tightened schema is a new one, never a
  rewrite of a name in use.

```
grayson schema new <name> --base <builtin>   # or: grayson workflow promote ...
grayson schema lint                            # repeat until clean
grayson schema preview <name>                  # paste `text` to the user
# set findings_schema: <name> on the workflows that should use it, then
grayson library push
```

The console's Schemas page (under Workflows) offers the same loop without
YAML — fields, branches and the discriminator edit one element at a time,
each reviewed before it is saved — and a workflow page's schema card offers
the promotion.

## Rules that are enforced, not advisory

- Core templates are canonical: you cannot edit or shadow them — fork.
- A colleague's workflow edits only under their user id — fork under the
  user's own name instead (`created_by` is stamped automatically).
- Renames are forks, never in-place edits.
- Deleting a workflow is the user's action (`grayson workflow delete <name>`,
  or the workflow page's danger zone): only its author can, never a core
  template, and never while sessions are still open on it. Never delete one
  yourself — say which workflow is obsolete and why, and let them decide.
- Schemas follow the same rules: built-ins are canonical, a colleague's schema
  forks, a schema a workflow names cannot be deleted (`grayson schema delete`
  is the user's), and a library schema cannot make a finding need less than
  its base does.
"""

HARNESSES = {"cursor", "claude-code", "codex", "copilot"}

#: where each harness reads SKILL.md-format skills from (Codex: none)
_SKILL_DIRS = {
    "claude-code": Path(".claude") / "skills",
    "cursor": Path(".cursor") / "skills",
    "copilot": Path(".github") / "skills",
}

_SKILL_NAME = "grayson-workflow-author"


INSTRUCTION_PATHS = {
    "cursor": ".cursor/rules/grayson.mdc",
    "claude-code": "CLAUDE.md",
    "copilot": ".github/copilot-instructions.md",
    "codex": "AGENTS.md",
}


def plan_harness(root: Path, harness: str, with_mcp: bool = True) -> dict[str, str]:
    """Render every output before writing any file, including shared sections."""
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness '{harness}' (known: {', '.join(sorted(HARNESSES))})")
    body = PROTOCOL + (MCP_NOTE if with_mcp else "")
    rel = INSTRUCTION_PATHS[harness]
    target = root / rel
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    if harness == "cursor":
        front = (
            "---\ndescription: grayson agentic SQL QA protocol\nglobs:\nalwaysApply: false\n---\n\n"
        )
        rendered = front + body
    else:
        rendered = _replace_section(existing, body, target)
    files = {rel: rendered}
    if harness in _SKILL_DIRS:
        skill = (_SKILL_DIRS[harness] / _SKILL_NAME / "SKILL.md").as_posix()
        front = f"---\nname: {_SKILL_NAME}\ndescription: {WORKFLOW_AUTHOR_DESCRIPTION}\n---\n\n"
        files[skill] = front + WORKFLOW_AUTHOR
    else:
        files[rel] = _replace_section(rendered, WORKFLOW_AUTHOR, target, "grayson-workflow-author")
    files[".grayson/PROTOCOL.md"] = body
    files[".grayson/WORKFLOW_AUTHOR.md"] = WORKFLOW_AUTHOR
    return files


def generate_harness(root: Path, harness: str, with_mcp: bool = True) -> dict:
    files = plan_harness(root, harness, with_mcp)
    from grayson.harness.update import apply_plan

    return {"harness": harness, "written": list(files), **apply_plan(root, files)}


def _replace_section(existing: str, body: str, target: Path, mark: str = "grayson") -> str:
    """Replace exactly one well-formed section; retain all surrounding text.

    Recognize the pre-rename markers too, without leaving two conflicting
    protocols installed. Ambiguous or broken markers need a human repair.
    """
    start, end = f"<!-- {mark}:start -->", f"<!-- {mark}:end -->"
    legacy_start, legacy_end = start.replace("grayson", "seekql"), end.replace("grayson", "seekql")
    pairs = [
        (a, b)
        for a, b in ((start, end), (legacy_start, legacy_end))
        if a in existing or b in existing
    ]
    section = f"{start}\n{body}\n{end}"
    if not pairs:
        separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
        return existing + separator + section + "\n"
    a, b = pairs[0]
    if (
        len(pairs) != 1
        or existing.count(a) != 1
        or existing.count(b) != 1
        or existing.index(a) > existing.index(b)
    ):
        raise ValueError(
            f"{target}: damaged or duplicate {mark} markers — repair them before updating"
        )
    inside = existing[existing.index(a) + len(a) : existing.index(b)]
    if re.search(r"<!-- (?:grayson|seekql)(?:-workflow-author)?:(?:start|end) -->", inside):
        raise ValueError(f"{target}: nested harness markers — repair them before updating")
    return existing[: existing.index(a)] + section + existing[existing.index(b) + len(b) :]
