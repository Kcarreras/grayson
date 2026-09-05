# Regression checks: make investigations repeatable

A fixed bug should be easier to catch the next time. Grayson can turn an
executed investigation query into a reviewed check, keep it in the team library,
and replay it in later sessions with fresh evidence.

For example, an investigation discovers duplicate order IDs. After the fix,
this query returns one row with `DUPLICATE_ORDERS = 0`:

```sql
SELECT COUNT(*) AS DUPLICATE_ORDERS
FROM (
  SELECT ORDER_ID
  FROM ANALYTICS.SHOP.ORDERS
  GROUP BY ORDER_ID
  HAVING COUNT(*) > 1
)
```

Save the query with that explicit expectation. When a teammate investigates
orders next week, they can run the approved check before starting an open-ended
search. A failure comes with a new query ID and observed count to investigate.

## In the console

1. Open an executed query and expand **Save as regression check**.
2. Name the check, describe the problem it should catch, and choose the expected
   result. Saving creates a proposal; it does not rerun the query.
3. Review the SQL, expectation, original observation, and source connection.
   Choose **Activate this check** when the rule captures the team's intent.
4. From **Checks**, select an open investigation session and **Run check**.
   Sessions also offer **Run N regression checks** for all active checks on
   their target tables.
5. Inspect the pass/fail/error result and follow its query ID to fresh evidence.
   Each check shows up to 50 recent runs; session briefs retain the latest 20
   regression events across agent restarts.

The rule is explicit: a metric that happened to be zero today is not
automatically a business invariant. Checks may be proposed from failing source
observations too, so a team can record the desired behavior before a fix lands.

## Expectations

| Kind | Meaning | Example |
|---|---|---|
| `no_rows` | The SQL selects violations; zero returned rows pass, any returned row fails. | Orders whose amount is negative |
| `scalar` | Exactly one complete result row contains a named numeric column, compared with an explicit value or inclusive range. | `DUPLICATE_ORDERS = 0`, `MATCH_RATE >= 0.99` |

Scalar operators are `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, and `between`.
Decimal values and large integer thresholds retain their precision. A missing
or ambiguous column, NULL, nonnumeric value, incomplete result, or failed query
produces **error**, never a pass. Column names match exactly first, then by an
unambiguous case-insensitive match.

For `no_rows`, a capped nonempty result is enough to prove a violation; its
count is a lower bound. For a scalar, a capped result cannot prove that exactly
one row exists and is an error. The guard exempts recognized aggregate queries
from auto-limiting; a one-row cap on a nonaggregate query cannot establish
completeness.

Each source must be a successfully executed SELECT or set query that reads a
table. Constant expressions, metadata statements, and rejected queries cannot
become regression checks.

## CLI walkthrough

Run the SQL above through `grayson run` in an investigation session. Substitute
the actual session ID and executed query ID below:

```bash
grayson checks propose <session-id> <qid> \
  --id orders_unique --name "Orders remain unique" \
  --description "Catch duplicate order IDs returning after the import fix" \
  --expect scalar --column DUPLICATE_ORDERS --operator eq --value 0

grayson checks definition orders_unique
grayson checks activate orders_unique
grayson checks run <session-id> --check orders_unique
grayson checks show regression.orders_unique
```

Activation and retirement require a human's interactive terminal or the
console. There is no unattended approval flag.

```bash
grayson checks regressions --table ANALYTICS.SHOP.ORDERS
grayson checks run <session-id>        # active checks on this session's targets
grayson checks retire orders_unique   # retains definition and history
```

Repeat `--check` to select several checks. Selection is validated before any
query executes. `checks run` returns JSON and exits nonzero if any check fails,
errors, cannot save its result, if definitions are malformed, or if no check
was selected. `library_sync` reports sharing separately from the SQL verdict.

## Agents and harnesses

The full MCP server exposes `checks_regressions`, `checks_propose`, and
`checks_run`; CLI and console use the same underlying code. There is no MCP
tool for activation or retirement. The knowledge-only server exposes definitions
and existing results read-only, with no query execution or mutation.

Session-start responses include definitions relevant to the target tables.
Updated instructions for Cursor, Claude Code, Codex, and Copilot explain when
to replay and propose them. Other harnesses can use the CLI or full MCP tools.
Refresh existing instructions with `grayson harness update <harness>` and
`--apply` after reviewing the preview; see [Upgrading](UPGRADING.md).

Replays use the chosen session's connection, table scope, query budget,
statement timeout, and audit path. They never extend scope automatically.
The connection and session ID are recorded with each result, so a teammate's
run can be distinguished from the source observation. A result on one connection
does not establish the state of another warehouse.

A pass tests the approved expectation. A failure does not establish root cause;
an error is inconclusive. Results never accept findings, verify a fix, or
complete workflow gates automatically. Agents should cite the new executed
query IDs when making claims.

## Sharing, replacement, and upgrades

Definitions live in `checks/regressions/<id>.yaml`. Each native replay writes
a separate JSON result under `checks/runs/regression.<id>/`, using the existing
[check result contract](CHECKS.md). Concurrent replays retain separate results
without competing to rewrite one history file. Native history is retained;
external ingestion's existing 25-run limit remains unchanged.

Definitions contain SQL, the expectation, review metadata, source query/session
IDs, and the original aggregate observation. Results carry SQL, outcome,
aggregate observation, connection, and evidence IDs. Cached result rows and
session databases stay in the originating workspace. A teammate can replay the
check without possessing the source session; source evidence links are shown
only when that session is available locally.

Writes commit their own paths when a Git library is linked and respect its
existing `auto_push` setting. CLI/MCP responses report sharing failures; the
console calls them out while retaining the local changes. Retry through the
normal library pull/push workflow.

Approval applies to the saved SQL and expectation. Editing a definition
invalidates it for replay. To change a rule, execute the revised query, propose
a check with a new ID, review it, and retire the old check. Retirement preserves
history while removing its results from current failure leads. Unsupported
definition formats are reported without rewriting the files.

No library migration or reinitialization is required. Existing knowledge,
workflows, external check results, and sessions retain their formats. Older
Grayson clients ignore the YAML definitions and can read the JSON results,
although they cannot interpret retirement. This is an on-demand investigation
feature; scheduling and external validation systems continue independently.
