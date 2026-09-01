"""MCP server exposing grayson's operations as typed tools.

A thin wrapper over the same core the CLI uses — every tool here has a CLI
twin, so a harness that speaks MCP and one that shells out behave identically.
The server discovers the workspace from the working directory at startup.
"""

from __future__ import annotations

from typing import Any

from grayson.cache.local import LocalQueryError, query_artifacts
from grayson.checks import ChecksStore
from grayson.core import engine
from grayson.core import proposals as proposals_engine
from grayson.core.engine import EnforcementError
from grayson.core.proposals import ProposalError
from grayson.core.run import cache_find, check_statement, run_statement, snapshot_metadata
from grayson.core.session import Session, find_recent_duplicate, resolve_session_id
from grayson.history import suggest_guard_profile
from grayson.interventions import build_request
from grayson.interventions.types import InterventionError
from grayson.knowledge import KnowledgeStore, completeness
from grayson.views import ViewRegistry, enter_session_scope
from grayson.workflows import WorkflowNotFound, get_workflow, list_workflows
from grayson.workspace import Workspace

INSTRUCTIONS = """\
grayson provides guarded, evidence-tracked QA over SQL tables (Snowflake).
Protocol: check knowledge and cached data first, start a session for a workflow,
run guarded queries (only SELECT/SHOW/DESCRIBE/EXPLAIN survive), close each required
checkpoint citing executed query ids as evidence, record findings against the schema,
request human interventions when judgment is needed, then propose fixes and verify them
with before/after evidence. grayson enforces the rails; you supply the analysis.
Finding nothing is a valid result: if the checks clear and nothing is worth acting on,
ask the user to close the session as a clean result — never invent a finding, or close a
checkpoint with a query picked to pass the evidence test, in order to clear a gate. A
check that does not apply is waived by the user on your request, not worked around.
Failing external checks returned at session start (external_checks) are pre-vetted
leads — replicate them first.
Profile before hand-rolling: profile_table covers a table's descriptive battery
(nulls, cardinality, ranges, key candidates, frequencies) in three or four guarded
queries whose ids are evidence — do not write forty single-column queries yourself.
Narrate the investigation visually: chart_add builds
bar/line/scatter charts from cached artifacts that render live in the user's console,
each traceable to its executed query.
If a target table has no recorded knowledge, settle grain/semantics with the user early
(or run the table-onboarding workflow), and persist durable intervention answers with
knowledge_add so future sessions start briefed.
Access warehouse data ONLY through these tools — never open warehouse or .grayson
database/state files directly (including local or sandbox files); direct reads bypass
the audit trail and produce nothing citable as evidence.
Setup/admin operations (workspace init, sandbox seeding, library config) belong to the
user: if infrastructure looks missing or broken, pause and ask instead of fixing it.
An empty knowledge library, cache, or view registry is normal in a fresh workspace.
"""


def build_server(workspace: Workspace) -> Any:
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(name="grayson", instructions=INSTRUCTIONS)

    def _session(session_id: str) -> Session:
        return Session(workspace, resolve_session_id(workspace, session_id))

    def _err(e: Exception) -> dict:
        return {"error": str(e.args[0] if e.args else e), "type": type(e).__name__}

    # -- workflows & session ------------------------------------------

    @mcp.tool(
        description="List available QA workflow templates (built-in + library). "
        "library_problems reports library files that are not loadable and why."
    )
    def workflow_list() -> dict:
        from grayson.workflows import override_problems

        return {
            "workflows": [
                {
                    "name": t.name,
                    "title": t.title,
                    "description": t.description.strip(),
                    "required_checks": t.required_check_keys(),
                    "suggested_checks": t.suggested_check_keys(),
                    "suggested_guard_profile": t.suggested_guard_profile,
                }
                for t in list_workflows(workspace.workflows_dir)
            ],
            "library_problems": override_problems(workspace.workflows_dir),
        }

    @mcp.tool(description="Show a workflow template's setup inputs, checks, and schema.")
    def workflow_show(name: str) -> dict:
        try:
            return get_workflow(name, workspace.workflows_dir).model_dump()
        except WorkflowNotFound as e:
            return _err(e)

    @mcp.tool(
        description="Start a QA session for a workflow over target tables. Pass the "
        "user's answers to the workflow's setup inputs via `inputs` (key -> answer) "
        "so the session records them. Returns the session id, seeded checkpoints, "
        "view coverage, and relevant knowledge."
    )
    def session_start(
        workflow: str,
        tables: list[str],
        title: str = "",
        guard_profile: str | None = None,
        strict_scope: bool | None = None,
        new: bool = False,
        inputs: dict[str, str] | None = None,
    ) -> dict:
        try:
            tpl = get_workflow(workflow, workspace.workflows_dir)
        except WorkflowNotFound as e:
            return _err(e)
        provided = {k: str(v) for k, v in (inputs or {}).items()}
        unknown = tpl.unknown_input_keys(provided)
        if unknown:
            return {
                "error": f"unknown setup input(s) {unknown} for workflow '{workflow}' "
                f"(defined: {tpl.input_keys() or 'none'})"
            }
        if not new:
            dup = find_recent_duplicate(workspace, workflow, tables)
            if dup:
                s = _session(dup)
                if provided:
                    s.set_setup_inputs(provided, actor="agent")
                return {
                    "reused_existing": True,
                    "session": s.summary(),
                    "checkpoints": s.checkpoints(),
                    "setup_inputs": s.setup_inputs(),
                    "note": f"an identical session '{dup}' was created moments ago and "
                    "has no work yet — continuing with it. Pass new=true to force "
                    "a separate one.",
                }
        # same resolution as `grayson session start`: explicit arg > workspace
        # per-workflow default (settings) > last-used > template suggestion
        wf_defaults = workspace.config.workflow_defaults.get(tpl.name)
        wf_profile = wf_defaults.guard_profile if wf_defaults else None
        last_used = (
            None if (guard_profile or wf_profile) else suggest_guard_profile(workspace, tables)
        )
        chosen = guard_profile or wf_profile or last_used or tpl.suggested_guard_profile
        if chosen not in workspace.config.guard_profiles:
            chosen = tpl.suggested_guard_profile
        settings = workspace.config.resolve_profile(chosen)
        if strict_scope is None:
            if wf_defaults and wf_defaults.strict_scope is not None:
                strict_scope = wf_defaults.strict_scope
            elif tpl.suggested_strict_scope is not None:
                strict_scope = tpl.suggested_strict_scope
        s = Session.create(
            workspace,
            workflow=workflow,
            targets=tables,
            guard=settings,
            guard_profile=chosen,
            title=title,
            strict_scope=strict_scope,
            actor="agent",
        )
        engine.seed_from_workflow(s, workspace.workflows_dir)
        if provided:
            s.set_setup_inputs(provided, actor="agent")
        snap = snapshot_metadata(s)
        current = {
            fq: info.get("last_altered")
            for fq, info in (snap.get("tables") or {}).items()
            if isinstance(info, dict) and info.get("last_altered")
        }
        knowledge = KnowledgeStore(workspace.knowledge_dir)
        facts = {t: knowledge.read(t)["facts"] for t in tables}
        gaps = sorted(t for t, f in facts.items() if not f)
        external = ChecksStore(workspace.checks_dir).summary(tables or None)
        registry = ViewRegistry(workspace.views_dir)
        out = {
            "session": s.summary(),
            "required_checks": [c.model_dump() for c in tpl.required_checks],
            "suggested_checks": [c.model_dump() for c in tpl.suggested_checks],
            "findings_schema": tpl.findings_schema,
            "setup_inputs": provided,
            "view_coverage": registry.coverage_check(tables, current),
            "views_in_scope": enter_session_scope(registry, s, tables),
            "knowledge": facts,
            "knowledge_gaps": gaps,
            "external_checks": external,
        }
        hints = []
        missing_inputs = tpl.missing_required_inputs(provided)
        if missing_inputs:
            hints.append(
                f"required setup inputs not recorded: {', '.join(missing_inputs)} — "
                "collect the user's answers and pass them via `inputs` at session "
                "start, so the session itself records why it was started"
            )
        if external["failing"]:
            ids = ", ".join(f["check_id"] for f in external["failing"])
            hints.append(
                f"{len(external['failing'])} external deterministic check(s) are FAILING "
                f"on the target tables ({ids}) — pre-vetted leads: replicate each with a "
                "guarded query first (their sql/details are in external_checks.failing), "
                "then widen the investigation"
            )
        if tpl.suggested_checks:
            hints.append(
                f"{len(tpl.suggested_checks)} suggested check(s) are available beyond the "
                "required ones (see suggested_checks) — they do not gate anything, but they "
                "are the fundamentals this workflow expects you to consider. Do the ones "
                "that apply to these tables and close them like any other checkpoint; skip "
                "the ones that do not, and say which in your findings"
            )
        if gaps:
            hints.append(
                f"no recorded knowledge for {', '.join(gaps)} — confirm grain/semantics "
                "with the user early (intervention), record durable answers with "
                "knowledge_add, or run the table-onboarding workflow first"
            )
        if hints:
            out["hint"] = "; ".join(hints)
        return out

    @mcp.tool(description="List sessions in this workspace.")
    def session_list() -> list[dict]:
        out = []
        for sid in workspace.list_session_ids():
            try:
                meta = Session(workspace, sid).meta_all()
            except (OSError, ValueError):
                continue
            out.append(
                {
                    "id": sid,
                    "title": meta.get("title", ""),
                    "workflow": meta.get("workflow", ""),
                    "stage": meta.get("stage", "setup"),
                    "created_at": meta.get("created_at"),
                }
            )
        return out

    @mcp.tool(description="Get a session's status summary.")
    def session_status(session_id: str) -> dict:
        try:
            return _session(session_id).summary()
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(
        description="Build a full session report: checkpoints, findings, proposals "
        "(with verification), charts, narrative, and query statistics."
    )
    def session_report(session_id: str) -> dict:
        from grayson.report import build_report

        try:
            return build_report(_session(session_id), workspace.workflows_dir)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(
        description="Set the session's report narrative — your written story of the "
        "investigation. Renders in its own clearly labeled 'agent-written' section of "
        "the report and never alters the deterministic sections. Must cite at least "
        "one executed query id (q_XXXX); write it before the user closes the session."
    )
    def session_narrate(session_id: str, text: str) -> dict:
        import re as _re

        try:
            s = _session(session_id)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)
        if s.stage == "closed":
            return {"error": "session is closed and its report is published"}
        cited = set(_re.findall(r"q_\d{4}", text))
        if not (cited & s.executed_qids()):
            return {
                "error": "the narrative must cite at least one executed query id "
                "(q_XXXX) from this session — it is the story of the evidence, "
                "not a substitute for it"
            }
        s.set_meta("report_narrative", text.strip())
        s.log_event("agent", "narrative_recorded", {"chars": len(text), "cites": sorted(cited)})
        return {"id": s.id, "narrative_chars": len(text.strip()), "cites": sorted(cited)}

    @mcp.tool(description="Register a parallel worker; returns its id for labeling queries.")
    def worker_join(session_id: str, label: str = "") -> dict:
        try:
            return {"worker": _session(session_id).worker_join(label)}
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(
        description="What still blocks the next gated stage transition. Reports open "
        "checkpoints, waived ones (and why), findings awaiting the user, and a "
        "`next_action` sentence. When `clean_close_available` is true the run found "
        "nothing worth acting on — ask the user to close it as a clean result; never "
        "record a finding you do not believe in order to clear a gate."
    )
    def session_readiness(session_id: str) -> dict:
        try:
            return engine.readiness(_session(session_id), workspace.workflows_dir)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(
        description="Advance the session stage. Evidence gates apply and cannot be forced "
        "by an agent — the user forces a bypass from the console if ever needed."
    )
    def session_advance(session_id: str, to_stage: str) -> dict:
        try:
            s = _session(session_id)
            return engine.advance_stage(s, to_stage, "agent", False, workspace.workflows_dir)
        except (EnforcementError, FileNotFoundError, ValueError) as e:
            return _err(e)

    # -- queries & cache ----------------------------------------------

    @mcp.tool(description="Guard-check a statement without executing it. Returns the verdict.")
    def query_check(session_id: str, sql: str) -> dict:
        try:
            return check_statement(_session(session_id), sql)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(
        description="Run a guarded query against Snowflake; caches results and returns a "
        "preview. Only read statements pass the guard."
    )
    def query_run(session_id: str, sql: str, worker: str | None = None, label: str = "") -> dict:
        try:
            return run_statement(_session(session_id), sql, worker=worker, label=label)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(description="The session's query audit log.")
    def query_log(session_id: str, limit: int = 50) -> list[dict]:
        try:
            return _session(session_id).query_log(limit)
        except (FileNotFoundError, ValueError) as e:
            return [_err(e)]

    @mcp.tool(
        description="Find cached query results by source table, with an optional "
        "freshness re-check against current LAST_ALTERED."
    )
    def cache_find_tool(
        session_id: str, tables: list[str] | None = None, check_freshness: bool = False
    ) -> list[dict]:
        try:
            return cache_find(_session(session_id), tables, check_freshness)
        except (FileNotFoundError, ValueError) as e:
            return [_err(e)]

    @mcp.tool(description="Show a cached artifact's sidecar metadata and a row preview.")
    def cache_show(session_id: str, qid: str, rows: int = 10) -> dict:
        try:
            s = _session(session_id)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)
        sidecar = s.cache.get(qid)
        if sidecar is None:
            return {"error": f"no cached artifact '{qid}'"}
        return {**sidecar, "preview": s.cache.preview(qid, rows)}

    @mcp.tool(
        description="Local read-only SELECT over cached artifacts (table names are qids, "
        "e.g. q_0003). Re-slice already-fetched data without a warehouse round-trip."
    )
    def cache_query(session_id: str, sql: str, max_rows: int = 1000) -> dict:
        try:
            s = _session(session_id)
            columns, data = query_artifacts(s.dir / "data", sql, max_rows)
        except (LocalQueryError, FileNotFoundError, ValueError) as e:
            return _err(e)
        return {
            "columns": columns,
            "row_count": len(data),
            "rows": [dict(zip(columns, r, strict=True)) for r in data],
        }

    # -- profiling -----------------------------------------------------

    @mcp.tool(
        description="Profile a table in a handful of guarded queries: per-column nulls, "
        "cardinality, ranges, key candidates, and value frequencies for low-cardinality "
        "columns. Every statement runs the ordinary guarded path, so the returned query "
        "ids are evidence you can close checkpoints with — do NOT hand-roll the same "
        "battery one column at a time. `observations` are mechanical leads, not verdicts."
    )
    def profile_table(
        session_id: str,
        table: str,
        sample_rows: int = 5000,
        frequencies: bool = True,
        sample: bool = True,
    ) -> dict:
        from grayson.profile import ProfileError
        from grayson.profile import profile_table as _profile

        try:
            return _profile(
                _session(session_id),
                table,
                sample_rows=sample_rows,
                frequencies=frequencies,
                sample=sample,
            )
        except (ProfileError, FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(
        description="Numeric summaries (mean, stdev, quantiles) over a cached artifact — "
        "typically a profile's sample. Computed LOCALLY, not by the warehouse: cite the "
        "artifact's query id and say the statistic was computed locally."
    )
    def profile_stats(session_id: str, qid: str) -> dict:
        from grayson.profile import summarize

        try:
            s = _session(session_id)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)
        columns, rows = s.cache.rows(qid)
        if not columns:
            return {"error": f"no cached artifact '{qid}'"}
        return {
            "qid": qid,
            "sample_rows": len(rows),
            "summaries": summarize(columns, rows),
            "computed": "local",
        }

    @mcp.tool(
        description="Pairwise correlation (pearson|spearman) over a cached artifact. Local "
        "by design — pairwise over N columns is quadratic and would cost hundreds of "
        "warehouse queries. The statistic itself is therefore unaudited: the response "
        "carries a caveat and a confidence ceiling, and you must pass both on in any "
        "finding that rests on it."
    )
    def profile_correlate(session_id: str, qid: str, method: str = "pearson") -> dict:
        from grayson.profile import correlations

        try:
            s = _session(session_id)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)
        columns, rows = s.cache.rows(qid)
        if not columns:
            return {"error": f"no cached artifact '{qid}'"}
        try:
            return {"qid": qid, **correlations(columns, rows, method)}
        except ValueError as e:
            return _err(e)

    # -- checkpoints & findings ---------------------------------------

    @mcp.tool(description="List the session's checkpoints and their status.")
    def checkpoint_list(session_id: str) -> list[dict]:
        try:
            return _session(session_id).checkpoints()
        except (FileNotFoundError, ValueError) as e:
            return [_err(e)]

    @mcp.tool(
        description="Complete a checkpoint. Requires evidence: executed query ids that "
        "exist and succeeded. If a required checkpoint genuinely does not apply to this "
        "target (freshness on a static dimension table, say), do NOT satisfy it with a "
        "query chosen to pass the scope test — file an intervention asking the user to "
        "waive it, and say why."
    )
    def checkpoint_complete(session_id: str, key: str, evidence: list[str], note: str = "") -> dict:
        try:
            return engine.complete_checkpoint(
                _session(session_id), key, evidence, note, "agent", workspace.workflows_dir
            )
        except (EnforcementError, FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(
        description="The severity/confidence scale findings are calibrated against, and "
        "which parts of it grayson enforces. Read it before assigning a severity: with "
        "no shared scale everything drifts to 'high', and a queue where everything is "
        "high has no priority in it."
    )
    def finding_rubric() -> dict:
        from grayson.findings.schemas import rubric

        return rubric()

    @mcp.tool(
        description="Record a finding. Validated against the workflow's findings schema; "
        "must cite executed query evidence. Pass the finding as a dict. Calibrate "
        "severity against `finding_rubric` — two rungs are enforced: confidence 'high' "
        "requires a `reproduction`, and severity 'critical'/'high' requires "
        "`affected_objects`."
    )
    def finding_add(session_id: str, finding: dict, worker: str | None = None) -> dict:
        try:
            return engine.record_finding(
                _session(session_id), finding, worker, workspace.workflows_dir
            )
        except (EnforcementError, FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(description="List the session's findings.")
    def finding_list(session_id: str) -> list[dict]:
        try:
            return _session(session_id).findings()
        except (FileNotFoundError, ValueError) as e:
            return [_err(e)]

    # -- interventions -------------------------------------------------

    @mcp.tool(
        description="File a human-input task (label_sample|confirm_semantics|choose|"
        "free_response). Returns the intervention id to await."
    )
    def intervention_request(
        session_id: str,
        kind: str,
        title: str,
        payload: dict,
        prompt: str = "",
        worker: str | None = None,
    ) -> dict:
        try:
            s = _session(session_id)
            request = build_request(kind, payload)
            iid = s.add_intervention(kind, title, prompt, request, worker)
            return s.intervention(iid)
        except (InterventionError, FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(
        description="Check an intervention's status/response (non-blocking; poll this). "
        "The user answers via the web console."
    )
    def intervention_check(session_id: str, iid: str) -> dict:
        try:
            item = _session(session_id).intervention(iid)
            return item or {"error": f"no intervention '{iid}'"}
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(description="List the session's interventions (optionally by status).")
    def intervention_list(session_id: str, status: str | None = None) -> list[dict]:
        try:
            return _session(session_id).interventions(status)
        except (FileNotFoundError, ValueError) as e:
            return [_err(e)]

    # -- proposals -----------------------------------------------------

    @mcp.tool(
        description="Draft a fix proposal (file_diff|ddl_snippet) linked to a finding. "
        "The user approves; the harness agent applies file diffs."
    )
    def proposal_add(
        session_id: str,
        kind: str,
        title: str,
        payload: dict,
        finding: str | None = None,
        worker: str | None = None,
    ) -> dict:
        try:
            return proposals_engine.record_proposal(
                _session(session_id), kind, title, payload, finding, worker
            )
        except (ProposalError, FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(description="List fix proposals for the session.")
    def proposal_list(session_id: str) -> list[dict]:
        try:
            return _session(session_id).proposals()
        except (FileNotFoundError, ValueError) as e:
            return [_err(e)]

    @mcp.tool(
        description="Mark an approved proposal as applied (after the harness agent "
        "edited the files)."
    )
    def proposal_applied(session_id: str, pid: str) -> dict:
        try:
            return proposals_engine.mark_applied(_session(session_id), pid)
        except (ProposalError, FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(
        description="Record before/after verification for a proposal, citing executed "
        "query ids. grayson computes the comparison deterministically."
    )
    def proposal_verify(
        session_id: str, pid: str, before_qid: str, after_qid: str, verdict: str, note: str = ""
    ) -> dict:
        try:
            return proposals_engine.verify(
                _session(session_id), pid, before_qid, after_qid, verdict, note
            )
        except (ProposalError, FileNotFoundError, ValueError) as e:
            return _err(e)

    # -- knowledge & views --------------------------------------------

    def _library_sync(out: dict, message: str) -> dict:
        from grayson.library import maybe_auto_push

        sync = maybe_auto_push(workspace, message, via="mcp-agent")
        if sync is not None:
            out["library_sync"] = sync
        return out

    @mcp.tool(
        description="Read the knowledge library entry for a table, including its "
        "base-descriptor completeness report (what is still undescribed)."
    )
    def knowledge_show(table: str) -> dict:
        try:
            doc = KnowledgeStore(workspace.knowledge_dir).read(table)
            return {**doc, "completeness": completeness(doc)}
        except ValueError as e:
            return _err(e)

    @mcp.tool(
        description="Add a fact about a table (status proposed|data_inferred|"
        "user_confirmed). Agents propose; users confirm."
    )
    def knowledge_add(
        table: str,
        fact: str,
        status: str = "proposed",
        evidence: list[str] | None = None,
        by: str = "agent",
    ) -> dict:
        try:
            out = dict(
                KnowledgeStore(workspace.knowledge_dir).add_fact(
                    table, fact, status=status, created_by=by, evidence=evidence or []
                )
            )
            return _library_sync(out, f"grayson knowledge: fact for {table.upper()}")
        except ValueError as e:
            return _err(e)

    @mcp.tool(
        description="Set structured base-descriptor fields for a table: grain, columns "
        "(name/type/description), relationships, freshness, owners, open_questions, "
        "definition_files. Merged per-field; returns the doc plus completeness."
    )
    def knowledge_set(table: str, profile: dict) -> dict:
        try:
            doc = KnowledgeStore(workspace.knowledge_dir).set_profile(table, profile)
            out = {**doc, "completeness": completeness(doc)}
            return _library_sync(out, f"grayson knowledge: profile {table.upper()}")
        except ValueError as e:
            return _err(e)

    @mcp.tool(description="Search the knowledge library for a term.")
    def knowledge_search(term: str) -> list[dict]:
        return KnowledgeStore(workspace.knowledge_dir).search(term)

    @mcp.tool(
        description="Search past findings and fix proposals across ALL sessions — "
        "how similar problems were diagnosed and what fixed them. Returns summaries; "
        "use records_get for the full record."
    )
    def records_search(term: str = "", kind: str | None = None, limit: int = 20) -> list[dict]:
        from grayson.records import search_records as _search

        try:
            return _search(workspace, term, kind, limit)
        except ValueError as e:
            return [_err(e)]

    @mcp.tool(
        description="Fetch one past record in full: a finding or proposal from any "
        "session, including its payload and (for proposals) verification."
    )
    def records_get(session_id: str, kind: str, record_id: str) -> dict:
        from grayson.records import get_record as _get

        try:
            item = _get(workspace, resolve_session_id(workspace, session_id), kind, record_id)
        except (ValueError, FileNotFoundError) as e:
            return _err(e)
        return item or {"error": f"no {kind} '{record_id}' in session '{session_id}'"}

    @mcp.tool(
        description="View-library coverage for target tables: which views to reuse, "
        "refresh (stale), or build (gaps). check_freshness fetches current "
        "LAST_ALTERED so stale views are actually detected."
    )
    def views_check(tables: list[str], check_freshness: bool = False) -> dict:
        registry = ViewRegistry(workspace.views_dir)
        current = None
        if check_freshness:
            from grayson.core.run import fetch_last_altered

            sources = sorted(
                {t.upper() for t in tables}
                | {s for v in registry.matching(tables) for s in v.normalized_sources()}
            )
            current = fetch_last_altered(workspace.config.connection, workspace.root, sources)
        return registry.coverage_check(tables, current)

    @mcp.tool(
        description="Bring registered library views into the session's query scope "
        "mid-session (session start does this automatically for views matching the "
        "targets). Only views already in the registry qualify."
    )
    def views_use(session_id: str, names: list[str]) -> dict:
        registry = ViewRegistry(workspace.views_dir)
        resolved = []
        for name in names:
            entry = registry.get(name)
            if entry is None:
                return {
                    "error": f"'{name}' is not in the view registry — only registered "
                    "views can enter scope"
                }
            resolved.append(entry.name.upper())
        try:
            s = _session(session_id)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)
        s.add_scope(resolved)
        s.log_event("agent", "views_in_scope", {"views": resolved})
        return {"views_in_scope": resolved, "scope": sorted(s.scope_tables)}

    @mcp.tool(
        description="Build a chart (bar|line|scatter) from a cached artifact; it renders "
        "live in the user's console, traceable to the executed query. Aggregate/order "
        "with SQL first, then chart the artifact. Up to 3 y columns (line/scatter); "
        "bar takes one. Use charts to narrate the investigation visually. The response's "
        "`text` field is a terminal rendering — paste it into your chat reply (in a code "
        "block) so the user sees the shape without leaving the conversation."
    )
    def chart_add(
        session_id: str,
        qid: str,
        kind: str,
        x: str,
        y: list[str],
        title: str,
        note: str = "",
        worker: str | None = None,
    ) -> dict:
        from grayson.charts import ChartError, add_chart, chart_data, render_text

        try:
            s = _session(session_id)
            spec = add_chart(s, qid, kind, x, y, title, note, worker)
            return {**spec, "text": render_text(spec, chart_data(s, spec))}
        except (ChartError, FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(description="List the charts built in a session.")
    def chart_list(session_id: str) -> list[dict]:
        from grayson.charts import list_charts

        try:
            return list_charts(_session(session_id))
        except (FileNotFoundError, ValueError) as e:
            return [_err(e)]

    @mcp.tool(
        description="External deterministic checks (Airflow, dbt, ...) on file in the "
        "library: latest result per check, failures and overdue runs called out. "
        "Failing checks on target tables are pre-vetted leads — replicate them first."
    )
    def checks_status(tables: list[str] | None = None) -> dict:
        return ChecksStore(workspace.checks_dir).summary(tables)

    @mcp.tool(description="One external check's run history, newest first.")
    def checks_show(check_id: str) -> list[dict]:
        return [r.model_dump() for r in ChecksStore(workspace.checks_dir).history(check_id)]

    @mcp.tool(
        description="Read the workspace configuration: guard profiles, scopes, "
        "connection, library pointer. READ-ONLY by design — changing settings is a "
        "user action (`grayson config` or the console's Settings page); ask the user "
        "if a setting needs to change."
    )
    def config_show() -> dict:
        from grayson.config_edit import config_summary

        return config_summary(workspace.root)

    return mcp


def serve_stdio(workspace: Workspace) -> None:
    mcp = build_server(workspace)
    mcp.run(transport="stdio")


class HealthzASGI:
    """Unauthenticated liveness endpoint, outermost so container platforms can
    probe the process without holding the bearer token. Reports process
    liveness only — never library content or tool state."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("path") == "/healthz"
            and scope.get("method") in ("GET", "HEAD")
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"status": "ok"}'})
            return
        await self.app(scope, receive, send)


class BearerAuthASGI:
    """Minimal bearer-token wall around the streamable-HTTP MCP app.

    The HTTP transport exists so the server can run where the Snowflake
    credentials live (a service account, a container) while the agent runs
    where they don't — the credential-isolation deployment. The token gates
    the tool surface itself; the isolation comes from the process boundary.

    Default-deny by scope type: only plain HTTP (checked) and lifespan pass.
    A websocket (or any future scope type) is refused outright rather than
    slipping past a wall that only inspected `http`.
    """

    def __init__(self, app: Any, token: str):
        self.app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "http":
            import secrets

            headers = {k.lower(): v for k, v in scope.get("headers") or []}
            supplied = (headers.get(b"authorization") or b"").decode("latin-1")
            if not secrets.compare_digest(supplied, self._expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"error": "missing or invalid bearer token"}',
                    }
                )
                return
        elif scope_type == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 1008})
            return
        elif scope_type != "lifespan":
            return
        await self.app(scope, receive, send)


def serve_http(mcp: Any, host: str, port: int, token: str | None) -> None:
    """Serve an MCP server over streamable HTTP.

    With a token, requests must present it as a bearer. token=None disables
    the wall — ONLY for deployment behind a gateway that already authenticates
    callers (and typically owns the Authorization header itself). GET /healthz
    answers 200 without a token either way (liveness only, no content)."""
    import uvicorn

    app = mcp.streamable_http_app(host=host)
    if token:
        app = BearerAuthASGI(app, token)
    app = HealthzASGI(app)
    uvicorn.run(app, host=host, port=port, log_level="warning")
