# Evidence contracts, change impact, and release comparisons

All three workflows are optional. They share deterministic engines across CLI,
MCP, and the console. Grayson never applies warehouse changes or calls an LLM.
Your harness supplies the investigation and interpretation; the engine evaluates
the declared rules against executed query evidence.

## 1. Approve success criteria before a fix

On a pending fix, choose **Define success criteria** in the session console.
Select an executed baseline query for each criterion and choose a numeric rule
or **No violating rows**. Review the SQL, baseline observation, and exact bounds,
then **Approve fix and these criteria**. Approval binds the fix content, query
text, expectation, and source connection. An outdated review is refused.

The equivalent CLI contract is a JSON or YAML file:

```yaml
format: 1
criteria:
  - id: duplicate_ids
    name: Duplicate IDs reach zero
    source_qid: q_0003
    expectation: {kind: scalar, column: DUPLICATE_IDS, operator: eq, value: 0}
  - id: revenue_preserved
    name: Revenue stays within 0.1 percent
    source_qid: q_0004
    expectation: {kind: scalar, column: REVENUE, operator: eq, value: 0}
    relative_percent: 0.1
  - id: customers_preserved
    name: No baseline customers disappear
    source_qid: q_0005
    expectation: {kind: no_rows}
```

```bash
grayson criteria queries latest --search duplicate
# Search earlier sessions on the same connection, if needed:
grayson criteria queries latest --source-session all --search revenue
grayson criteria set latest p_001 criteria.yaml
grayson criteria show latest p_001
grayson proposal approve latest p_001        # human terminal, or use console
# Apply the approved change using your normal deployment process.
grayson proposal applied latest p_001
grayson criteria run latest p_001
grayson criteria promote latest p_001 duplicate_ids orders_no_duplicates
grayson checks activate orders_no_duplicates # human review
```

`relative_percent` is a percentage, not a fraction. A baseline of 100 with
`0.1` resolves to the inclusive range `[99.9, 100.1]` before approval. The
placeholder scalar value is replaced by these fixed bounds. Zero baselines
have a zero-width relative range; use explicit bounds for an absolute allowance.

Source queries must execute successfully, read a target table, and stay within
the session scope. Explicit LIMIT, OFFSET, and sampling are refused on success
criteria. Engine-injected guard limits still apply to fresh execution. Scalar
rules require one complete numeric result; no-rows rules test violation sets.
Use a query that returns missing baseline customer IDs to test preservation.
Counts alone cannot prove that the same identities survive. Preserve the
baseline in a stable relation or a time-travel query before applying the fix.

After the fix is marked applied, Grayson reruns the frozen SQL through the
ordinary guard, cache, audit, and query budget. Each criterion records a fresh
query ID, connection, observation time, expected and observed values, and
**pass**, **fail**, or **unproven**. Errors, unavailable data, and incomplete
scalar results remain unproven. Any failed criterion makes the overall verdict
fail; otherwise any unproven criterion prevents a pass. Every run is retained
in session events; the latest report is attached to the proposal and published
with all its cited evidence. A full pass also records verified-fix knowledge.

Promotion copies a passed criterion's exact SQL and resolved expectation into
a proposed regression check. Activation remains a separate human decision.
The tolerance is not silently rebased to the most recent result.

`criteria queries` reads history without running SQL. Results include `session_id`
and `qid`; use them as `source_session` and `source_qid` in the criteria file.
Use `--offset` with the returned `next_offset` to read another page.

MCP twins: `criteria_queries`, `criteria_set`, `criteria_show`, `criteria_run`, `criteria_promote`.
Approval remains on the human CLI/console surface, as with existing fixes and
regression checks. The old `proposal_verify` analytical verdict is refused for
fixes carrying criteria. Existing fixes without criteria retain that API.

Findings may optionally include `machine_claims` using the same format-1
envelope. Its source query IDs must belong to the finding's evidence, and every
assertion must pass before recording the finding. Baseline-relative rules are
not allowed for claims. This checks the explicit assertions; causal explanations,
severity, and whether an expectation is the right business rule still require
human judgment. The console displays each computed assertion.

## 2. Turn recorded changes into a scoped investigation

Ingest dbt definitions as usual. The ingester also records explicit dependencies
from the manifest, including sources and dependencies through ephemeral models.
It retains the manifest's observation time separately from the import time.
An older manifest cannot replace a newer observation. SQL definitions recorded
with `knowledge define`, or DDL captured by `knowledge sync`, contribute fully
qualified references; unresolved names remain visible instead of being guessed.

Routine imports preserve detected changes, including when the same manifest is
imported again. Removed dependency links retain their original observation times
and are labeled **Prior manifest**, so renamed or removed relations can still
lead to downstream investigations. These historical links describe potential
impact from earlier topology; they do not claim the dependency still exists.

```bash
grayson impact plan latest
grayson impact plan latest --table DB.S.ORDERS --freshness-days 7
grayson impact show latest
grayson impact launch latest <digest-from-plan>
grayson session brief <new-session-id>
grayson impact run-checks <new-session-id>
```

**Investigate a change** under **Session tools** opens the same workflow. A plan combines
recorded definition/hash changes, observed schema drift, explicit downstream
dependencies, fact standing and anchors, related relationships, historical
findings/fixes, and currently approved regression checks. Local unowned SQL
definition pointers can also be checked for a changed file hash. Imports
establish a baseline first; no prior observation means no demonstrated change.
You can select a changed table explicitly, labeled as a requested lead.

Each dependency shows its source, capture/observation times, and freshness under
the selected window. Unknown lineage and missing test coverage stay unknown.
Table overlap selects relevant checks; it does not prove that those checks
cover every assumption or behavior. Join relationships are leads, not dependency
edges. Missing manifest nodes, malformed/newer evidence, and omitted history
are visible in the plan.

Launch validates that the saved plan still matches current inputs, then creates
a strict-scope session with the affected tables and the full plan in its brief.
The source session's scope remains unchanged. No warehouse query runs during
planning or launch. Continue the new session in your harness; **Run selected
checks** or `impact run-checks` replays the exact selected definitions and
refuses a definition changed since planning. The harness decides which further
queries and findings follow from the plan.

MCP twins: `impact_plan`, `impact_show`, `impact_launch`, `impact_run_checks`.

## 3. Compare releases, pipelines, or equivalent time windows

Open **Compare datasets** under **Session tools**. Choose an existing session for each
environment, name each table, declare matching keys and value mappings, and
set filters and tolerances. A comparison can use two connections or two tables
in one connection. Sessions keep their existing guard, scope, and query budget.
Window descriptions are report labels; WHERE filters select the actual rows.

```yaml
format: 1
id: orders_release_01
name: Orders release candidate
left:
  session_id: <production-session-id>
  table: PROD.SALES.ORDERS
  label: Production
  filter: "ORDER_DATE >= '2026-08-01' AND ORDER_DATE < '2026-09-01'"
  window: August 2026
right:
  session_id: <candidate-session-id>
  table: CANDIDATE.SALES.ORDERS
  label: Candidate
  filter: "CREATED_DATE >= '2026-08-01' AND CREATED_DATE < '2026-09-01'"
  window: August 2026
keys:
  - {left: ORDER_ID, right: ID}
columns:
  - left: REVENUE
    right: AMOUNT
    kind: numeric
    relative_percent: 0.1
    absolute_tolerance: 0
    aggregate: true
  - {left: REGION, right: REGION}
group_by: [REGION]
allowed_missing_left: 0
allowed_missing_right: 0
allowed_changed_rows: 0
row_count_tolerance: 0
max_rows: 100000
example_limit: 20
```

```bash
grayson comparison create latest comparison.yaml
grayson comparison list latest
grayson comparison show latest orders_release_01
grayson comparison run latest orders_release_01
grayson comparison report latest orders_release_01 --output release-evidence.json
```

Definitions are immutable: revise a contract using a new ID. Preview shows the
exact SQL before execution. Each run uses six guarded SELECTs: a mapped record
extract, full-relation counts/null keys/SUMs, and duplicate-key counts per side.
All queries honor that side's filter. Filters cannot query other tables.

Keys use exact typed equality and may be composite. Duplicate or null keys fail
key integrity. A key with multiple rows is ambiguous; changed-value matching
cannot pass until that ambiguity is resolved. Values compare exactly by default;
numeric columns use decimal arithmetic with inclusive tolerances. The allowed
numeric difference is `max(absolute_tolerance, abs(baseline) * relative_percent / 100)`.
The same declared tolerance applies to a numeric column's optional SUM. Null
values match only nulls; non-finite/non-numeric numeric observations are unproven.

The report includes missing and duplicate keys, changed values, per-column
mismatch counts, full-relation aggregates, and concentrations by declared groups.
`missing_left` means candidate-only keys; `missing_right` means baseline-only
keys. Example lists and group breakdowns are bounded and label their limits.
The record-level counts describe unique, non-null matching keys.

Record parity is incomplete if either extract hits a guard/local cap, loses its
cache, changes shape, or disagrees with the separately observed row count.
Missing-key checks then remain unproven. Changed-value checks also cannot pass,
but observed mismatches above the allowed count still fail when full-relation
duplicate checks prove that matching keys are unique in both environments.
Full-relation summary checks may still pass or fail independently. A demonstrated
failure wins over unproven checks, but a pass requires all declared checks to pass. Unmapped
columns are explicitly outside coverage. Large releases may need narrower
equivalent filters or higher human-configured guard limits for full row parity.

Every result records each environment, session, connection, table, filter,
observation time, and query SQL/ID. Observations are sequential, not an atomic
cross-environment snapshot; use stable release inputs or snapshot tables
where concurrent changes matter. The console links every query and exports
the report as JSON. Session events retain earlier runs, and session reports
carry the comparison evidence.

MCP twins: `comparison_create`, `comparison_list`, `comparison_show`,
`comparison_run`, `comparison_report`. CLI `criteria run` and `comparison run`
exit nonzero for fail/unproven, so automation can gate on a computed result.

## Compatibility and storage

No database or library-format migration is required. Success criteria are an
optional format-1 proposal payload; approval/application markers, plans,
comparison definitions and latest reports use additive session metadata. Run
history uses the existing event log. Published findings and fixes retain their
format-1 record shape with optional machine evidence. Regression definitions
and replay results keep their existing formats.

Manifest lineage observations live in `knowledge/_dependencies/*.json`, each
with its own format version. Definition dependencies and change observations
are optional nested additions to format-1 knowledge documents. Existing
documents and sessions are not backfilled on upgrade. Unsupported new contract,
plan, comparison, and dependency versions are refused or reported as gaps.

Old clients can still read legacy sessions and library assets, but do not
understand or enforce these new contracts. Use an updated client for sessions
using these features, and restart MCP/console after upgrading. Refresh installed
harness instructions with `grayson harness update`; see [Upgrading](UPGRADING.md).
