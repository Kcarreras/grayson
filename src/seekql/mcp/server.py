"""MCP server exposing seekql's operations as typed tools.

A thin wrapper over the same core the CLI uses — every tool here has a CLI
twin, so a harness that speaks MCP and one that shells out behave identically.
The server discovers the workspace from the working directory at startup.
"""

from __future__ import annotations

from typing import Any

from seekql.core import engine
from seekql.core import proposals as proposals_engine
from seekql.core.engine import EnforcementError
from seekql.core.proposals import ProposalError
from seekql.core.run import cache_find, check_statement, run_statement, snapshot_metadata
from seekql.core.session import Session
from seekql.history import suggest_guard_profile
from seekql.interventions import build_request
from seekql.interventions.types import InterventionError
from seekql.knowledge import KnowledgeStore
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
"""


def build_server(workspace: Workspace) -> Any:
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(name="seekql", instructions=INSTRUCTIONS)

    def _session(session_id: str) -> Session:
        return Session(workspace, session_id)

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
    ) -> dict:
        try:
            tpl = get_workflow(workflow, workspace.workflows_dir)
        except WorkflowNotFound as e:
            return _err(e)
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
        return {
            "session": s.summary(),
            "required_checks": [c.model_dump() for c in tpl.required_checks],
            "findings_schema": tpl.findings_schema,
            "view_coverage": ViewRegistry(workspace.views_dir).coverage_check(tables, current),
            "knowledge": {t: knowledge.read(t)["facts"] for t in tables},
        }

    @mcp.tool(description="Get a session's status summary.")
    def session_status(session_id: str) -> dict:
        try:
            return _session(session_id).summary()
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(description="What still blocks the next gated stage transition.")
    def session_readiness(session_id: str) -> dict:
        try:
            return engine.readiness(_session(session_id), workspace.workflows_dir)
        except (FileNotFoundError, ValueError) as e:
            return _err(e)

    @mcp.tool(description="Advance the session stage (evidence gates apply unless force).")
    def session_advance(session_id: str, to_stage: str, force: bool = False) -> dict:
        try:
            s = _session(session_id)
            return engine.advance_stage(s, to_stage, "agent", force, workspace.workflows_dir)
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

    @mcp.tool(description="Read the knowledge library entry for a table.")
    def knowledge_show(table: str) -> dict:
        try:
            return KnowledgeStore(workspace.knowledge_dir).read(table)
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
            return KnowledgeStore(workspace.knowledge_dir).add_fact(
                table, fact, status=status, created_by=by, evidence=evidence or []
            )
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
