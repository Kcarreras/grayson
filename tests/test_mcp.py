"""MCP server: verify it builds, registers tools, and tools invoke the core."""

from __future__ import annotations

import asyncio

import pytest

from seekql.mcp.server import build_server


@pytest.fixture
def server(workspace, fake_snow_env):
    return build_server(workspace)


def _list_tools(server) -> set[str]:
    tools = asyncio.run(server.list_tools())
    return {getattr(t, "name", None) for t in tools}


def _call(server, name: str, args: dict):
    import json

    result = asyncio.run(server.call_tool(name, args))
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]  # scalars/lists wrapped as {"result": ...}
    # dict returns arrive as JSON text content
    content = getattr(result, "content", None) or []
    if content and getattr(content[0], "text", None):
        return json.loads(content[0].text)
    return structured


def test_server_builds(server):
    assert server is not None


def test_tools_registered(server):
    names = _list_tools(server)
    expected = {
        "workflow_list",
        "workflow_show",
        "session_start",
        "session_status",
        "session_readiness",
        "session_advance",
        "query_run",
        "query_check",
        "query_log",
        "cache_find_tool",
        "checkpoint_list",
        "checkpoint_complete",
        "finding_add",
        "finding_list",
        "intervention_request",
        "intervention_check",
        "intervention_list",
        "proposal_add",
        "proposal_list",
        "proposal_verify",
        "knowledge_show",
        "knowledge_add",
        "knowledge_search",
        "views_check",
    }
    assert expected <= names, f"missing: {expected - names}"


def test_workflow_list_tool(server):
    result = _call(server, "workflow_list", {})
    assert any(t["name"] == "bug-hunter" for t in result)


def test_session_lifecycle_via_mcp(server, workspace):
    started = _call(server, "session_start", {"workflow": "table-health", "tables": ["DB.S.T1"]})
    sid = started["session"]["id"]
    assert started["required_checks"]
    assert "view_coverage" in started

    # guard check via MCP
    check = _call(server, "query_check", {"session_id": sid, "sql": "DROP TABLE DB.S.T1"})
    assert check["allowed"] is False

    # run a query (fake snow), then complete a checkpoint with its evidence
    run = _call(server, "query_run", {"session_id": sid, "sql": "SELECT * FROM DB.S.T1"})
    assert run["status"] == "executed"

    checks = _call(server, "checkpoint_list", {"session_id": sid})
    key = checks[0]["key"]
    done = _call(
        server,
        "checkpoint_complete",
        {"session_id": sid, "key": key, "evidence": [run["qid"]], "note": "ok"},
    )
    assert done["status"] == "complete"


def test_knowledge_via_mcp(server, workspace):
    added = _call(
        server,
        "knowledge_add",
        {"table": "DB.S.T", "fact": "id is a surrogate key", "status": "proposed"},
    )
    assert added["status"] == "proposed"
    hits = _call(server, "knowledge_search", {"term": "surrogate"})
    assert hits and hits[0]["source"] == "DB.S.T"


def test_evidence_enforced_via_mcp(server, workspace):
    started = _call(server, "session_start", {"workflow": "bug-hunter", "tables": ["DB.S.T1"]})
    sid = started["session"]["id"]
    # completing a checkpoint with fake evidence must fail
    out = _call(
        server,
        "checkpoint_complete",
        {"session_id": sid, "key": "replicate_anomaly", "evidence": ["q_9999"]},
    )
    assert "error" in out
