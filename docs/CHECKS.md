# External checks: setting up ingestion

Your team already runs deterministic validations somewhere — dbt tests, Airflow
DAGs, a data-quality service, a cron job with SQL in it. grayson does not run
any of those. It reads their **results**, keeps them in the library, and hands
failing checks on the target tables to the agent at session start as
pre-vetted leads (with the check's own SQL to replicate first). A check whose
latest run is older than its declared cadence is flagged **overdue**, so
silently-stopped automation surfaces too.

This page is the end-to-end setup guide: the result contract, the three ways
to feed results in, and how to adapt whatever shape your validation tool
already produces.

## 1. The contract

One check run = one JSON object. A file may hold a single object, a list, or
`{"results": [...]}` — all three parse.

```json
{
  "check_id": "orders_null_email",
  "status": "fail",
  "run_at": "2026-08-24T06:00:00Z",

  "name": "orders: email should not be NULL",
  "tables": ["ANALYTICS.SHOP.ORDERS"],
  "source": "airflow",
  "metrics": {"null_count": 812},
  "details": "812 rows with NULL email since 2026-08-20",
  "sql": "SELECT COUNT(*) FROM ANALYTICS.SHOP.ORDERS WHERE EMAIL IS NULL",
  "url": "https://airflow.internal/dags/qa_orders/runs/1234",
  "ttl_hours": 26
}
```

| Field | Required | What it does |
|---|---|---|
| `check_id` | yes | Stable identity across runs (1–100 chars of letters, digits, `.`, `_`, `-`). History and dedupe key on it. |
| `status` | yes | `pass` \| `fail` \| `warn` \| `error` \| `skipped`. `fail` and `error` become session-start leads. |
| `run_at` | yes | ISO timestamp of the run. Dedupe key with `check_id`; drives overdue detection. |
| `tables` | no, but load-bearing | Fully-qualified `DB.SCHEMA.TABLE` names the check covers (upper-cased on read). **This is the join key to sessions** — a check with no tables never surfaces as a lead. |
| `sql` | no | The check's own query. Agents replicate it first, so include it whenever it exists. |
| `ttl_hours` | no | Expected cadence. Latest run older than this ⇒ flagged overdue. A daily check wants ~26 (cadence + slack), not 24. |
| `name`, `source`, `details`, `metrics`, `url`, `severity` | no | Display and context. `metrics` keeps any numbers worth trending. |

Malformed entries never break a load — they are reported as parse errors in
the console's Checks tab while every valid result still counts.

## 2. Three ways in

### a. Drop files (simplest — automation writes, grayson reads)

Anything that can write a file can integrate. grayson reads **every**
`*.json` under the library's `checks/` directory, any layout — one file per
DAG, per day, per check, your call. Overwriting one file per check with its
latest result is a perfectly good steady state.

An Airflow task, in its entirety:

```python
def publish_check_result(**ctx):
    result = {
        "check_id": "orders_null_email",
        "status": "fail" if null_count else "pass",
        "run_at": ctx["ts"],
        "tables": ["ANALYTICS.SHOP.ORDERS"],
        "source": "airflow",
        "metrics": {"null_count": null_count},
        "sql": CHECK_SQL,
        "ttl_hours": 26,
    }
    path = QA_LIBRARY / "checks" / "orders_null_email.json"
    path.write_text(json.dumps(result, indent=2))
```

### b. `grayson checks ingest` (validated, deduped, bounded history)

Point it at a results file (or a directory of them):

```bash
grayson checks ingest results.json --source airflow
```

Ingest validates against the contract, skips `(check_id, run_at)` pairs
already on file (safe to re-run), keeps the newest 25 runs per check under
`checks/ingested/<check_id>.json`, and — with the team library linked and
`--auto-push` on — commits and pushes the update. Use this from CI/cron when
you want history and validation errors surfaced at hand-off time instead of
read time.

### c. dbt (built in — no glue code)

`dbt test` already writes everything needed. After a run:

```bash
grayson checks ingest target/run_results.json --manifest target/manifest.json --ttl-hours 26
```

The dbt shape is detected automatically. Each test node becomes one check:
dbt statuses map 1:1 (`pass`/`fail`/`warn`/`error`/`skipped`), `failures` and
execution time land in `metrics`, and the test's message becomes `details`.
The `--manifest` flag is what makes results *useful*: it resolves each test to
the fully-qualified tables it depends on (the join key to sessions) and pulls
the test's compiled SQL so agents can replicate it. Without a manifest the
results still ingest, but with no `tables` they will not surface as leads.

## 3. Sharing through the team library

Checks are plain files, shared exactly like knowledge and views:

```bash
grayson library link git@github.com:your-org/qa-library.git --auto-push
```

With `--auto-push`, every ingest commits and pushes; teammates' sessions see
the new results on their next `library pull` (or automatically, depending on
your setup). Automation can equally commit directly to the library repo —
pattern (a) with a `git commit` at the end.

## 4. Adapting your own tool

Any validation system fits with a small mapping onto the contract — the whole
dbt adapter is ~60 lines (`src/grayson/checks/adapters.py`) and makes a good
template. The recipe:

1. Find your tool's stable identity for a validation → `check_id`.
2. Map its outcome vocabulary onto `pass|fail|warn|error|skipped`.
3. Resolve the physical tables it covers → `tables` (this is the step that
   matters most; everything else is garnish).
4. Carry the query if one exists → `sql`; numbers → `metrics`.
5. Emit JSON into `checks/` (pattern a) or through `checks ingest` (pattern b).

Great Expectations, for instance: `expectation_suite_name` +
`expectation_type` + column → `check_id`; `success` → `pass`/`fail`;
`batch_kwargs`/`batch_spec` table → `tables`; `result.unexpected_count` →
`metrics`. Twenty lines in the task that already runs your suite.

## 5. What agents see

At session start, grayson surveys the library for checks whose `tables`
intersect the session's targets: failing ones arrive in full (details,
metrics, SQL) as leads to replicate first; passing ones as compact context;
overdue ones flagged. The console's **Checks** tab shows the same picture for
humans — failures first, then all checks with status, source, age, and the
check SQL (highlighted, one-click copy).
