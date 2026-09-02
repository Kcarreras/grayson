"""Knowledge-only MCP server: read-only library access without the harness.

For a teammate who does not run grayson sessions but wants their agent briefed
by the team's knowledge library. Serves a library directory directly — no
workspace, no warehouse connection, no session state — and registers ONLY read
tools. Read-only is enforced by construction (no write tool exists on this
surface), not by prompting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from grayson.checks import ChecksStore
from grayson.knowledge import KnowledgeStore, completeness
from grayson.views import ViewRegistry
from grayson.workflows import (
    WorkflowNotFound,
    get_workflow,
    list_workflows,
    override_problems,
)

INSTRUCTIONS = """\
Read-only access to a grayson team knowledge library: table semantics with
provenance (proposed / data_inferred / user_confirmed facts), QA workflow
templates, registered QA views, external deterministic check results, and the
team's published records (accepted findings and verified fixes from past
sessions). Use it to brief yourself before working with these tables: check
knowledge_show for grain/semantics, checks_status for failing deterministic
checks, records_search for how similar problems were diagnosed and fixed
before, and workflow_list for how this team structures investigations.
This surface cannot write, run queries, or start sessions — it is a library
card, not the harness. Treat user_confirmed facts as authoritative; treat
proposed facts as unverified hypotheses.
"""


def build_knowledge_server(library_root: Path) -> Any:
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(name="grayson-knowledge", instructions=INSTRUCTIONS)
    knowledge_dir = library_root / "knowledge"
    views_dir = library_root / "views"
    workflows_dir = library_root / "workflows"
    checks_dir = library_root / "checks"

    def _err(e: Exception) -> dict:
        return {"error": str(e.args[0] if e.args else e), "type": type(e).__name__}

    @mcp.tool(
        description="Read the knowledge library entry for a table, including its "
        "base-descriptor completeness report."
    )
    def knowledge_show(table: str) -> dict:
        try:
            doc = KnowledgeStore(knowledge_dir).read(table)
            return {**doc, "completeness": completeness(doc)}
        except ValueError as e:
            return _err(e)

    @mcp.tool(description="Search the knowledge library (facts and glossary) for a term.")
    def knowledge_search(term: str) -> list[dict]:
        return KnowledgeStore(knowledge_dir).search(term)

    @mcp.tool(description="List every table documented in the knowledge library.")
    def knowledge_tables() -> list[str]:
        return KnowledgeStore(knowledge_dir).all_tables()

    @mcp.tool(
        description="List the team's QA workflow templates (built-in + library). "
        "library_problems reports library files that are not loadable and why."
    )
    def workflow_list() -> dict:
        return {
            "workflows": [
                {
                    "name": t.name,
                    "title": t.title,
                    "description": t.description.strip(),
                    "required_checks": t.required_check_keys(),
                    "suggested_guard_profile": t.suggested_guard_profile,
                }
                for t in list_workflows(workflows_dir)
            ],
            "library_problems": override_problems(workflows_dir),
        }

    @mcp.tool(description="Show a workflow template's setup inputs, checkpoints, and schema.")
    def workflow_show(name: str) -> dict:
        try:
            return get_workflow(name, workflows_dir).model_dump()
        except WorkflowNotFound as e:
            return _err(e)

    @mcp.tool(
        description="Registered QA views in the library: name, source tables, and "
        "where their DDL lives."
    )
    def views_list() -> list[dict]:
        return [v.model_dump() for v in ViewRegistry(views_dir).list()]

    @mcp.tool(
        description="External deterministic checks (Airflow, dbt, ...) on file in the "
        "library: latest result per check, failures and overdue runs called out."
    )
    def checks_status(tables: list[str] | None = None) -> dict:
        return ChecksStore(checks_dir).summary(tables)

    @mcp.tool(description="One external check's run history, newest first.")
    def checks_show(check_id: str) -> list[dict]:
        return [r.model_dump() for r in ChecksStore(checks_dir).history(check_id)]

    @mcp.tool(
        description="Search the team's published records — accepted findings and "
        "verified fixes from past sessions — for how similar problems were "
        "diagnosed and what fixed them. Returns summaries; use records_get for "
        "the full record."
    )
    def records_search(term: str = "", kind: str | None = None, limit: int = 20) -> list[dict]:
        from grayson.records import search_library_records

        try:
            return search_library_records(library_root / "records", term, kind, limit)
        except ValueError as e:
            return [_err(e)]

    @mcp.tool(
        description="Fetch one published team record in full (a finding or "
        "proposal, including its payload and verification)."
    )
    def records_get(session_id: str, record_id: str) -> dict:
        from grayson.records import get_library_record

        item = get_library_record(library_root / "records", session_id, record_id)
        return item or {"error": f"no published record '{record_id}' from '{session_id}'"}

    @mcp.tool(
        description="Where this library lives and how fresh the local copy is "
        "(behind/ahead of its git remote)."
    )
    def library_info() -> dict:
        from grayson.library import library_admins, repo_status

        return {
            "mode": "knowledge-only (read-only)",
            **repo_status(library_root),
            "admins": library_admins(library_root),
        }

    return mcp


def serve_knowledge_stdio(library_root: Path) -> None:
    """Serve the knowledge-only surface, best-effort refreshing the clone first."""
    from grayson.library import library_pull_path

    library_pull_path(library_root)  # stale knowledge misleads; failure is non-fatal
    build_knowledge_server(library_root).run(transport="stdio")
