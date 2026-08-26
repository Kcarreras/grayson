# Workflows

A workflow template defines the shape of an investigation: the setup inputs a
human provides, the evidence-gated checkpoints a session must clear, and the
findings schema every claim validates against. Templates are YAML data — small
files an analyst can read, fork, and share.

## The core templates

Six ship built-in:

| Workflow | Purpose |
|---|---|
| `bug-hunter` | Replicate a reported anomaly and isolate its source |
| `pipeline-qa` | Validate a transform/pipeline stage end to end |
| `table-health` | Single-table health: nulls, duplicates, drift, distributions |
| `semantic-rule-qa` | Test stated business rules against the data |
| `migration-parity` | Old-vs-new parity: schemas, counts, keys, values |
| `table-onboarding` | Build the base descriptor for an undocumented table |

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
checkpoints, file/name mismatches. A file that fails to load is reported
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
