# The team library

Knowledge compounds through an ordinary git repo — no server. Table semantics
with provenance, QA views, workflow templates, external check results, and the
team's published records (accepted findings, verified fixes) are all plain,
merge-friendly files. Solo mode keeps them in the workspace; team mode shares
them through the repo.

## Linking a library

```bash
# one-time, per team: create an EMPTY repo on your git host, then
grayson library link git@github.com:your-org/qa-library.git --auto-push
```

grayson clones the repo, scaffolds the structure (`knowledge/`, `views/`,
`workflows/`, `checks/`, `records/`), pushes the first commit, and points the
workspace at the clone. Collaborators run the same command and get the shared
library. With `--auto-push`, every library write is committed and pushed
automatically; otherwise `grayson library push` batches. `grayson library
status|pull` keep the clone honest; `grayson library extract` splits a solo
workspace's assets out into a new shareable repo. Git auth stays entirely
inside git — grayson never handles those credentials.

## Knowledge, with provenance

Facts about tables carry provenance — `proposed` / `data_inferred` /
`user_confirmed` — and agents can never mark a fact user-confirmed themselves:
confirmation is a human action in the console or
`grayson knowledge confirm`. Structured base descriptors (grain, columns,
relationships, freshness, owners) live beside free-form facts, and each
table's completeness report shows what is still undescribed.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/knowledge_dark.png">
  <img src="img/knowledge_light.png" alt="The Knowledge tab: table completeness cards and the relationship canvas of the whole library">
</picture>

The console's Knowledge tab includes a relationship canvas of the whole
library (Cytoscape + ELK, vendored — no CDN); each table page shows its
completeness report, facts, and the external checks that cover it.

## Format stability

The library is the team's compounding asset, so its file formats are a public
interface with a compatibility contract — the code adapts to the files, never
the reverse:

- **The library must remain useful with zero grayson.** Knowledge docs are
  markdown with YAML frontmatter, records are small JSON files: readable by a
  human or any agent from the raw repo alone. No change may trade this away.
- **Changes within a format version are additive-only.** New fields may
  appear; existing fields never change meaning or disappear. A rename means
  writing both names and reading either for a deprecation window.
- **Unknown fields round-trip.** A grayson that does not recognize a
  frontmatter key or fact field preserves it verbatim on rewrite, so mixed
  versions can share one library without a rewrite by an older install
  stripping what a newer one recorded.
- **Docs are stamped** (`format: N` in frontmatter; unstamped docs predate
  stamping and are format 1). A reader may load a newer doc best-effort, but
  it **refuses to rewrite** one — visible refusal with an upgrade message,
  never silent loss.
- **Breaking changes ship only alongside `grayson library migrate`**, which
  runs on a clean git tree and lands the rewrite as one labeled commit —
  reviewable, revertible, run deliberately by a human. Migration never
  happens implicitly on read; git history is the rollback mechanism.

```bash
grayson library doctor    # read-only health pass: knowledge format, workflow
                          # lint, records parse, git freshness; non-zero exit
                          # when something needs fixing
grayson library migrate   # idempotent; refuses on a dirty tree
```

Hand edits and merges are first-class ways to write the library, so drift is
normal — `doctor` is how it surfaces on demand instead of accumulating: the
doc that no longer parses, the fact id a merge duplicated, the doc a newer
grayson wrote that this version can read but not rewrite.

## Attribution: user ids

Set a short per-user id once after install:

```bash
grayson user set kcg     # stored per-user in ~/.grayson/config.toml
```

Every fact carries it (`author`) alongside the actor kind, and every library
commit message carries a `Grayson-User:` trailer (plus `Grayson-Via:
mcp-agent` for agent-surface writes), so shared history stays attributable
even from shared machines. `GRAYSON_USER_ID` overrides it per process.

## Records: sessions stay local, their output travels

Raw session state (query cache, live progress, interventions) never leaves the
workspace. At the human-approved moments — a finding **accepted**, a fix
verification recorded — the distilled record publishes into the library's
`records/` as small, author-stamped JSON. Rejected findings never publish;
accepting a superseding finding republishes the superseded one so the library
copy stops reading as current knowledge.

From any linked workspace, `grayson records search` then answers "how did
*anyone on the team* diagnose and fix something like this" — collaborators'
records merge into every search, show on the console's Records tab badged
`team`, and open from the published copy when the session isn't local.

## Knowledge without the harness

A collaborator who doesn't run sessions can still give their agent the team's
library, read-only — no workspace, no Snowflake, and no write tools registered
at all:

```bash
grayson mcp serve --knowledge-only --library git@github.com:your-org/qa-library.git
```

This clones (or pulls) the library and serves `knowledge_*`, `workflow_*`,
`views_list`, `checks_*`, `records_*`, and `library_info` over stdio. The same
surface runs containerized for a whole team — see the knowledge-appliance
recipe in [DEPLOYMENT.md](DEPLOYMENT.md).
