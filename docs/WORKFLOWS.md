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
without" test for required checks, breadth as suggested, schema) followed by
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

## Lint

```bash
grayson workflow lint    # non-zero exit on errors; CI-friendly
```

Errors: YAML that does not parse or validate (a required chart of an unknown
kind is one — a session could never satisfy it), core-name shadowing,
duplicate workflow names or checkpoint keys, unknown findings schemas.
Warnings: missing descriptions (agents pick workflows by description), no
checkpoints, file/name mismatches, `depends_on` naming an undefined check,
`uses_inputs` naming a missing input, a required input no checkpoint reads,
a required chart that does not say what it should show.

A file that fails to load is reported everywhere workflows are listed — CLI,
MCP (`workflow_list.library_problems`), red-badged in the console — never
silently skipped: loadable means runnable. The same semantic rules run over
the core templates in the test suite.

## The Workflows tab

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/workflows_dark.png">
  <img src="img/workflows_light.png" alt="The Workflows tab: a gallery of core and team workflows with a create-or-fork card">
</picture>

The console's Workflows tab is the browsable catalog: core and team workflows
(lint failures red, in place) and a create-or-fork card. Each workflow's page
draws the session flow — evidence gates and human-approval points marked —
with checkpoints, setup inputs, and schema fields unpacked, plus usage and
provenance. `/workflows/{name}/yaml` exports any definition.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/workflow_detail_dark.png">
  <img src="img/workflow_detail_light.png" alt="A workflow detail page: the session flow drawn with evidence gates, checkpoints unpacked, setup inputs, findings schema, and provenance">
</picture>
