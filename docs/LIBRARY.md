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
| `knowledge/` | One document per table (`<db>/<schema>/<table>.md`): descriptors, dated definition observations, facts with provenance and standing, retired questions, resolutions; `glossary.md` | agents propose, humans confirm; lifecycle actions under the knowledge policy |
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

## Standing, pruning, and the knowledge policy

A library that only ever grows misleads twice over: every fact on a busy
table lands in every briefing, and a fact recorded in March about a model
that changed in June reads exactly like one recorded yesterday. grayson
answers both without deleting anything.

### Status and standing are two axes

A fact's **status** says who vouches for it (`proposed`, `data_inferred`,
`user_confirmed`). Its **standing** says whether what it rests on still
holds:

| Standing | Meaning | Set by |
|---|---|---|
| `current` | nothing it rests on has changed | the rules, on every read |
| `unverified` | something it rests on changed and nobody has looked: a definition it was recorded against has a new hash, a `knowledge verify` re-run disagreed with the record, or a proposed fact sat unconfirmed past the horizon | the rules; `verify` |
| `stale` | something it rests on is gone: a column it names was dropped (per the last sync, or the DESCRIBE at session start), or the record it came from was removed or superseded | the rules |
| `retired` | an actor retired it with a reason, or a confirmed successor superseded it | a person, a permitted agent, a confirm |

The axes are orthogonal: a user-confirmed fact can be stale. Standing derives
from **anchors** — what would falsify the fact — recorded at write time from
what the store can already see: the recorded column names the text mentions,
the hash of every definition entry on the doc, and, for a verified fix, the
published record it came from. Everything is computed from library files, so
the knowledge-only server serves it too; `retired` is the one sticky state,
cleared only by a restore. `library reconcile` also writes the computed
standing onto the doc so a hand reader and a git diff see it; where the file
and the rules disagree, the rules win.

A fact's fields for all this are additive (`anchors`, `standing`,
`standing_reason`, `supersedes`, `superseded_by`, `retired_by`, …) and are
written only when set, so a doc nobody has touched with a lifecycle action
diffs as before, and an older grayson round-trips them.

### The briefing

Session start no longer dumps the doc. Each target table briefs as:

- facts ranked by standing (current before unverified), then status
  (confirmed, data-inferred, proposed), then newest first, each carrying its
  `role` under the policy's **trust** — `knowledge` or `hypothesis` — so the
  agent is told which is which;
- capped per table (`briefing_cap`, default 12), the count of the rest stated
  and `knowledge_show` named to fetch them;
- stale and retired facts hidden and counted, never silently dropped;
- verified-fix facts folded into one line per table pointing at
  `records_search`, since the record already holds the full story;
- **contested** pairs beside the facts: a fact proposing to supersede another
  that nobody has confirmed, two answers to one open question, or (weakest,
  console and doctor only) two facts on one column with mixed provenance;
- recent **agent actions** — what agents retired, restored, dismissed or
  resolved within the policy's window, with the reason.

`knowledge_briefing.<table>` carries the counts and pairs; `knowledge.<table>`
stays a list of facts, now ranked, capped and annotated.

### Lifecycle actions, and who may take them

| Action | What it does | Must cite | `propose` | `curate` | `autonomous` |
|---|---|---|---|---|---|
| `retire` | out of briefings, kept with who and why; restorable | evidence (agent), a reason (person) | user | agent | agent |
| `supersede` | record a corrected fact naming the one it replaces; always recorded, the replacement **executes** now or waits for the human's confirm | evidence (agent) | user | agent | agent |
| `dismiss_question` | retire an open question as moot, kept under `retired_questions` | a reason | user | agent | agent |
| `reconcile` | the rules pass, as one commit (a dry run is always allowed) | — | user | agent | agent |
| `resolve_contested` | judge two contested facts compatible | nothing: judgment | user | user | agent |
| `restore` | back to current; anchors re-baselined on the doc as it stands | nothing: judgment | user | user | agent |

The **evidence rule** is code, not policy: an agent retiring or superseding
a fact must cite what falsified it (a query id, an intervention, a drift
observation, a record), and with `--session` query ids must have executed
there. A permitted agent acts alone, but never on an assertion. The
**confirmed label** stays outside the policy too: `user_confirmed` records
that a human vouched, so no actor but a human sets it; authority for agent
facts is the trust setting instead.

An agent's supersession executes only when the new fact ranks as knowledge
under trust, or at least as high as the one it replaces: a proposed fact
does not displace a confirmed one by itself, however permissive the preset —
that pair reads as contested until the human confirms the correction, which
executes it (the same shape as a finding's supersession inside accept).

```bash
grayson knowledge retire DB.S.T amounts_are_net -e q_0007 [--session <sid>]
grayson knowledge supersede DB.S.T amounts_are_net --fact "amounts are gross of tax since 2026-06" -e q_0007
grayson knowledge dismiss DB.S.T -q "is AMOUNT signed" -r "AMOUNT was dropped"
grayson knowledge resolve DB.S.T fact_a fact_b --note "different date ranges"
grayson knowledge restore DB.S.T [fact_id]        # no id: re-anchor every live fact
grayson knowledge verify DB.S.T --session <sid>   # re-run verified fixes' after-queries
grayson knowledge policy                          # what the agent may do here
```

MCP: `knowledge_retire`, `knowledge_supersede`, `knowledge_dismiss_question`,
`knowledge_resolve`, `knowledge_restore`, `knowledge_verify`,
`knowledge_reconcile`, `knowledge_policy`. A refusal names the setting that
withheld the action. At the CLI a person at a terminal is `user` and always
allowed; a non-interactive shell-out is `agent` and policy-gated, and
`--by user` from a shell-out is refused as everywhere else.

### What replaces pre-approval

Under a permissive preset the human moves from approving before to auditing
after. Three things make that workable:

- **Provenance.** Every action stamps who and when; a retired fact carries
  its reason beside the original.
- **Reversibility.** Each lifecycle action lands as its own library commit
  on the doc's path alone, with a `Grayson-Via: mcp-agent | cli-agent |
  reconcile` trailer, so one action is one `git revert`. With auto-push off
  they sit in your clone until `grayson library push`.
- **Visibility.** The briefing, the table page, the Knowledge tab's tiles and
  `library doctor` list agent actions within the window
  (`agent_window_days`, default 30). An over-eager agent shows up as a
  number, not as silently missing knowledge.

### The policy: presets, where it lives, how two sides combine

```toml
# grayson.toml — the workspace's own
[knowledge]
policy = "curate"          # propose | curate | autonomous
trust = "data_inferred"    # lowest status a briefing ranks as knowledge
proposed_horizon_days = 90 # an unconfirmed proposed fact reads unverified after this
briefing_cap = 12

[knowledge.agent]          # per-action overrides
restore = "agent"
```

```toml
# library.toml — the team's, admin-owned, travelling with the repo
[library]
admins = ["kcg"]
knowledge_policy = "curate"
knowledge_agent_denied = ["restore"]   # withheld from agents whatever the preset says
```

Blast radius decides where a policy lives. In solo mode the workspace's is
the policy. With a team library linked, the **effective** policy is the meet
of the two: an action is the agent's only when both sides say so, the
stricter trust wins, and a library that has not chosen counts as `propose`
until an admin widens it — a workspace narrows the team's policy, never
widens it. `grayson library policy show` prints the effective policy and
which side withheld each action; `grayson library policy set --preset curate
[--deny restore] [--allow retire] [--trust proposed]` changes the library's
(an admin's commit, interactive terminal only) or, in solo mode, the
workspace's. `grayson setup` asks once. The console's Settings page shows
the effective policy; agents read it (`knowledge_policy`) and never change it.

### Reconcile: the unattended pass

```bash
grayson library reconcile [--dry-run]                 # from a workspace
grayson library reconcile --library ./qa-library --push   # from CI, on a checkout
```

Rules only: materialize standing onto every doc, fold duplicate open
questions, retire questions that name a dropped column, and report
`needs_human` — contested pairs, unverified and stale facts — as the queue
for the console or a permitted agent. It never retires a fact by rule and
never touches status. The one write it makes beyond the rules is executing a
supersession a human already confirmed but nothing executed (a confirm done
by an older grayson, or by hand): the decision was the human's, the pass
records it, and read time treats such a pair as done even before the pass
runs. Clean tree required; one commit with a `Grayson-Via: reconcile`
trailer. `library doctor` runs the same pass dry as its `standing` section,
which never fails the doctor: standing is a queue, not a fault. Because it
needs no warehouse it runs on a schedule from the library repo itself:

```yaml
# .github/workflows/reconcile.yml in the library repo
on:
  schedule: [{cron: "0 6 * * 1"}]
jobs:
  reconcile:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pipx install git+https://github.com/Kcarreras/grayson
      - run: git config user.name grayson-reconcile && git config user.email bot@example.com
      - run: grayson library reconcile --library . --push
```

Put the run behind a pull request instead of `--push` when the team wants
to review what the rules materialized.

### Upgrading a library written before standing existed

Nothing migrates: the format stays at version one and an older grayson
round-trips every new field. But anchors are recorded at write time, so a
fact written before this release carries none — a dropped column or a
changed model cannot touch it, an old verified-fix fact neither folds in
briefings nor re-verifies, and only the proposed-fact horizon applies. The
upgrade step is the reconcile pass with `--anchor-missing`, once:

```bash
grayson library doctor                            # standing.unanchored_facts says how many
grayson library reconcile --dry-run --anchor-missing
grayson library reconcile --anchor-missing        # one commit; review it
```

It anchors every live fact that has none to the doc as it stands — its
mentioned columns, the definition hashes on record, and for a verified-fix
fact the record its id encodes, when that record is still in the library —
stamped `anchored_by: reconcile` so a reader knows the fact was *baselined*
then, not recorded then. That is the honest claim about a fact nobody
re-verified: flag it if these change from here on. Facts on a doc with no
columns and no definitions have nothing to anchor to and are counted as
`unanchorable`; they age by the horizon rule alone.

Two other things change on upgrade. Proposed facts older than the horizon
read as unverified at once — confirm the ones you stand behind, or set
`proposed_horizon_days = 0` to switch the rule off. And a solo workspace
whose grayson.toml has no `[knowledge]` section reads as `curate`, so an
agent may retire and supersede with evidence from the first session;
`grayson library policy set --preset propose` first if you want to opt in
later. A team library without a policy reads as `propose` until an admin
widens it. Re-run `grayson harness init` afterwards: the protocol files
agents read are generated once and say nothing about standing until then.

### Re-verification against the warehouse

The one anchor with a query cost. A verified fix's record carries the fix's
after-query; `knowledge verify <table> --session <sid>` (MCP
`knowledge_verify`) re-runs it through the session — guarded, audited,
budgeted — and compares the row count with the record's. A match
re-baselines the fact (`verified_at`); a mismatch marks it unverified with
both counts in its reason until the next verify agrees or someone restores
it. The verdict is code comparing counts, so it is not policy-gated; it
spends budget, so it is opt-in per session.

### Records read as history when they are

Records are search-time, not briefing-time, so they need collapsing rather
than pruning. `records search` now carries a `state` on every row: a finding
with a passing proposal in the same session is `resolved` (with
`resolved_by`), a superseded one `superseded`, and both rank below `current`.
A finding may supersede a **published** finding from another session by
citing it as `<session_id>/<fid>`; on accept, the library copy is marked
`superseded_by` (first wins there too).

### What grayson does not do

It never judges semantics. Two free-text facts that contradict without a
shared column, question, or supersession claim are invisible to it — the
briefing ranking and the supersede path are the mitigation, and an agent that
reads both is expected to record the correction with evidence or say in its
findings that both hold. That is the ceiling of a tool that runs no LLM, and
it is deliberate.

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
