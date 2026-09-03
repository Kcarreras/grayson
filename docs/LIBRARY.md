# The team library

Knowledge compounds through an ordinary git repo — no server. Table facts
with provenance, QA views, workflow templates, external check results,
report profiles, and published records are all plain, merge-friendly files.
Solo mode keeps them in the workspace; team mode shares them through the
repo.

## Linking a library

```bash
# one-time, per team: create an EMPTY repo on your git host, then
grayson library link git@github.com:your-org/qa-library.git --auto-push
```

This clones the repo, scaffolds the structure (`knowledge/`, `views/`,
`workflows/`, `findings_schemas/`, `checks/`, `records/`, `reports/`),
pushes the first commit,
and points the workspace at the clone. Collaborators run the same command.

- `--auto-push` commits and pushes every library write; otherwise
  `grayson library push` batches.
- Concurrent pushes are handled: when a teammate pushed first, the rejected
  push rebases the library commit onto theirs and retries once. On a genuine
  conflict (both edited the same lines) the write stays committed locally and
  the sync result says to `grayson library pull`, resolve, and `push` —
  nothing is lost either way. Agent surfaces should check `library_sync.ok`.
- `grayson library status|pull` keep the clone fresh.
- `grayson library extract` splits a solo workspace's assets into a new repo.
- Git auth stays inside git — grayson never handles those credentials.

### What each folder holds

| Folder | Contents | Written by |
|---|---|---|
| `knowledge/` | One document per table (`<db>/<schema>/<table>.md`): descriptors, dated definition observations, facts with provenance; `glossary.md` | agents propose, humans confirm |
| `views/` | The QA view library: `registry.yaml` (name, purpose, source tables, base files, DDL path) and `ddl/*.sql` | humans register the views agents proposed |
| `workflows/` | Workflow templates: overrides of the built-ins and custom investigation types | humans, `grayson workflow fork` |
| `findings_schemas/` | The team's own findings schemas: each extends a built-in with fields and, optionally, branches ([WORKFLOWS.md](WORKFLOWS.md#shared-schemas-in-the-library)) | humans, `grayson schema new`, `grayson workflow promote` |
| `checks/` | External check results as JSON (Airflow, dbt, scheduled QA jobs) | automation |
| `records/` | Published session output: accepted findings, verified fixes, and each closed session's `report.md` + `report.json` | sessions, at human-approved moments |
| `reports/` | Report **profiles** (`*.yaml`) — how reports render. The reports themselves are in `records/` | humans |

The scaffold writes a README into each folder saying the same, so the repo
explains itself when browsed on the git host. Linking a library scaffolds
only what is missing, so re-running `grayson library link <path-or-url>`
against a library made by an older grayson adds the READMEs without touching
anything else.

## Knowledge, with provenance

Facts about tables carry a status — `proposed` / `data_inferred` /
`user_confirmed` — and agents can never mark a fact user-confirmed:
confirmation is a human action (a Confirm button on the console's table page,
or `grayson knowledge confirm`). Structured base descriptors (grain, columns,
relationships, freshness, owners) live beside free-form facts; each table's
completeness report shows what is still undescribed.

The table page is editable where a human is the authority: write facts
directly (recorded user-confirmed — you *are* the confirmation), fill in
column descriptions and the grain/freshness/owners descriptor, and answer
open questions inline — the question retires and the answer becomes a
confirmed fact. Agents get the same lightweight path for questions a user
simply answers in chat: `grayson knowledge answer` (MCP: `knowledge_answer`)
records the relayed answer as a `proposed` fact and retires the question, no
session required — confirmation still waits for the human.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/knowledge_dark.png">
  <img src="img/knowledge_light.png" alt="The Knowledge tab: table completeness cards and the relationship canvas of the whole library">
</picture>

The console's Knowledge tab adds a relationship canvas of the whole library
(Cytoscape + ELK, vendored); each table page shows its completeness report,
facts, and covering external checks.

### Recording a relationship

The schema map draws exactly what the descriptor says, so the shape matters.
Each entry in a table's `relationships` list is:

```yaml
relationships:
- to: SHOP.PUBLIC.CUSTOMERS          # fully qualified; a bare name is completed
                                      #   from this table's DB.SCHEMA, with a warning
  on: CUSTOMER_ID                     # both sides share the name
  cardinality: many-to-one            # THIS table first: many orders, one customer
- to: SHOP.PUBLIC.PROMOS
  on: PROMO_CODE = CODE               # this table's column = the other's column
  cardinality: many-to-one
  note: only orders placed after 2024-01 carry a code
- to: SHOP.PUBLIC.ORDER_LINES
  on: ORDER_ID, LINE_NO = LINE        # composite key: comma-separated pairs
  cardinality: one-to-many
```

Rules, all enforced on read and write by `knowledge/relationships.py`:

- **`on` is column pairs, this table first.** `COL` when both tables use the
  same name, `THIS_COL = THAT_COL` when they differ, `, ` between the parts of
  a composite key. Qualified forms (`ORDERS.CUSTOMER_ID = CUSTOMERS.ID`), lists,
  `{from, to}` mappings and the aliases `join`, `join_key(s)`, `keys`, `using`
  are read and rewritten to this form. A join that is not a column equality
  (`lower(email) = lower(EMAIL)`) is kept as written, flagged, and drawn in
  italics — the map cannot say whose column is whose.
- **`cardinality` is one of** `one-to-one`, `one-to-many`, `many-to-one`,
  `many-to-many`, **read from this table towards `to`.** `1:N`, `N:1`,
  `one_to_many`, `many to one` and the like are accepted and canonicalised.
  Anything else is kept as written and flagged.
- **One relationship, one edge.** A declaration from either side, in any
  accepted spelling, merges with the other side's: both tables recording
  `ORDERS ⟷ CUSTOMERS` on `CUSTOMER_ID` is one solid line; only one recording
  it is a dashed line; the two disagreeing on cardinality or on the join
  columns is drawn in the attention colour and spelled out in the tooltip.

`knowledge set` (CLI and MCP) returns `warnings` naming every shape it had to
guess or could not read — the agent that wrote the entry is the right one to
fix it. `grayson library doctor` reports the same for what is already in the
library, plus a join-key column the table's own column list does not have.

## Where a table is defined, and what it is made of

A knowledge doc holds three different things, and they come from different
authorities:

- **Semantics** — grain, what a column means, relationships, owners. Only
  humans know these; they are facts and descriptor fields with provenance.
- **Structure** — the column list the warehouse actually has. The warehouse
  owns it, and a hand-typed copy silently falls behind.
- **Definition** — the dbt model, the view's SELECT, the DDL: what actually
  explains *why* the table looks the way it does. For a base table this lives
  outside the warehouse, in the repo that owns it.

The library records the first as testimony, and the other two as *pointers
plus dated observations* — never as an undated copy posing as the authority.

**Structure: `knowledge sync`.** Merges DESCRIBE into the doc: names, types,
nullability, and order come from the warehouse; every description and human
field is kept; a recorded column the warehouse no longer has keeps its
description and is flagged `dropped`. Through a session the DESCRIBE is an
ordinary guarded, audited statement and its query id is the observation's
evidence, recorded under `structure`:

```bash
grayson knowledge sync DB.SCHEMA.TABLE --session <sid>      # MCP: knowledge_sync
grayson knowledge sync DB.SCHEMA.TABLE --session <sid> --ddl  # also capture GET_DDL
```

At session start grayson DESCRIBEs each target the library records columns
for and reports `knowledge_drift` — columns added, dropped, or retyped since
the library last looked — with a hint to sync and describe the new ones. A
column that appeared since the last investigation is a lead, not noise.
Targets nobody has described are a `knowledge_gap`, not drift.

**Definitions.** The doc's `definitions` list says where the table is
defined, completely enough that every reader of the library — a teammate in
another checkout, an agent with no checkout, whoever inherits the table —
can find it. Each entry answers three questions:

- **who**: `recorded_by` (the actor kind, `agent` or `user`), `author` (the
  configured user id), `captured_at` — the same provenance a fact carries,
  stamped on every write whichever surface made it;
- **what**: `kind` (`dbt_model`, `dbt_seed`, `dbt_snapshot`, `view`, `ddl`,
  `job`, `other`), a one-line `description`, and a `hash` of the text it was
  recorded from, so a later pass can say "changed since";
- **where**: `repo` (one spelling per repo, `github.com/org/dbt`, whichever
  transport the clone used), `ref` (the commit it was read at), `branch`, and
  `path` *relative to that repo* — never to someone's home directory.

`knowledge define` records one entry from a local file and observes the
"what" and "where" instead of asking for them: the repo's remote and HEAD,
the file's hash, the repo-relative path, and `dirty: true` when the working
copy differs from `ref`. A path that does not exist on this machine is
recorded as a pointer, and the command's `warnings` say what is still short
of complete (no repo, unknown kind). `library doctor` flags a bare path with
no repo and no captured copy, since no collaborator can follow it.

```bash
grayson knowledge define DB.SCHEMA.TABLE --path models/marts/orders.sql        # MCP: knowledge_define
grayson knowledge define DB.SCHEMA.TABLE --path jobs/load.py --kind job --capture   # copy it beside the doc
grayson knowledge define DB.SCHEMA.TABLE --path models/x.sql --repo github.com/org/dbt --ref v3   # a pointer
```

The console's table page records one too, with user provenance. `knowledge
set-files` replaces the whole path list the same way (the format-1
`definition_files` list of bare paths is still written and read; it is the
same record), and `knowledge set` accepts `definitions` entries verbatim,
stamped with who and when. A dbt project fills them in bulk from the manifest
that already feeds `checks ingest`:

```bash
grayson knowledge ingest --manifest target/manifest.json [--repo org/dbt] [--all]
```

For every table the library documents (`--all` for every model), that records
the model's path, unique id, package, materialization, and hash; copies the
compiled SQL beside the doc as `TABLE.dbt.sql`; and fills column descriptions
from the model's `schema.yml` where the doc has none — a description already
written in grayson stands. Re-running reports which definitions changed.

**Snapshots** (`TABLE.dbt.sql`, `TABLE.ddl.sql`, and `TABLE.<file>.source.<ext>`
for a file captured by `knowledge define --capture`) are dated copies beside
the doc, headed with when and from what they were captured. They exist for one
reader: the collaborator served the library read-only, with no warehouse and
no dbt checkout, whose agent can otherwise dereference nothing. `knowledge
show` and the console carry them; `library doctor` flags a referenced snapshot
that has gone missing. The `--ddl` capture is for views, where GET_DDL is the
defining SELECT, or for a table with no definition repo at all.

**A verified fix is knowledge.** When a proposal's verification passes, the
fix lands as a `data_inferred` fact on the tables the before/after queries
touched — title, what changed, proposal and session ids, both query ids as
evidence. The published record already holds the full story for `records
search`; the fact is what puts it in the *briefing* of the next session over
the same table. The structure does not refresh itself: if the fix changed
columns or a model, `knowledge sync` (and a manifest re-ingest) is the step
that makes the descriptor follow, and the next session's drift line catches
it if nobody does.

## Format stability

The library's file formats are a public interface with a compatibility
contract — the code adapts to the files, never the reverse:

- **Useful with zero grayson.** Markdown + YAML frontmatter and small JSON:
  readable from the raw repo by a human or any agent. No change trades this
  away.
- **Additive-only within a format version.** Fields never change meaning or
  disappear; a rename writes both names and reads either for a window.
- **Unknown fields round-trip.** An older grayson preserves what a newer one
  (or a hand edit) wrote — a rewrite never strips fields it doesn't know.
- **Docs are stamped** (`format: N`; unstamped means format 1). A newer doc
  loads best-effort but is **refused for rewrite**, with an upgrade message —
  never silent loss. Published records carry the same stamp; a record from a
  newer grayson serves best-effort (fields are additive) and `doctor` flags
  it for upgrade.
- **Breaking changes ship only with `grayson library migrate`:** clean git
  tree required, one labeled commit, run deliberately by a human. Never
  implicit on read; git history is the rollback.

```bash
grayson library doctor    # read-only health pass; non-zero exit on errors
grayson library migrate   # idempotent; refuses on a dirty tree
```

Hand edits and merges are first-class, so drift is normal — `doctor` finds
it on demand: the doc that no longer parses, the fact id a merge duplicated,
the newer-format doc this version can read but not rewrite.

## Attribution: user ids

```bash
grayson user set kcg     # once, stored in ~/.grayson/config.toml
```

Every fact carries the id (`author`), and every library commit carries a
`Grayson-User:` trailer (plus `Grayson-Via: mcp-agent` for agent-surface
writes) — history stays attributable even from shared machines.
`GRAYSON_USER_ID` overrides per process.

## Records: sessions stay local, their output travels

Raw session state (query cache, live progress, interventions) never leaves
the workspace. At the human-approved moments — a finding accepted, a fix
verification recorded — the distilled record publishes into `records/` as
small, author-stamped JSON. Rejected findings never publish; accepting a
superseding finding republishes the superseded one so the library copy stops
reading as current.

Every published record carries the queries it cites — statement, executed
form, timestamp, outcome, tables — as `evidence_queries`, because a query id
is a per-session counter (`q_0002` means nothing outside its session) and the
SQL lives only in local session state. The record page shows them one fold
away, links them to the query page while the session is local, and qualifies
every id with its session. Results stay local; the statement and its stats
are what a reviewer needs.

From any linked workspace, `grayson records search` answers "how did anyone
on the team diagnose and fix something like this" — collaborators' records
merge into every search, badge `team` in the console, and open from the
published copy when the session isn't local.

## Removing records

A session's published output — its findings, verified fixes, and report —
can be removed from the library as a unit, by **its author or a library
admin**: a teammate's records are theirs. The record page in the console
offers it (*Remove from the library*), as does the session page's delete
fold for a local session, and the CLI:

```bash
grayson records delete <sid> --reason "session restarted; superseded by 2026…"
```

The removal lands as one library commit carrying the reason and the actor's
`Grayson-User:` trailer, so `git log` says who removed what and why, and
`git revert` brings it back. With `--auto-push` it goes up at once;
otherwise `grayson library push` carries it. Records published before a user
id was set carry no author, so only an admin removes them through grayson
(or anyone, through git). In solo mode there is no team to protect, and the
records are simply yours.

### Admins

`library.toml` at the library root names the admins — user ids as set with
`grayson user set` — and travels with the repo:

```toml
[library]
admins = ["kcg"]
```

It is never prepopulated. `grayson library init` asks for the first admin
when a human is at the terminal (scripted runs write an empty list, which
means *no admins*), and `library link` or `grayson setup` offer the role to
whoever links a library that has none. After that, `grayson library admins
add|remove <id>` changes it: an admin's action, at an interactive terminal,
landing as its own commit — or a reviewed edit to the file. There is no MCP
tool for any of this, and the console shows the list read-only.

**Be clear about what this is.** Identity in grayson is declared, not
authenticated — `GRAYSON_USER_ID` overrides whatever was set, and a library
is a git clone anyone with write access can edit, `library.toml` included.
The author and admin checks are guard rails against the accidental and the
casual path, in the same class as the interactive-terminal check on user-only
actions: they keep grayson's own surfaces honest and the history attributable.
The lock, if you want one, is the git host: a `CODEOWNERS` line for
`library.toml` with *require review from code owners* means even an admin
changes the list through a reviewed pull request, and branch protection on
`records/` does the same for removals. `grayson library status` and
`library doctor` report the admins and the last commit that touched
`library.toml`, so a change nobody expected shows up rather than sitting
quietly.

## Reports: profiles in, whole investigations out

Report facts render deterministically from the session record and are not
configurable. *Presentation* is a **report profile** — a small YAML in
`reports/` (scaffolded with a commented `default.yaml`): section order and
inclusion, `engineering` vs `stakeholder` audience, header/footer. Pick one
with `grayson session report --profile <name>`. That is all `reports/`
holds: profiles, not reports.

When a session closes, its full report publishes into `records/<sid>/`
(`report.md` + searchable `report.json`), so `records search` returns whole
investigations — the agent's cited narrative included — not just their
findings.

## Knowledge without the harness

A collaborator who never runs sessions can serve the library to their agent
read-only — no workspace, no Snowflake, no write tools registered:

```bash
grayson mcp serve --knowledge-only --library git@github.com:your-org/qa-library.git
```

Serves `knowledge_*`, `workflow_*`, `views_list`, `checks_*`, `records_*`,
and `library_info` (freshness and the admins list) over stdio. The same surface runs containerized for a
whole team — see the knowledge-appliance recipe in
[DEPLOYMENT.md](DEPLOYMENT.md).
