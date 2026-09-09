# Autonomous SQL projects

Project workflows develop a SQL deliverable against an approved brief. They use
the existing workflow catalog, fork/edit/review controls, library ownership, query
guard, evidence store, interventions and DDL proposal UI. `pipeline-development`
and `goal-analysis` are the two built-in project templates. Existing QA workflows
keep their current lifecycle and permissions.

## Start from a goal

Ask your agent to start a project workflow with explicit source tables and strict
scope. It should interview you about grain, population, business definitions,
exclusions, time windows, the intended result and operational limits. Scoped
discovery can happen before a brief is drafted. Once drafted, execution waits for
its approval. The agent reads `project_schema`, writes a complete brief with
`project_draft`, and directs you to the session's **Brief** view for approval.
Project workflows open directly into a project-specific session interface.
There is no separate project nested inside a QA session.

Review the definitions, baseline SQL, tolerances, source and output scope, join
contracts and approval level there. Approving binds the exact brief, connection
and workflow snapshot. Changing a brief invalidates its approval and starts a new
candidate history; earlier versions remain in the audit events. Template edits do
not alter existing projects. New execution fields reject unknown semantics.

CLI entry points:

```text
grayson session start --workflow pipeline-development --table DB.S.ORDERS --table DB.S.CUSTOMERS --strict-scope
grayson project schema
grayson project draft latest brief.yaml
grayson project status latest
grayson project approve latest REVISION CONTRACT_DIGEST
grayson project candidate latest candidate.yaml REVISION
grayson project verify latest REVISION
grayson project review latest review.yaml REVISION
grayson project finish latest REVISION
```

Approval commands require the human's interactive terminal. MCP deliberately has
no brief-approval, candidate-approval, resume or cancel tool. Revisions are
optimistic concurrency tokens; a stale write must refresh the brief and retry.

## Permission presets

| Decision | Guided | Milestones | Bounded |
|---|---|---|---|
| Approve initial/revised brief | Human | Human | Human |
| Approve each candidate before verification | Human | Automatic | Automatic |
| Read scoped data, execute checks, diagnose and repair | Automatic | Automatic | Automatic |
| Accept verified work/deployment | Human | Human | Automatic |
| Interpret explicitly untestable semantic rules | Human | Human | Human |
| Expand scope, raise budget, change success criteria | New human brief approval | Same | Same |
| Approve and execute DDL/DML | Human | Human | Human |

An agent completion is labelled **machine verified; not human accepted**. Human
acceptance and agent review are separate facts. You can pause or cancel in the
console, or use `project control SID pause|resume|cancel REASON REVISION`.
Resuming a paused review or deployment restores its previous phase. Interrupted
verification and genuinely blocked runs return to revision instead.

Use the console's **Change approval level**, or `project approval SID LEVEL
REVISION`, to change human review points without discarding valid work. This is
human-only, remains subject to team/workspace ceilings, and does not change
warehouse permissions or acceptance criteria. Tightening to Guided returns an
unverified, unapproved candidate to its human review gate, including when a
workspace or library ceiling changes. Existing evidence and active verifiers
are preserved; paused sessions wait for resume.

Per-workflow project defaults edit on the workflow page using the ordinary
review-before-save flow. Per-run settings are part of the brief. Workspace policy
can cap autonomy in `grayson.toml`:

```toml
[projects]
max_approval = "milestones"
```

A linked library can set `project_max_approval = "guided" | "milestones" |
"bounded"` in its `[library]` settings. Team libraries default to milestones;
solo workspaces permit bounded. The most restrictive level wins, and restrictive
policy changes take effect on subsequent actions. Policy files require the same
external ownership/access controls as the existing library policy.

## Candidate graphs and generated checks

A candidate consists of ordered named nodes. A query node reads one approved
table or earlier node. It can compute, filter, aggregate or use windows, but cannot
contain nested SELECTs, joins, UNION, sampling or limits. A join node references
an approved `joins` entry by ID and projects named expressions over `l` and `r`.
All nodes must contribute to the output. Multi-stage pipelines are expressed as
multiple nodes; hidden joins in scalar subqueries or comma syntax are refused.

Join contracts name input nodes, key tuples, left/inner join type, business
meaning, expected cardinality, maximum matches per left row and acceptable
unmatched input counts. Generated probes inspect both input relations:

- Null join keys are failures. One-to-one requires uniqueness on both sides;
  many-to-one on the right; one-to-many on the left and an explicit right bound.
- Unmatched left rows are counted before the join so inner joins cannot hide loss.
  Right-side coverage can also be constrained.
- Grain checks test key uniqueness and nulls.
- Population checks compare identities in both directions against independent,
  frozen baseline SQL. Equal row counts do not establish equal populations.
- Measure checks compare sums by the approved grouping, null counts, null totals
  and missing groups. Tolerance is absolute allowance plus a percentage of the
  expected magnitude. Choose groupings that expose compensating errors; SQL cannot
  prove an arbitrary business choice is correct merely because totals agree.
- Value checks compare expected categorical or numeric values per key and reject
  ambiguous duplicate-key baselines. This catches attaching the wrong region or
  status even when counts and revenue match.
- Additional assertions use frozen SQL containing `{{relation}}` and the existing
  scalar/no-rows expectation grammar. They must read the specified candidate
  relation. They remain only as sound as the expectation the human approved.

Every semantic definition maps to acceptance check IDs through `semantic_checks`,
or is listed in `human_semantic_review`. The latter cannot be silently certified
by an autonomous agent. Every project needs output grain and population checks,
and a measure check or an explicit, reviewable measure exemption. Checks may also
target named intermediate nodes. Put conservation checks around material lossy
steps where final-output checks could conceal their effects.

Verification reruns all required probes through the common guard and audit path.
Errors, missing cache data, partial scalar results and interrupted checks are
**unproven**, never passes. All versions are retained. A repair names failed check
IDs and a diagnosis; changing the candidate clears its verification, review,
checkpoint validity and deployment binding. Descriptions are excluded from the
candidate fingerprint; changing only narrative text preserves existing evidence
and approvals. Re-submitting unchanged executable
content cannot manufacture a new iteration. Repeated failures without additional
passing checks stop for human input. Query and iteration limits are enforced;
active-phase elapsed time excludes approval/deployment waits and explicit pauses.
These are query/time budgets, not estimates or hard limits on Snowflake credits.
The supervisor also has a persistent `policy.max_actions` budget (default 200),
so stopping/restarting it cannot create an unlimited loop of otherwise valid
planning or review actions. `project diagnose SID CHECK_ID` obtains bounded
violating-row examples from a failing generated probe; those samples never turn
a failed acceptance check into a pass.

After passing checks, a critical review answers the brief's fixed questions with
current evidence, unresolved issues and limitations. The engine cannot prove the
reviewer's prose, causal interpretation, or that a human chose the correct rules.
Review is an additional challenge, not a substitute for executable checks. The
supervisor requests review as a distinct reasoning step; it does not claim an
independent model or separate reviewer was used.

## Run and resume

The MCP tools support any connected agent without a dedicated runtime. A compact
project brief survives restarts and context compaction. A provider-neutral runner
is also available:

```text
grayson project run SID provider.json --watch
```

`provider.json` is a JSON array of executable arguments, e.g.
`["python", "my_agent_adapter.py"]`. The adapter receives one JSON object on stdin
with instructions, action schemas, current brief and previous result; it returns
`{"action":"candidate","payload":{...}}` (or another advertised action) on
stdout. Connect that adapter to your chosen agent API/runtime. Grayson does not
bundle a model account or invoke an LLM by itself. The command runs without shell
interpolation, with a time limit; nonzero exits and malformed actions feed back
for correction and eventually block after repeated errors.

The supervisor runs verification automatically after candidates, tracks a durable
runner lease, rejects competing runners, and checks revisions after reasoning.
`--watch` stays attached to console decisions, reports state changes and resumes
after approval or reported deployment. Without it, the command returns at human
boundaries and can be invoked again. Process restarts retain all project state;
an abandoned active lease can be cleared by human pause/resume. The watcher is an
attached process, not a system-installed service or an external scheduler.
Embedded callers may pass `executor=` to `drive` or `watch`; the supplied executor
is retained for discovery, candidate checks, diagnostics and deployment verification.

Provider processes are **not sandboxed by this runner**. Retain the documented
read-only warehouse role and credential isolation. The common guarded query path
enforces project scope and budgets even when an agent queries outside the runner;
unrestricted direct credential access can bypass that path, as described in
[SECURITY.md](SECURITY.md#warehouse-access).

## Deployment and evidence

`project deployment` prepares exact CREATE VIEW/TABLE SQL for the approved output
destination using the existing DDL proposal UI. It intentionally does not generate
OR REPLACE. The person approves and executes it, then records application.
Approving a stale candidate's package is refused. No project tool runs DDL.
Project source tables and deployment targets must use fully qualified, unquoted
`DB.SCHEMA.OBJECT` names so connection defaults cannot change the approved objects.
The current scope registry canonicalizes names to uppercase. Quoted source names
are rejected during brief validation, including baseline SQL, and in candidate SQL;
quoted deployment targets are rejected before approval.
Each name component follows Snowflake's ASCII bare-identifier grammar and
255-character limit; validation runs before uppercase normalization.

`project deployment-check` reads the actual destination, rerunning acceptance
queries in a separate audit session containing the approved sources and target.
It also compares every output column against the candidate in both directions,
so semantic checks on intermediate relations cannot mask changed deployed values.
The output grain checks enforce uniqueness; incompatible schemas or unsupported
value comparisons leave verification unproven.
An audit-session setup error releases the deployment-check lease and restores the
prior phase so the check can be retried. Failed attempts never retain a passing report.
Those queries consume the original project's budget. The source session's scope
is not temporarily widened. `project accept-deployment` follows the configured
acceptance policy after fresh passing results. Candidate correctness and deployed
correctness are separate results. The warehouse application marker is a report of
external execution, not proof of success or of the exact deployed definition.

This release develops SELECT-based pipelines and CREATE VIEW/TABLE packages.
Incremental MERGE behaviour, orchestration schedules, transactional rollback,
non-equi/lateral joins and cross-environment deployment are not automatically
certified by these checks. Use the existing hands-on QA workflow for those cases
or make the missing integration checks explicit before relying on the pipeline.
Evidence freshness is time-bounded; separate queries are not a transactionally
consistent warehouse snapshot. Use pinned source versions/time-travel or stable
batch boundaries when concurrent data changes could invalidate comparisons.
The approved `evidence_minutes` window starts with the first probe, so finishing
later probes does not refresh older observations. If a suite outlasts that window,
its passing results cannot authorize completion. Choose a sufficient window before
approving the brief, or revise it through human approval; the runner never extends it.

The session opens on **Build**: the current proposal, a clickable source-to-output
diagram, the latest change and the next decision. Deployment SQL can be reviewed,
approved, downloaded and reported applied here, without entering a findings/fixes
screen. **Checks** shows failures first and groups passing evidence. **Brief** holds
definitions, acceptance criteria and run controls. **Query log** uses the shared
query inspection tools. **History** compares SQL between attempts and shows which
failed checks were resolved. Available charts appear as output previews in Build.
Old `/session/SID/project` links redirect to the same session workspace.
Download JSON from History; session reports also include project verdicts and attribution.

## Reuse and bounded revalidation

Revalidation children are internal until attached to their parent's approved
request. Incomplete or failed setup attempts stay out of the dashboard and cannot
query; successfully attached runs remain visible. Retrying an interrupted request
preserves its original finite allowance.

`project recipe SID NAME` returns a workflow draft containing a verified method
and a clearly labelled historical example. Save it through ordinary workflow
authoring; examples never transfer approval or scope to new projects.
`project promote SID CRITERION_ID CHECK_ID` proposes a passed criterion in the
regression library, preferring deployed evidence. Activation remains human-owned.

The brief may explicitly grant `policy.max_revalidations` (default zero, maximum
100). `project revalidate SID REQUEST_ID REVISION` consumes one of those grants,
reruns the unchanged checks in a separate audit session, and keeps the original
result historical. Deployed projects test the deployed target. Retries with the
same request ID return the same run. Replays cannot change the brief/candidate or
grant further replays. If audit-session creation is interrupted, retry the same
request ID: ordinary creation errors release its lease immediately, and a process
exit leaves a creation lease that expires after 60 seconds. Recovery retains the
original allowance; an unattached audit session cannot execute queries.
If the child was already attached, retry resumes that same run. Fresh completed
evidence is concluded without rerunning queries; an active verifier or an explicit
pause is left alone. Completed and failed runs remain idempotent.
Each replay has the approved per-run query/time budget;
the finite replay count bounds total authorised work. Failed replays block with
their evidence for investigation rather than quietly repairing production.

An external change detector or scheduler may invoke this command with a stable
event ID. This does not install a scheduler or subscribe to warehouse change
events automatically.

## Runnable local example

```text
uv run python docs/examples/autonomous-project/demo.py --output NEW_DIRECTORY --serve
```

The demonstration uses a scripted provider and local SQLite data. Its first
candidate causes fanout, wrong region values and inflated amounts. The supervisor
reads the failing probes, applies the approved current-customer rule, verifies the
repair, and stops at an unapproved DDL package. The console URL and an exported
evidence report are printed. It uses no live warehouse or model account and makes
no claim about LLM quality; the same supervisor supports an actual model adapter.
