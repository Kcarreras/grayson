# Workflows

A workflow template defines the shape of an investigation: the setup inputs a
human provides, the evidence-gated checkpoints a session must clear, and the
findings schema every claim validates against. Templates are YAML — small
files an analyst can read, fork, and share.

## Required, suggested, waived

Checkpoints come in two kinds, with an escape hatch for the third case:

- **`required_checks`** gate. Keep them to the handful without which the
  investigation is meaningless — four to six in the core set. `depends_on`
  expresses genuine ordering (bug-hunter: no cause-hunting until the anomaly
  reproduces). `uses_inputs` names which setup answers a check works from;
  lint uses it to catch a required input no checkpoint reads.
- **`suggested_checks`** carry breadth and gate nothing. Surfaced at session
  start and in the console; the agent does the ones that fit the tables in
  front of it. This is how a workflow names thirty fundamentals without
  demanding all thirty on a five-column lookup table.

## Required charts

Whether an agent charts is otherwise its own judgment, prompted by the
protocol's prose. Where a checkpoint's content *is* a shape — a distribution,
a trend, a stage-to-stage comparison, how measures move together — the
workflow can require the picture:

```yaml
required_checks:
  - key: null_completeness
    title: Check nulls and completeness
    charts:
      - kinds: [bar]
        description: null rate per column, ranked, so the sparse columns stand out
```

Each entry is one required chart. `kinds` bounds the choice (`bar`, `line`,
`scatter`, `histogram`, `correlation`; empty means any) and `description`
says what the picture should show, the way a check's description says what
the evidence should show. The gate then refuses to close the checkpoint
without a chart of an allowed kind whose query is cited as evidence
(`checkpoint complete ... --evidence q_0007 --charts c_002`; MCP: `charts`).
`checkpoint list` shows the requirement as `requires_charts`; the console
marks the open checkpoint "needs chart" and links the charts it closed with;
the brief and the report carry them.

A suggested check can carry a requirement too — it applies only when the
agent takes the check up. Waiving still works: a required chart that cannot
be drawn for a target is a check that does not apply, and the honest exit is
the user's waiver, not whatever chart is to hand. So mandate sparingly. In
the core set the requirement sits where the shape is universal — nulls per
column, the label's distribution, row counts per pipeline stage, keys only
in old/new/both, redundancy as a correlation matrix — and nowhere else:

| Workflow | Checkpoints that require a chart |
|---|---|
| `bug-hunter` | `scope_blast_radius` (line or bar) |
| `table-health` | `null_completeness` (bar), `distributions` (histogram or bar), `freshness` (line or bar) |
| `pipeline-qa` | `rowcount_reconciliation`, `measure_conservation` (bar) |
| `migration-parity` | `rowcount_parity`, `value_parity`, `aggregate_parity` (bar) |
| `feature-readiness` | `label_profiled` (bar or histogram), `feature_profiled` (bar), `missingness_characterized` (bar or line), `redundancy_assessed` (correlation or scatter) |
| `semantic-rule-qa` | `rule_coverage`, `accuracy_estimate` (bar) |
| `table-onboarding` | `structure_profiled` (bar) |

Several suggested checks carry one as well (onset dating as a line, load lag
as a histogram, the outlier scan as a histogram). The workflow-author skill
asks the question per checkpoint during the interview.

A setup input can declare `adds_scope: true`: the tables the human names in
its answer join the session's readable scope at start. That is how a
strict-scoped workflow gets deliberate context — table-onboarding's
`related_tables` input asks for upstream/downstream neighbours, and exactly
those become readable beside the target instead of the scope loosening.
A neighbour that surfaces mid-session is asked for the same way: a
`scope_request` intervention names it and why, and the human's grant widens
the scope from the answer itself (see [SESSIONS.md](SESSIONS.md)).
- **Waiving** covers a *required* check that genuinely does not apply. The
  agent asks via an intervention; a human waives with a reason. Waived
  satisfies the gate and never renders as complete.

The design rule behind the split: a check that is mandatory everywhere gets
closed hollow where it does not apply — the evidence-laundering the gates
exist to prevent.

## The core templates

Seven ship built-in:

| Workflow | Purpose |
|---|---|
| `bug-hunter` | Replicate a reported anomaly and isolate its source |
| `pipeline-qa` | Validate a transform/pipeline stage end to end |
| `table-health` | Single-table health: grain, nulls, distributions, domain validity, freshness |
| `semantic-rule-qa` | Test stated business rules against the data |
| `migration-parity` | Old-vs-new parity: schemas, counts, keys, values, null semantics |
| `table-onboarding` | Build the base descriptor for an undocumented table |
| `feature-readiness` | Assess a feature table / training set before it feeds a model |

Notes on three of them:

- `bug-hunter` opens by checking the *expectation* itself — many reported
  anomalies are misunderstandings of the grain or of a deliberate rule, and
  finding that out costs one query here versus a day of lineage walking later.
  Its schema accepts `resolution: inconclusive` (with what is still open):
  reproducing and bounding an anomaly without isolating it is a real result.
- `feature-readiness` answers a decision — is this set safe to train on? —
  not generic profiling (that lives in `table-health`/`table-onboarding`).
  Its checkpoints are what sinks models: population and grain, label
  distribution, missingness *mechanism*, redundancy, leakage and
  point-in-time correctness. Its schema requires a `leakage_assessment`.
- `migration-parity` doubles as the verification stage for any other workflow.

Core templates are **canonical**: a library file cannot shadow a core name,
so core behavior changes only with a grayson release. Customization forks
under a new name.

## Findings schemas and severity

Each workflow validates claims against a closed schema — six ship:
`standard_v1`, `bug_hunter_v1`, `parity_v1`, `pipeline_qa_v1`, `rule_qa_v1`,
`feature_readiness_v1`. Two use a discriminator: `bug_hunter_v1` requires a
`resolution` (`root_caused`, or `inconclusive` with `remaining_hypotheses`);
`rule_qa_v1` requires a `finding_kind`, since an accuracy estimate needs a
sample size and frame while an unreachable-category defect has no sample at
all.

The contract is published, not discovered one rejection at a time.
`grayson workflow schemas` unpacks every built-in schema; `grayson workflow
show <name>` (and the reply to `session start`; MCP: `workflow_show`,
`session_start`) carries a workflow's effective schema as
`findings_schema_spec`: the base fields with the rule each is held to, the
`extra` fields required, the discriminator and its branches, the enforced
calibration rules, and an example payload shaped to pass. The console shows
the same on every workflow page, and the catalog lists the schemas side by
side with the workflows that use them.

### A workflow's own fields

The built-in schemas are fixed with the release. A workflow extends the one
it names with `findings_fields` — the structure its team's findings need
that the schema does not demand:

```yaml
findings_schema: standard_v1
findings_fields:
  - key: owner_team
    description: Which team owns the fix.
    choices: [data-platform, finance-eng]
  - key: ticket
    description: The tracker id, once one exists.
    required: false
```

`required` (default true) makes the gate refuse a finding without the field;
`choices` closes the value set, so a verdict cannot be hedged into prose. A
field named like one the schema already requires — `resolution` under
`bug_hunter_v1`, say — tightens that field (its description and choices)
instead of adding a second. Keys are the workflow's own: a base field every
finding carries (`title`, `severity`, `evidence`, …) is refused. The
effective schema is the built-in plus these, in that order, and it is what
the engine validates against, what `findings_schema_spec` describes, and
what the preview shows at sign-off.

### Shared schemas in the library

When the fields one workflow needed turn out to be what the team's findings
all need, they become a **library schema**: a file in the library's
`findings_schemas/` directory, named, owned, and git-shared like a workflow,
that several workflows point at with `findings_schema`.

```yaml
name: orders_triage_v1
title: Orders triage
description: Findings on the orders pipeline — who owns the fix, and whether it shipped.
base: standard_v1
fields:
  - key: owner_team
    description: Which team owns the fix.
    choices: [data-platform, finance-eng]
  - key: outcome
    description: Whether the fix shipped, was deferred, or the finding was withdrawn.
    choices: [fixed, deferred, withdrawn]
discriminator: outcome
branches:
  fixed:
    - key: fix_reference
      description: The change that fixed it.
  deferred:
    - key: deferred_until
      description: When it will be picked up, and by whom.
```

Two rules shape it. A library schema **extends** a built-in, never replaces
one: `base` names the built-in, whose fields, calibration rules and required
extras all stay, so no library schema can make a finding need less than the
built-in it starts from and findings stay comparable across the library. A
field named like one the base requires tightens it. And a schema may
**branch**: one required field with `choices` is the `discriminator`, and
each value's `branches` entry lists the further fields that value needs — a
value with no entry needs nothing more. That is what lets an honest partial
result (`deferred`, `inconclusive`) have a shape of its own instead of being
forced into a confident one. One discriminator per schema, and only when the
base does not branch already (`bug_hunter_v1` and `rule_qa_v1` do).

The effective schema a finding validates against is then three layers with
one merge rule: built-in, then library schema, then the workflow's own
`findings_fields`.

```bash
grayson schema list                            # built-ins, library, who uses each
grayson schema new orders_triage_v1 --base standard_v1
grayson schema lint
grayson schema preview orders_triage_v1        # the sign-off form
grayson workflow promote orders-slim-health --schema orders_slim_v1
grayson schema delete orders_triage_v1
```

`workflow promote` is how most shared schemas start: it lifts a workflow's
`findings_fields` into a new schema and points the workflow at it, and the
effective contract does not change. Ownership follows the workflow rules —
built-ins are canonical, a colleague's schema forks under your id, a legacy
file is anyone's — with one addition: a schema a workflow still names cannot
be deleted. Lint (`schema lint`, also part of `workflow lint` and `library
doctor`) reports unloadable files as errors and, as warnings, a missing
description, a field without one, a name with no version suffix (findings on
record cite the schema by name, so a tightened schema should be a new one),
a discriminator with no branch, and a schema no workflow uses.

The console's **Schemas** page (under Workflows) is the same loop without
YAML: a filterable catalog of built-in and library schemas beside the
workflows that use them, and for each library schema element-by-element
editing of its header, fields, discriminator and branches, every change
reviewed before it is saved. A workflow page's schema card offers the
promotion, and links the schema it uses. The MCP tools `schema_list` and
`schema_show` mirror the CLI.

Severity has a published scale (`grayson finding rubric`) so findings don't
all drift to "high". grayson never judges whether a severity is right — that
is what accept/reject is for — but the top rungs cost the specificity a real
severe finding already has:

- `confidence: high` requires a `reproduction`.
- `severity: critical` or `high` requires `affected_objects`.

## Team workflows: fork, edit, share

The library's `workflows/` directory extends the catalog with your team's
templates, git-shared like everything else ([LIBRARY.md](LIBRARY.md)). Each
carries provenance in the YAML: `created_by`, and `forked_from` for forks.

```bash
grayson workflow new orders-slim-health --fork table-health
grayson workflow preview orders-slim-health   # the standard confirmation form
```

`workflow preview` renders any template the way a person signs off on it —
setup inputs, gating checks with order and the answers each uses, suggested
breadth, session shape — with lint findings attached for library workflows.
Show the preview, not raw YAML.

`grayson harness init` installs a **workflow-author skill** for the agent: an
interview (purpose, fork-or-fresh, inputs wired to checks, the "meaningless
without" test for required checks, breadth as suggested, required charts,
schema and the workflow's own findings fields, tags) followed by
the draft → lint → preview → sign-off → push loop. One canonical SKILL.md,
written to each harness's skills directory (`.claude/skills/`,
`.cursor/skills/` for Cursor ≥2.1, `.github/skills/` for VS Code Copilot;
Codex gets an AGENTS.md section; a reference copy lands at
`.grayson/WORKFLOW_AUTHOR.md`). It ships with grayson, not the library, so
the interview cannot drift from what lint enforces.

Editing is ownership-aware, enforced server-side: workflows you created edit
in place; a collaborator's workflow — or a core template — forks under your
id instead. A legacy file with no author is editable by anyone; the first
save stamps the editor's id. Renames are forks, never in-place edits.
Deleting follows the same rule (`grayson workflow delete <name>`, or the
workflow page's danger zone): only the author, never a core template, and
never while a session is still open on it — open sessions resolve their
checkpoints and schema from the file on every call. The library's git
history keeps the file; `library push` propagates the removal. A file that
no longer parses has no author to protect and can be removed by anyone,
which is how a broken library file gets cleaned up.

`tags: [orders, finance]` on a workflow are free labels; the console's
catalog filters by them, and nothing else reads them.

Findings schemas of the team's own live beside workflows in
`findings_schemas/`, under the same ownership rules — see
[Shared schemas in the library](#shared-schemas-in-the-library).

## Lint

```bash
grayson workflow lint    # non-zero exit on errors; CI-friendly
```

Errors: YAML that does not parse or validate (a required chart of an unknown
kind is one — a session could never satisfy it; so is a findings field
named like a base field, or two with the same key), core-name shadowing,
duplicate workflow names or checkpoint keys, unknown findings schemas.
Warnings: missing descriptions (agents pick workflows by description), no
checkpoints, file/name mismatches, `depends_on` naming an undefined check,
`uses_inputs` naming a missing input, a required input no checkpoint reads,
a required chart that does not say what it should show, a findings field
with no description.

A file that fails to load is reported everywhere workflows are listed — CLI,
MCP (`workflow_list.library_problems`), red-badged in the console — never
silently skipped: loadable means runnable. The same semantic rules run over
the core templates in the test suite.

## The Workflows tab

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/workflows_dark.png">
  <img src="img/workflows_light.png" alt="The Workflows tab: a gallery of core and team workflows with a create-or-fork card">
</picture>

The console's Workflows tab is the browsable catalog, built to stay usable
as the library grows: one filterable list (text search over name,
description, schema and tags; sort by name, usage, recency or size; filter
chips for core, team, mine, forks, chart requirements, own findings fields,
open sessions, lint failures, and every user tag in use), with lint failures
shown red in place — removable from there — a create-or-fork section, and
the built-in findings schemas unpacked beside the workflows that use them.

Each workflow's page opens with a count strip (inputs, gates, suggested
checks, chart requirements, extra finding fields, sessions) and the session
flow drawn with its evidence gates and human-approval points. Checkpoints
are a filterable list of their own, each fold carrying its gating order,
the inputs it works from and the charts it requires; setup inputs say which
checkpoints read them (a required input nobody reads is flagged); the
findings schema is unpacked in full — base fields and their rules, the
`extra` fields required with their source, discriminator branches, enforced
calibration, and an example payload. Library workflows show lint's notes.
`/workflows/{name}/yaml` shows the definition in the console, with a copy
button and a download (`?raw=1`).

**Editing** is element by element, on the workflow's own page, for its
author: the header (title, description, tags, guard and scope defaults,
schema), each setup input, each checkpoint — title, intent, prerequisites,
inputs, required charts as `kinds: what it should show` lines — and each
findings field, plus add, reorder, move between required and suggested, and
remove. The whole file is also editable as YAML. Either way, every change
stops at a **review step** first: the exact diff a save would write, the
template as `workflow preview` renders it, and lint's warnings; nothing is
written until it is confirmed, and confirming writes exactly what was
reviewed. A save rewrites the file in grayson's canonical layout, so a
hand-edited file's comments are normalised on its first save.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/workflow_detail_dark.png">
  <img src="img/workflow_detail_light.png" alt="A workflow detail page: the session flow drawn with evidence gates, checkpoints unpacked, setup inputs, findings schema, and provenance">
</picture>
