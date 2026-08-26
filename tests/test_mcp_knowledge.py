"""Knowledge-only MCP server: read-only by construction, workspace-free."""

from __future__ import annotations

import asyncio
import json

import pytest

from grayson.knowledge import KnowledgeStore
from grayson.library import init_library
from grayson.mcp.knowledge_server import build_knowledge_server


@pytest.fixture
def library(tmp_path):
    return init_library(tmp_path / "qa-library")


@pytest.fixture
def server(library):
    return build_knowledge_server(library)


def _list_tools(server) -> set[str]:
    tools = asyncio.run(server.list_tools())
    return {getattr(t, "name", None) for t in tools}


def _call(server, name: str, args: dict):
    result = asyncio.run(server.call_tool(name, args))
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    content = getattr(result, "content", None) or []
    if content and getattr(content[0], "text", None):
        return json.loads(content[0].text)
    return structured


def test_only_read_tools_registered(server):
    names = _list_tools(server)
    assert names == {
        "knowledge_show",
        "knowledge_search",
        "knowledge_tables",
        "workflow_list",
        "workflow_show",
        "views_list",
        "checks_status",
        "checks_show",
        "records_search",
        "records_get",
        "library_info",
    }
    # the write/session surface must not exist here at all
    assert not names & {"knowledge_add", "knowledge_set", "session_start", "query_run"}


def test_knowledge_reads_from_library(server, library):
    KnowledgeStore(library / "knowledge").add_fact(
        "DB.S.ORDERS", "grain is one row per order", fact_id="grain"
    )
    doc = _call(server, "knowledge_show", {"table": "DB.S.ORDERS"})
    assert doc["facts"][0]["id"] == "grain"
    assert "completeness" in doc
    hits = _call(server, "knowledge_search", {"term": "grain"})
    assert hits and hits[0]["fact_id"] == "grain"
    assert _call(server, "knowledge_tables", {}) == ["DB.S.ORDERS"]


def test_workflows_include_builtins_and_library(server, library):
    (library / "workflows" / "custom.yaml").write_text(
        "name: custom\ntitle: Custom\ndescription: d\n"
        "required_checks:\n  - key: one\n    title: One\n    description: d\n",
        encoding="utf-8",
    )
    result = _call(server, "workflow_list", {})
    names = {w["name"] for w in result["workflows"]}
    assert "bug-hunter" in names and "custom" in names
    assert result["library_problems"] == []
    shown = _call(server, "workflow_show", {"name": "custom"})
    assert shown["required_checks"][0]["key"] == "one"


def test_records_served_from_library(server, library):
    record_dir = library / "records" / "s_20260826_0001"
    record_dir.mkdir(parents=True)
    (record_dir / "f_001.json").write_text(
        json.dumps(
            {
                "kind": "finding",
                "session_id": "s_20260826_0001",
                "id": "f_001",
                "ts": "2026-08-26T10:00:00Z",
                "title": "Join fan-out on ORDERS",
                "severity": "high",
                "accepted": True,
                "summary": "Fan-out duplicated revenue rows.",
                "author": "kcg",
                "payload": {"summary": "Fan-out duplicated revenue rows."},
                "record": {"fid": "f_001", "payload": {"summary": "Fan-out duplicated rows."}},
            }
        ),
        encoding="utf-8",
    )
    hits = _call(server, "records_search", {"term": "fan-out"})
    assert len(hits) == 1 and hits[0]["author"] == "kcg"
    full = _call(server, "records_get", {"session_id": "s_20260826_0001", "record_id": "f_001"})
    assert full["record"]["fid"] == "f_001"
    missing = _call(server, "records_get", {"session_id": "s_x", "record_id": "f_009"})
    assert "error" in missing


def test_checks_and_views_and_info(server, library):
    status = _call(server, "checks_status", {})
    assert status["failing"] == []
    assert _call(server, "views_list", {}) == []
    info = _call(server, "library_info", {})
    assert info["mode"].startswith("knowledge-only")
    assert info["exists"] is True and info["is_git"] is False
