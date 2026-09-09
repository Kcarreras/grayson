"""Shared project instructions installed into each supported agent harness."""

PROJECT_PROTOCOL = """

## Goal-driven SQL projects

Use `pipeline-development` for new pipelines and `goal-analysis` for analytical
goals. Existing QA workflows retain their ordinary findings/fix lifecycle.
Project tools never authorise warehouse DDL/DML execution.

1. Interview for the decision, deliverable, population, grain, business definitions,
   exclusions, source tables, time window, output destination and budget. Reuse
   confirmed knowledge with attribution. Discover schema facts with scoped reads;
   do not ask the user questions SQL can settle. Batch related human decisions.
2. Read `project_schema`; draft the complete brief with `project_draft` for console
   approval. Every semantic definition must map to executable acceptance checks or
   explicitly require human semantic review. Baselines must be independent of the
   candidate. Freeze tolerances before observing candidate results. A negative or
   inconclusive analysis can satisfy an honest goal.
3. Read `project_status` / `session_brief` on every resume. The effective policy,
   revision, pinned method and remaining budgets govern the work. Use `project_plan`
   for working steps; it cannot weaken the brief or remove workflow gates.
4. Build a candidate graph. Single-input query nodes may filter, compute, aggregate,
   or use windows. Joins are explicit join nodes whose left/right inputs, key tuples,
   cardinality, coverage and business meaning are in the approved contract. Join
   projections use l.COLUMN and r.COLUMN. The output is available as `candidate`.
   Unsupported SQL shapes are a request for a scoped design change, never a reason
   to bypass the guarded query path.
5. Run `project_verify`. It generates full-relation probes for null/duplicate join
   keys, fanout bounds, unmatched inputs, grain, missing/added identities, measure
   preservation and expected semantic values. Group measures at the decision's
   meaningful grain; grand totals alone conceal compensating errors. Include tests
   for each material intermediate transformation when final totals could hide it.
6. Diagnose fail/unproven results. Inspect source keys and examples through guarded
   queries. State what failed, why, and what the next candidate changes. Name
   addressed_checks. Never fix fanout with arbitrary DISTINCT or ROW_NUMBER, discard
   unknown rows, coalesce nulls to zero, or select a convenient dimension version
   without an approved business rule. Never lower tolerances to obtain a pass.
7. Once all checks pass, perform a fresh critical review using only the fixed brief,
   current SQL and evidence. Answer each review question, cite current verification
   query IDs, and record issues and limitations using `project_review`. This is an
   agent assessment, not human confirmation. Complete workflow checkpoints with
   current candidate evidence. A changed candidate invalidates earlier evidence.
8. `project_finish` follows the effective approval policy. A pipeline becomes ready
   for deployment, not deployed. `project_deployment` prepares exact CREATE SQL in
   the existing proposal UI. The person approves, runs it, and reports application.
   `project_deployment_check` then checks the real output in a separate scoped audit
   session. `project_accept_deployment` applies the result-acceptance policy.

Semantic ambiguity, new tables, altered goals or tolerances, expired credentials,
budget exhaustion and stalled progress require a structured intervention or a
specific `project_pause` blocker. Continue independent permitted work while waiting;
do not answer a human intervention yourself. Time spent in human review/deployment
phases is excluded from the active-phase time budget. Completed work, cancelled
work, failed verification and deployment pending are distinct outcomes.

`grayson project run SID provider.json --watch` can supervise a human-configured
JSON-action provider and resume across approvals. Interactive MCP use needs no
provider adapter. Neither mode is a sandbox around a model or its external process;
use read-only warehouse credentials and isolate the guarded server in production.
"""
