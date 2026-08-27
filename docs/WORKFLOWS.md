# Workflows

A workflow template defines the shape of an investigation: the setup inputs a
human provides, the evidence-gated checkpoints a session must clear, and the
findings schema every claim validates against. Templates are YAML data — small
files an analyst can read, fork, and share.

## Required, suggested, waived

A workflow has to be complete without being universal, and those pull against
each other: a check that is mandatory everywhere gets closed hollow where it
does not apply, which is precisely the evidence-laundering the gate exists to
prevent. So checkpoints come in two kinds, with an escape for the third case.

- **`required_checks`** gate. Keep them to the handful without which the
  investigation is meaningless — four to six is the shape of the core set.
  `depends_on` expresses the rare genuine ordering (bug-hunter will not let you
  hunt a cause before the anomaly reproduces). `uses_inputs` names which of the
  user's setup answers a check works from — it tells the agent what the check is
  testing, and lint uses it to catch a required input no checkpoint ever reads.
- **`suggested_checks`** carry breadth and gate nothing. They are surfaced at
  session start and in the console; an agent does the ones that fit the tables
  in front of it and closes those like any other checkpoint. This is how a
  workflow names thirty fundamentals without demanding all thirty on a
  five-column lookup table.
- **Waiving** handles the last case: a *required* check that genuinely does not
  apply here. The agent asks (an intervention saying why); a human waives it
  with a reason. Waived satisfies the gate and never renders as complete.

## The core templates

Six ship built-in:

| Workflow | Purpose |
|---|---|
| `bug-hunter` | Replicate a reported anomaly and isolate its source |
| `pipeline-qa` | Validate a transform/pipeline stage end to end |
| `table-health` | Single-table health: grain, nulls, distributions, domain validity, freshness |
| `semantic-rule-qa` | Test stated business rules against the data |
| `migration-parity` | Old-vs-new parity: schemas, counts, keys, values, null semantics |
| `table-onboarding` | Build the base descriptor for an undocumented table |

`bug-hunter` opens by checking the *expectation* itself — a large share of
reported anomalies are misunderstandings of the grain or of a deliberate rule,
and finding that out costs one query here and a day of lineage walking later.
Its findings schema takes a `resolution`: `root_caused`, or `inconclusive` with
what is still open. An investigation that reproduces and bounds an anomaly
without isolating it is a real result, and a schema that accepts only a
confident answer will be given one.

`migration-parity` doubles as the verification stage for every other workflow.

Core templates are **canonical**: a library file cannot shadow a core name (a
collision is rejected and reported, never merged), so core behavior changes
only with a grayson release. Customization forks under a new name.

## Team workflows: fork, edit, share

The library's `workflows/` directory extends the catalog with the team's own
templates, shared by git like everything else in the library
([LIBRARY.md](LIBRARY.md)). Each carries provenance in the YAML itself:
`created_by` (the author's `grayson user` id) and, for forks, `forked_from`
lineage.

```bash
grayson workflow new orders-slim-health --fork table-health
```

Editing is ownership-aware, enforced server-side in the console: workflows
you created edit in place; a collaborator's workflow — or a core template —
forks a copy under your id instead, so nothing shared breaks under someone
else's edit. A legacy file with no recorded author is editable by anyone, and
the first save stamps the editor's id.

## Lint: broken workflows are loud

```bash
grayson workflow lint
```

Errors (non-zero exit, CI-friendly for the qa-library repo): YAML that does
not parse or validate, core-name shadowing, duplicate workflow names,
duplicate checkpoint keys, unknown findings schemas. Warnings: missing
descriptions (agents pick workflows by description), workflows with no
checkpoints, file/name mismatches, a `depends_on` naming a check the workflow
does not define, a `uses_inputs` naming an input it does not have, and a
required setup input no checkpoint works from — a question asked of the user and
then ignored. The same semantic rules run over the **core** templates in the
test suite: they are canonical and non-editable, so their quality is grayson's
to keep rather than a reviewer's to notice. A file that fails to load is reported
everywhere workflows are listed — CLI, MCP (`workflow_list.library_problems`),
and red-badged in the console — never silently skipped: **loadable means
runnable**.

## The Workflows tab

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/workflows_dark.png">
  <img src="img/workflows_light.png" alt="The Workflows tab: a gallery of core and team workflows with a create-or-fork card">
</picture>

The console's Workflows tab makes the catalog browsable: a gallery of core and
team workflows (lint failures shown red, in place), and a create-or-fork card.
Each workflow's page draws the session flow stage by stage — evidence gates
and human-approval points marked — with every checkpoint, setup input, and
findings-schema field unpacked, plus usage and provenance.
`/workflows/{name}/yaml` exports any definition for sharing.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/workflow_detail_dark.png">
  <img src="img/workflow_detail_light.png" alt="A workflow detail page: the session flow drawn with evidence gates, checkpoints unpacked, setup inputs, findings schema, and provenance">
</picture>
