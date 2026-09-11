# Upgrading an existing installation

An upgrade has three independent parts: the installed package, the instructions
your harness reads, and your team's library. Update each deliberately. You do
not need to initialize a new workspace, reseed a sandbox, or rebuild a library.

## Existing Grayson workspaces

If Cursor is missing `project_schema` or `project_draft`, re-running harness init
will not add tools to a running MCP process. Update the installed package using
step 1, refresh existing instructions with `grayson harness update cursor --apply`,
then restart Cursor and the console. A remote HTTP MCP server must be updated and
restarted on its host.

To investigate an installation mismatch, run `grayson project schema` and
`grayson harness mcp status --harness cursor` in the affected workspace. Inspect
the actual Grayson entry in `.cursor/mcp.json` (or the global MCP configuration):
an absolute executable path, another virtual environment, or a remote URL can
point to a different installation from the `grayson` command in your terminal.
Harness status checks the project configuration; it does not inspect the running
server or Cursor's cached tool list. Updating instructions does not change that
entry. Keep your existing workspace and sessions.

1. Stop running investigations and restart the console and harness MCP servers
   after updating the package. For an unpinned uv tool installation, run
   `grayson upgrade`. For a pinned tag, follow the reinstall instructions in
   [Deployment](DEPLOYMENT.md#pinning-a-version-for-a-team-install). A source checkout uses
   `git pull --ff-only` followed by `uv sync --frozen`.
2. In the workspace root, run `grayson harness status`. It checks instructions
   for Cursor, Claude Code, Codex, and Copilot against the installed package.
   An uninstalled harness needs no action.
3. Preview each installed harness's changes, then apply the ones you reviewed:

   ```bash
   grayson harness update claude-code
   grayson harness update claude-code --apply
   ```

   Substitute `cursor`, `codex`, or `copilot` as appropriate. Use `--path` to
   name another repository root. Preview emits file diffs and writes nothing.
   Applying refreshes the protocol and workflow-author instructions, keeping
   text outside managed sections and the original choice to omit the MCP note.
   Running it again is a no-op when instructions are current.

   Changed files are backed up as exact bytes under
   `.grayson/harness-backups/update-*/`, and the result names that directory.
   `manifest.json` lists changed and newly created files. To undo, stop the
   harness, restore the backed-up files to the same relative paths, and remove
   only the newly created files listed in the manifest. An IO failure attempts
   this restoration automatically and reports any restoration failure.

   Damaged, duplicate, or nested managed markers stop the update before files
   are written. Repair the markers and preview again. Keep personal instructions
   outside managed sections; edits inside them appear in the diff and backup,
   but the installed package owns their replacement.

   Updating instructions does **not** change MCP server entries or permissions.
   In particular, a custom remote MCP URL, environment, and unrelated servers
   remain as configured. Inspect those separately with `harness mcp status` and
   `harness guard status`; apply changes only when appropriate for your setup.
4. Run `grayson library doctor` and `grayson library migrate --dry-run`.
   Reads never migrate the knowledge format. The preview can run without a
   terminal and returns a nonzero exit status if it finds an unreadable or
   newer-format document.
5. If a migration is needed, commit or stash library changes, then run
   `grayson library migrate` in your terminal. A Git library gets a labeled
   commit containing only migrated documents. A failed status check stops
   before writing; a failed commit is reported and is never followed by a push.
   For a library without Git, back it up first: there is no automatic rollback
   commit. Existing `auto_push` behavior still applies after a successful commit.

Adding the format-1 stamp preserves comments, unknown fields, fact metadata,
prose, and line endings. It does not normalize the document or assign new dates
to old facts. Current documents are validated but not rewritten; newer formats
are refused for rewrite. Broken documents are reported while other documents
can still migrate. Review the report before considering the upgrade complete.

The optional one-time anchoring pass for libraries that predate fact standing
is described in [Library](LIBRARY.md). It is separate from format migration and
does not need repeating on each package upgrade.

Relative `[library] path` values resolve beside `grayson.toml`, so entering a
workspace subdirectory or starting a server elsewhere uses the same library.
Absolute paths and `~` paths keep their existing meaning. If an old relative
path relied on a particular shell directory, change it to the intended
config-relative or absolute path before resuming work.

## Repairing the Cursor session-state guard after an upgrade

The Cursor guard is a generated workspace script. Reinstalling the package
alone does not replace it. After installing the updated package, run this from
your workspace root in your own terminal, then restart Cursor and the console:

```bash
grayson harness guard apply --harness cursor
```

This refreshes Grayson's hook while preserving unrelated hook registrations.
Do not remove the guard or delete session databases to recover access.

Some SQLite builds fail with `unable to open database file` when reading a WAL
database through a read-only connection, even though a normal connection can
read it. The updated guard first tries read-only access, then retries only
`SQLITE_CANTOPEN` failures using an existing-database connection with SQL writes
disabled (`query_only`). SQLite can manage journal sidecars; session records and
journal mode are not rewritten by the guard. It reads current WAL contents and
does not use immutable snapshots. Missing or damaged state still blocks tools,
and the denial now includes the failing database path and underlying error.

No session migration or manual journal-mode change is needed.

If calls work until a session starts, check for Cursor's qualified server name
(for example `project-0-sql-qa-workspace-grayson`). The guard accepts the bare
`grayson` identity and `project-<index>-<workspace-folder>-grayson` when that
workspace's `.cursor/mcp.json` registers `grayson` and the indexed entry in
Cursor's `workspace_roots` resolves to that workspace. Other workspace names,
unrelated servers, and ambiguous registrations remain blocked. Refresh the
guard with the same command above after installing this fix. Keep the configured
server key as `grayson`; do not rename it to Cursor's generated identity.

Cursor can also dispatch MCP through `CallDynamicTool`, with the target in
`tool_input.namespace`. The guard validates that route during both `preToolUse`
and `beforeMCPExecution`, including when `mcp_server_name` is absent. A conflicting
server identity is denied. `GetDynamicTools` discovery is allowed, but discovering
a tool does not permit calling another server. Denials report hook, tool and
routing identity so unknown event shapes can be diagnosed without exposing SQL
or nested tool arguments. Do not replace these checks with a substring match
or an unconditional exemption for dynamic tool calls.

Direct calls labeled `MCP:<tool>` (such as `MCP:knowledge_sync`) are recognized
at `preToolUse` too. That label identifies the tool kind, not a trusted server;
the separate `beforeMCPExecution` identity check still applies. A missing server
name on a direct call is denied rather than inferred from arbitrary tool arguments.

## Adopting regression checks

[Regression checks](REGRESSIONS.md) add optional files under the existing
library's `checks/` directory. There is no configuration, session database, or
knowledge migration. Updating the package and refreshing installed harness
instructions with the steps above enables discovery, proposal, and replay.
Restart MCP servers so the new tools appear.

Existing external JSON results, knowledge, workflows, custom harness text,
and connection settings remain usable. Definitions are YAML in
`checks/regressions/`; older Grayson clients ignore them and still read native
JSON replay results. Those older clients cannot run checks or interpret
retirement, so they may continue showing a retired check's last result until
they upgrade. Only current clients should manage regression definitions.

Upgrading never creates or activates checks automatically. Propose checks from
existing executed queries as useful; a person reviews each before activation.
Source sessions and cached rows stay local. Teammates can replay a shared check
without copying the original session.

## Adopting evidence contracts and comparisons

[Success criteria, change-impact plans, and release comparisons](ASSURANCE.md)
use optional format-1 additions. No session or library migration is required.
Refresh harness instructions and restart MCP/console to enable the new tools.
Existing analytical fix verification remains available on proposals without
success criteria. Current clients require human approval of contracted fixes
and compute their verdict from fresh results; older clients do not enforce
that contract, so use updated clients for sessions adopting these features.

New libraries include `comparisons` in the default report profile. Existing
profiles keep their explicit section lists: add `comparisons` to `sections`
in the library's `reports/default.yaml` (or another selected profile) to include
comparison evidence in Markdown reports. JSON reports include it regardless.

## SeekQL rename

Older releases used `seekql.toml`, `.seekql/`, and the `seekql` command. Grayson
now detects the old workspace and refuses to initialize over it. The repository
directory itself can keep its old name.

Perform this once, with all old CLI, console, and MCP processes stopped:

1. Back up the entire workspace, including hidden `.seekql/` state and its
   SQLite/WAL files. Back up the linked library separately if it lives elsewhere.
2. Rename `seekql.toml` to `grayson.toml` and `.seekql` to `.grayson` within the
   workspace. Keep all contents. If either destination already exists, stop and
   inspect the two workspaces; do not merge or overwrite their state directories.
   Add `.grayson/` to `.gitignore` and retain `.seekql/` for any old backups.
3. Install `grayson-sql` using the current repository URL in the README. Preserve
   connection settings and the library path in the renamed configuration.
   Check any custom paths or scripts that explicitly contain `.seekql` or invoke
   `seekql`. The warehouse credentials and linked library need no rename.
4. Run `grayson harness init <your-harness>` to refresh instructions. Well-formed
   `seekql` managed sections in CLAUDE.md, AGENTS.md, and Copilot instructions
   are replaced in place, with backups. For Cursor, archive the old
   `.cursor/rules/seekql.mdc` outside the rules directory after reviewing the
   newly generated `grayson.mdc`; do the same for any old SeekQL authoring skill.
5. Inspect old MCP and permission configuration. Replace the old `seekql` server
   registration with `grayson mcp serve`, retaining custom arguments, environment,
   transport, and connection settings where applicable. For remote deployments,
   update the server installation separately. Apply the Grayson guard for your
   harness and remove obsolete SeekQL hook registrations only after reviewing
   them. User-global configuration remains a manual change.
6. Preserve your author identity: `grayson user set <existing-user-id>` records
   the same ID under the new user configuration directory. Existing fact and
   record authors are unchanged. Relocated warehouses under
   `~/.seekql/sandboxes` (or `SEEKQL_SANDBOX_DIR`) are read in place when no
   current warehouse or explicit `GRAYSON_SANDBOX_DIR` overrides them. The old
   `_seekql_meta` catalog remains readable without rewriting the warehouse.
   Keep the workspace directory path unchanged because it keys the warehouse
   filename. Verify with `grayson doctor`; do not reseed it.
7. Run `grayson session list`, `grayson library doctor`, and the migration preview
   above. Confirm that sessions and library assets are present before restarting
   the harness.

No library format change is required just because the product was renamed.
