"""MCP server exposing seekql's operations as typed tools.

A thin wrapper over the same core the CLI uses — every tool here has a CLI
twin, so a harness that speaks MCP and one that shells out behave identically.
The server discovers the workspace from the working directory at startup.
"""

from __future__ import annotations

from typing import Any

from seekql.cache.local import LocalQueryError, query_artifacts
from seekql.core import engine
from seekql.core import proposals as proposals_engine
from seekql.core.engine import EnforcementError
from seekql.core.proposals import ProposalError
from seekql.core.run import cache_find, check_statement, run_statement, snapshot_metadata
from seekql.core.session import Session, find_recent_duplicate, resolve_session_id
from seekql.history import suggest_guard_profile
from seekql.interventions import build_request
from seekql.interventions.types import InterventionError
from seekql.knowledge import KnowledgeStore, completeness
from seekql.views import ViewRegistry
from seekql.workflows import WorkflowNotFound, get_workflow, list_workflows
from seekql.workspace import Workspace

INSTRUCTIONS = """\
seekql provides guarded, evidence-tracked QA over SQL tables (Snowflake).
Protocol: check knowledge and cached data first, start a session for a workflow,
run guarded queries (only SELECT/SHOW/DESCRIBE/EXPLAIN survive), close each required
checkpoint citing executed query ids as evidence, record findings against the schema,
request human interventions when judgment is needed, then propose fixes and verify them
with before/after evidence. seekql enforces the rails; you supply the analysis.
If a target table has no recorded knowledge, settle grain/semantics with the user early
(or run the table-onboarding workflow), and persist durable intervention answers with
knowledge_add so future sessions start briefed.
Access warehouse data ONLY through these tools — never open warehouse or .seekql
database/state files directly (including local or sandbox files); direct reads bypass
the audit trail and produce nothing citable as evidence.
Setup/admin operations (workspace init, sandbox seeding, library config) belong to the
user: if infrastructure looks missing or broken, pause and ask instead of fixing it.
An empty knowledge library, cache, or view registry is normal in a fresh workspace.
"""


def build_server(workspace: Workspace) -> Any:
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(name="seekql", instructions=INSTRUCTIONS)

    def _session(session_id: str) -> Session:
        return Session(workspace, resolve_session_id(workspace, session_id))

    def _err(e: Exception) -> dict:
        return {"error": str(e.args[0] if e.args else e), "type": type(e).__name__}

    # -- workflows & session ------------------------------------------

    @mcp.tool(description="List available QA workflow templates.")
    def workflow_list() -> list[dict]:
        return [
            {
                "name": t.name,
                "title": t.title,
                "description": t.description.strip(),
                "required_checks": t.required_check_keys(),
                "suggested_guard_profile": t.suggested_guard_profile,
            }
            for t in list_workflows(workspace.workflows_dir)
        ]

    @mcp.tool(description="Show a workflow template's setup inputs, checks, and schema.")
    def workflow_show(name: str) -> dict:
        try:
            return get_workflow(name, workspace.workflows_dir).model_dump()
        except WorkflowNotFound as e:
            return _err(e)

    @mcp.tool(
        description="Start a QA session for a workflow over target tables. Returns the "
        "session id, seeded checkpoints, view coverage, and relevant knowledge."
    )
    def session_start(
        workflow: str,
        tables: list[str],
        title: str = "",
        guard_profile: str | None = None,
        strict_scope: bool | None = None,
        new: bool = False,
    ) -> dict:
        try:
            tpl = get_workflow(workflow, workspace.workflows_dir)
        except WorkflowNotFound as e:
            return _err(e)
        if not new:
            dup = find_recent_duplicate(workspace, workflow, tables)
            if dup:
                s = _session(dup)
                return {
                    "reused_existing": True,
                    "session": s.summary(),
                    "checkpoints": s.checkpoints(),
                    "note": f"an identical session '{dup}' was created moments ago and "
                    "has no work yet — continuing with it. Pass new=true to force "
                    "a separate one.",
                }
        last_used = None if guard_profile else suggest_guard_profile(workspace, tables)
        chosen = guard_profile or last_used or tpl.suggested_guard_profile
        if chosen not in workspace.config.guard_profiles:
            chosen = tpl.suggested_guard_profile
        settings = workspace.config.resolve_profile(chosen)
        s = Session.create(
            workspace,
            workflow=workflow,
            targets=tables,
            guard=settings,
            guard_profile=chosen,
            title=title,
            strict_scope=strict_scope,
        )
        engine.seed_from_workflow(s, workspace.workflows_dir)
        snap = snapshot_metadata(s)
        current = {
            fq: info.get("last_altered")
            for fq, info in (snap.get("tables") or {}).items()
            if isinstance(info, dict) and info.get("last_altered")
        }
        knowledge = KnowledgeStore(workspace.knowledge_dir)
        facts = {t: knowledge.read(t)["facts"] for t in tables}
        gaps = sorted(t for t, f in facts.items() if not f)
        out = {
            "session": s.summary(),
            "required_checks": [c.model_dump() for c in tpl.required_checks],
            "findings_schema": tpl.findings_schema,
            "view_coverage": ViewRegistry(workspace.views_dir).coverage_check(tables, current),
            "knowledge": facts,
            "knowledge_gaps": gaps,
        }
        if gaps:
            out["hint"] = (
                f"no recorded knowledge for {', '.join(gaps)} — confirm grain/semantics "
                "with the user early (intervention), record durable answers with "
                "knowledge_add, or run the table-onboarding workflow first"
            )
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
        "(with verification), and query statistics."
    )
    def session_report(session_id: str) -> dict:
        from seekql.report import build_report

        try:
            return build_report(_session(session_id), workspace.workflows_dir)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(description="Register a parallel worker; returns its id for labeling queries.")
    def worker_join(session_id: str, label: str = "") -> dict:
        try:
            return {"worker": _session(session_id).worker_join(label)}
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(description="What still blocks the next gated stage transition.")
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

    # -- checkpoints & findings ---------------------------------------

    @mcp.tool(description="List the session's checkpoints and their status.")
    def checkpoint_list(session_id: str) -> list[dict]:
        try:
            return _session(session_id).checkpoints()
        except (FileNotFoundError, ValueError) as e:
            return [_err(e)]

    @mcp.tool(
        description="Complete a checkpoint. Requires evidence: executed query ids that "
        "exist and succeeded."
    )
    def checkpoint_complete(session_id: str, key: str, evidence: list[str], note: str = "") -> dict:
        try:
            return engine.complete_checkpoint(
                _session(session_id), key, evidence, note, "agent", workspace.workflows_dir
            )
        except (EnforcementError, FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(
        description="Record a finding. Validated against the workflow's findings schema; "
        "must cite executed query evidence. Pass the finding as a dict."
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
        "query ids. seekql computes the comparison deterministically."
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
        from seekql.library import maybe_auto_push

        sync = maybe_auto_push(workspace, message)
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
            return _library_sync(out, f"seekql knowledge: fact for {table.upper()}")
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
            return _library_sync(out, f"seekql knowledge: profile {table.upper()}")
        except ValueError as e:
            return _err(e)

    @mcp.tool(description="Search the knowledge library for a term.")
    def knowledge_search(term: str) -> list[dict]:
        return KnowledgeStore(workspace.knowledge_dir).search(term)

    @mcp.tool(
        description="View-library coverage for target tables: which views to reuse, "
        "refresh (stale), or build (gaps)."
    )
    def views_check(tables: list[str]) -> dict:
        return ViewRegistry(workspace.views_dir).coverage_check(tables)

    return mcp


def serve_stdio(workspace: Workspace) -> None:
    mcp = build_server(workspace)
    mcp.run(transport="stdio")
