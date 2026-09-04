"""`knowledge verify`: re-run a verified fix's after-query and compare."""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core import engine, proposals
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.knowledge import KnowledgeStore, StandingContext, effective_standing
from grayson.knowledge.verify import verify_table


def _session(workspace):
    s = Session.create(
        workspace,
        workflow="bug-hunter",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    return s


def _verified_fix(session) -> str:
    qid = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    fid = engine.record_finding(
        session,
        {
            "title": "Dup rows",
            "severity": "high",
            "confidence": "high",
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "Duplicate rows appear in the output table.",
            "evidence": [qid],
            "extra": {
                "resolution": "root_caused",
                "root_cause": "join fan-out",
                "blast_radius": "1000 rows",
                "alternatives_tested": "two ruled out",
            },
        },
    )["fid"]
    session.accept_finding(fid)
    p = proposals.record_proposal(session, "ddl_snippet", "fix join", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    after = run_statement(
        session, "SELECT * FROM DB.S.T1 WHERE dup > 1", executor=FakeExecutor(rows=[])
    )["qid"]
    out = proposals.verify(session, p["pid"], qid, after, "pass", "anomaly gone")
    return out["knowledge_facts"][0]["fact_id"]


@pytest.fixture
def fixed(workspace):
    session = _session(workspace)
    fact_id = _verified_fix(session)
    return session, fact_id


def test_verified_fix_fact_is_anchored_to_its_record(workspace, fixed):
    session, fact_id = fixed
    fact = KnowledgeStore(workspace.knowledge_dir).fact("DB.S.T1", fact_id)
    assert fact["kind"] == "verified_fix"
    assert fact["anchors"][0] == {
        "kind": "record",
        "session": session.id,
        "id": fact["anchors"][0]["id"],
        "record_kind": "proposal",
    }


def test_verify_holds_then_differs_then_holds(workspace, fixed):
    session, fact_id = fixed
    ks = KnowledgeStore(workspace.knowledge_dir)
    later = _session(workspace)
    out = verify_table(workspace, "DB.S.T1", later, executor=FakeExecutor(rows=[]))
    assert out["holds"] == 1 and out["differs"] == 0
    result = out["results"][0]
    assert result["fact_id"] == fact_id and result["rows"] == 0 and result["qid"]
    assert ks.fact("DB.S.T1", fact_id)["verified_at"]
    # the anomaly is back: the fact becomes unverified, and stays so on read
    out = verify_table(workspace, "DB.S.T1", later, executor=FakeExecutor(rows=[{"ID": 1}]))
    assert out["differs"] == 1
    fact = ks.fact("DB.S.T1", fact_id)
    assert fact["standing"] == "unverified" and fact["standing_by"] == "verify"
    assert "1 row(s) where the record has 0" in fact["standing_reason"]
    doc = ks.read("DB.S.T1")
    assert effective_standing(fact, doc, StandingContext())[0] == "unverified"
    # the re-run is an audited query in the verifying session
    assert any("knowledge verify" in (q["label"] or "") for q in later.query_log(limit=10))
    out = verify_table(workspace, "DB.S.T1", later, executor=FakeExecutor(rows=[]))
    assert out["holds"] == 1 and ks.fact("DB.S.T1", fact_id)["standing"] is None


def test_verify_skips_facts_with_nothing_to_rerun(workspace):
    ks = KnowledgeStore(workspace.knowledge_dir)
    ks.add_fact("DB.S.T1", "semantics only", fact_id="s")
    out = verify_table(workspace, "DB.S.T1", _session(workspace), executor=FakeExecutor())
    assert out["results"] == [] and out["holds"] == 0


def _call(server, name: str, args: dict):
    result = asyncio.run(server.call_tool(name, args))
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    content = getattr(result, "content", None) or []
    if content and getattr(content[0], "text", None):
        return json.loads(content[0].text)
    return structured


def test_mcp_knowledge_verify_tool(workspace, fake_snow_env, fixed):
    from grayson.mcp.server import build_server

    session, fact_id = fixed
    server = build_server(workspace)
    out = _call(server, "knowledge_verify", {"table": "DB.S.T1", "session_id": session.id})
    assert out["table"] == "DB.S.T1" and out["results"][0]["fact_id"] == fact_id
    assert out["results"][0]["result"] in ("holds", "differs")
