"""The briefing: ranked, capped, annotated knowledge at session start."""

from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from grayson.cli import app
from grayson.knowledge import KnowledgeStore, StandingContext
from grayson.knowledge.briefing import briefing_hints, build_briefing
from grayson.mcp.server import build_server

T = "DB.S.T"
runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _call(server, name: str, args: dict):
    result = asyncio.run(server.call_tool(name, args))
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    content = getattr(result, "content", None) or []
    if content and getattr(content[0], "text", None):
        return json.loads(content[0].text)
    return structured


@pytest.fixture
def ks(workspace):
    return KnowledgeStore(workspace.knowledge_dir)


def test_briefing_ranks_caps_hides_and_folds(ks):
    ks.set_profile(T, {"columns": [{"name": "AMOUNT", "description": "d"}, {"name": "ID"}]})
    ks.add_fact(T, "confirmed one", fact_id="a")
    ks.confirm_fact(T, "a")
    ks.add_fact(T, "a hunch", fact_id="b")
    ks.add_fact(T, "inferred", fact_id="c", status="data_inferred")
    ks.add_fact(T, "gone", fact_id="d")
    ks.retire_fact(T, "d", by="user", reason="r")
    ks.add_fact(
        T,
        "Verified fix: dedupe",
        fact_id="e",
        status="data_inferred",
        kind="verified_fix",
        anchors=[{"kind": "record", "session": "s", "id": "p_001", "record_kind": "proposal"}],
    )
    ks.add_fact(T, "AMOUNT is gross", fact_id="f")
    ks.sync_columns(T, [{"name": "ID", "type": "NUMBER"}])  # AMOUNT dropped: f is stale
    ctx = StandingContext()
    b = build_briefing(ks, [T], ctx, cap=2)[T]
    assert [x["id"] for x in b["facts"]] == ["a", "c"]
    assert b["omitted"] == 1 and b["hidden"] == {"stale": 1, "retired": 1}
    assert b["counts"] == {"current": 4, "unverified": 0, "stale": 1, "retired": 1}
    assert b["verified_fixes"]["count"] == 1 and "records_search" in b["verified_fixes"]["hint"]
    assert b["facts"][0]["role"] == "knowledge" and b["facts"][0]["standing"] == "current"
    full = build_briefing(ks, [T], ctx, cap=0)[T]
    assert [x["id"] for x in full["facts"]] == ["a", "c", "b"]
    assert full["facts"][2]["role"] == "hypothesis" and full["omitted"] == 0
    hints = briefing_hints({T: b})
    assert any("capped" in h and "DB.S.T (1)" in h for h in hints)
    assert any("stale facts hidden" in h for h in hints)


def test_briefing_surfaces_contested_unverified_and_questions(ks):
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:a", "kind": "dbt_model"})
    ks.add_fact(T, "old", fact_id="old")
    ks.confirm_fact(T, "old")
    ks.add_fact(T, "new", fact_id="new", supersedes="old", evidence=["q_1"])
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:b", "kind": "dbt_model"})
    ks.set_profile(T, {"open_questions": [f"q{i}?" for i in range(10)]})
    b = build_briefing(ks, [T], StandingContext(), cap=12)[T]
    assert b["contested"][0]["kind"] == "supersession"
    assert all(f["standing"] == "unverified" for f in b["facts"])
    assert "changed" in b["facts"][0]["standing_reason"]
    assert len(b["open_questions"]) == 8 and b["open_questions_omitted"] == 2
    hints = "\n".join(briefing_hints({T: b}))
    assert (
        "contested knowledge on DB.S.T (1)" in hints and "unverified facts on DB.S.T (2)" in hints
    )


def test_unreadable_table_briefs_as_an_error_not_a_crash(ks, workspace):
    path = workspace.knowledge_dir / "DB" / "S" / "BAD.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntable: [unclosed\n---\n", encoding="utf-8")
    b = build_briefing(ks, ["DB.S.BAD"], StandingContext(), cap=5)["DB.S.BAD"]
    assert b["facts"] == [] and "front-matter" in b["error"]


def test_mcp_session_start_briefs(workspace, fake_snow_env, ks):
    for i in range(14):
        ks.add_fact("DB.S.T1", f"fact number {i}", fact_id=f"f{i}")
    server = build_server(workspace)
    out = _call(server, "session_start", {"workflow": "table-health", "tables": ["DB.S.T1"]})
    assert len(out["knowledge"]["DB.S.T1"]) == 12
    briefing = out["knowledge_briefing"]["DB.S.T1"]
    assert briefing["omitted"] == 2 and "facts" not in briefing
    assert out["knowledge_gaps"] == [] and out["knowledge_policy"]["trust"] == "data_inferred"
    assert "capped" in out["hint"] and "knowledge_show(table)" in out["hint"]
    fact = out["knowledge"]["DB.S.T1"][0]
    assert fact["role"] == "hypothesis" and fact["standing"] == "current"


def test_cli_session_start_briefs(workspace, fake_snow_env, ks):
    for i in range(3):
        ks.add_fact("DB.S.T1", f"fact number {i}", fact_id=f"f{i}")
    out = invoke("session", "start", "--workflow", "table-health", "--table", "DB.S.T1")
    assert len(out["knowledge"]["DB.S.T1"]) == 3
    assert out["knowledge_briefing"]["DB.S.T1"]["omitted"] == 0
    assert out["knowledge_policy"]["actions"]["retire"] == "agent"
    assert out["knowledge_gaps"] == []
