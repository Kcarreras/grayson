"""MCP server: verify it builds, registers tools, and tools invoke the core."""

from __future__ import annotations

import asyncio
import json

import pytest

from grayson.mcp.server import build_server


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
        "proposal_applied",
        "proposal_verify",
        "session_list",
        "session_report",
        "worker_join",
        "cache_show",
        "cache_query",
        "knowledge_show",
        "knowledge_add",
        "knowledge_search",
        "views_check",
    }
    assert expected <= names, f"missing: {expected - names}"


def test_workflow_list_tool(server):
    result = _call(server, "workflow_list", {})
    assert any(t["name"] == "bug-hunter" for t in result["workflows"])
    assert result["library_problems"] == []


def test_workflow_list_reports_library_problems(server, workspace):
    (workspace.workflows_dir / "shadow.yaml").write_text(
        "name: table-health\ntitle: Shadow\n", encoding="utf-8"
    )
    result = _call(server, "workflow_list", {})
    assert len(result["library_problems"]) == 1
    assert "shadows the core workflow" in result["library_problems"][0]["problem"]


def test_session_start_setup_inputs(server, workspace):
    bad = _call(
        server,
        "session_start",
        {"workflow": "bug-hunter", "tables": ["DB.S.T1"], "inputs": {"nope": "x"}},
    )
    assert "unknown setup input" in bad["error"]
    started = _call(
        server,
        "session_start",
        {
            "workflow": "bug-hunter",
            "tables": ["DB.S.T1"],
            "inputs": {"anomaly_description": "dup rows"},
            "new": True,
        },
    )
    assert started["setup_inputs"] == {"anomaly_description": "dup rows"}
    assert "setup inputs not recorded" in started["hint"]
    from grayson.core.session import Session

    assert Session(workspace, started["session"]["id"]).setup_inputs() == {
        "anomaly_description": "dup rows"
    }


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


def test_cache_and_report_via_mcp(server, workspace):
    started = _call(server, "session_start", {"workflow": "table-health", "tables": ["DB.S.T1"]})
    sid = started["session"]["id"]
    run = _call(server, "query_run", {"session_id": sid, "sql": "SELECT * FROM DB.S.T1"})
    qid = run["qid"]

    shown = _call(server, "cache_show", {"session_id": sid, "qid": qid, "rows": 2})
    assert shown["qid"] == qid
    assert len(shown["preview"]) == 2

    local = _call(
        server, "cache_query", {"session_id": sid, "sql": f"SELECT COUNT(*) AS n FROM {qid}"}
    )
    assert local["rows"][0]["n"] == 5

    sessions = _call(server, "session_list", {})
    assert any(s["id"] == sid for s in sessions)

    report = _call(server, "session_report", {"session_id": sid})
    assert report["query_stats"]["by_status"]["executed"] == 1

    joined = _call(server, "worker_join", {"session_id": sid, "label": "w1"})
    assert joined["worker"].startswith("w-")


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


def test_checks_via_mcp(server, workspace):
    (workspace.checks_dir / "airflow.json").write_text(
        json.dumps(
            {
                "check_id": "t1_dupes",
                "status": "fail",
                "tables": ["DB.S.T1"],
                "run_at": "2026-08-24T06:00:00Z",
                "sql": "SELECT 1",
            }
        ),
        encoding="utf-8",
    )
    names = _list_tools(server)
    assert {"checks_status", "checks_show"} <= names
    status = _call(server, "checks_status", {"tables": ["DB.S.T1"]})
    assert [f["check_id"] for f in status["failing"]] == ["t1_dupes"]
    history = _call(server, "checks_show", {"check_id": "t1_dupes"})
    assert history[0]["sql"] == "SELECT 1"
    # session start surfaces the failing check as a lead
    started = _call(server, "session_start", {"workflow": "bug-hunter", "tables": ["DB.S.T1"]})
    assert started["external_checks"]["failing"][0]["check_id"] == "t1_dupes"
    assert "t1_dupes" in started.get("hint", "")


def test_no_agent_surface_for_ending_or_deleting(server):
    """Closing, abandoning, deleting a session, removing its published records,
    and changing the library admins are the user's — no MCP twin, on purpose."""
    names = _list_tools(server)
    forbidden = {
        "session_close",
        "session_abandon",
        "session_delete",
        "records_delete",
        "library_admins_add",
        "library_admins_remove",
    }
    assert not forbidden & names
    assert not any("abandon" in n or n.endswith("_delete") for n in names)
