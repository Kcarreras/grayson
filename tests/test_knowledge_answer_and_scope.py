"""`knowledge answer` (CLI + MCP) and setup inputs that add session scope."""

from __future__ import annotations

import asyncio
import json

from typer.testing import CliRunner

from grayson.cli import app
from grayson.core.session import Session
from grayson.knowledge import KnowledgeStore

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def invoke_err(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 1, result.output
    return json.loads(result.stderr or result.output)


def test_cli_knowledge_answer(workspace):
    ks = KnowledgeStore(workspace.knowledge_dir)
    ks.set_profile("DB.S.T1", {"open_questions": ["What is the grain?"]})
    out = invoke("knowledge", "answer", "DB.S.T1", "-q", "grain", "-a", "one row per order id")
    assert out["question"] == "What is the grain?"
    assert out["fact"]["status"] == "proposed"
    assert ks.read("DB.S.T1")["open_questions"] == []
    # no session existed at any point — that is the point
    err = invoke_err("knowledge", "answer", "DB.S.T1", "-q", "grain", "-a", "again")
    assert "no open question" in err["error"]


def test_mcp_knowledge_answer(workspace):
    from grayson.mcp.server import build_server

    ks = KnowledgeStore(workspace.knowledge_dir)
    ks.set_profile("DB.S.T1", {"open_questions": ["Who owns the nightly load?"]})
    server = build_server(workspace)
    result = asyncio.run(
        server.call_tool(
            "knowledge_answer",
            {"table": "DB.S.T1", "question": "nightly load", "answer": "the data-eng team"},
        )
    )
    content = getattr(result, "content", None) or []
    data = json.loads(content[0].text)
    assert data["question"] == "Who owns the nightly load?"
    assert data["fact"]["status"] == "proposed"  # confirmation stays with the user
    assert ks.read("DB.S.T1")["open_questions"] == []


def test_related_tables_input_joins_session_scope(workspace, fake_snow_env):
    out = invoke(
        "session",
        "start",
        "--workflow",
        "table-onboarding",
        "--table",
        "DB.S.ORDERS",
        "-I",
        "table=DB.S.ORDERS",
        "-I",
        "related_tables=DB.S.CUSTOMERS, db.s.order_items",
        "--skip-snapshot",
    )
    assert out["context_scope"] == ["DB.S.CUSTOMERS", "DB.S.ORDER_ITEMS"]
    s = Session(workspace, out["session"]["id"])
    assert {"DB.S.CUSTOMERS", "DB.S.ORDER_ITEMS"} <= s.scope_tables
    assert s.strict_scope is True  # onboarding stays strict; context is deliberate
    ev = next(e for e in s.events(20) if e["type"] == "scope_from_inputs")
    assert ev["payload"]["tables"] == ["DB.S.CUSTOMERS", "DB.S.ORDER_ITEMS"]


def test_scope_only_added_when_input_provided(workspace, fake_snow_env):
    out = invoke(
        "session",
        "start",
        "--workflow",
        "table-onboarding",
        "--table",
        "DB.S.ORDERS",
        "-I",
        "table=DB.S.ORDERS",
        "--skip-snapshot",
        "--new",
    )
    assert "context_scope" not in out
    s = Session(workspace, out["session"]["id"])
    assert s.scope_tables == {"DB.S.ORDERS"}
