"""View library integration: scope entry, evidence via views, staleness, auto-registration."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from conftest import FakeExecutor
from seekql.cli import app as cli_app
from seekql.core import engine
from seekql.core import proposals as proposals_engine
from seekql.core.proposals import ProposalError
from seekql.core.run import run_statement
from seekql.core.session import Session
from seekql.views import ViewEntry, ViewRegistry

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(cli_app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def registry(workspace) -> ViewRegistry:
    reg = ViewRegistry(workspace.views_dir)
    reg.register(
        ViewEntry(
            name="V_T1_DAILY",
            purpose="daily rollup of T1",
            source_tables=["DB.S.T1"],
            base_files=["models/t1.sql"],
        ),
        ddl="CREATE VIEW V_T1_DAILY AS SELECT 1",
        source_last_altered={"DB.S.T1": "2026-08-20 00:00:00"},
    )
    return reg


# -- staleness baseline --------------------------------------------------


def test_register_stores_baseline_and_coverage_detects_stale(registry):
    entry = registry.get("V_T1_DAILY")
    assert entry.source_last_altered == {"DB.S.T1": "2026-08-20 00:00:00"}
    fresh = registry.coverage_check(["DB.S.T1"], {"DB.S.T1": "2026-08-20 00:00:00"})
    assert [v["name"] for v in fresh["reuse"]] == ["V_T1_DAILY"] and not fresh["refresh"]
    moved = registry.coverage_check(["DB.S.T1"], {"DB.S.T1": "2026-08-24 09:00:00"})
    assert [v["name"] for v in moved["refresh"]] == ["V_T1_DAILY"] and not moved["reuse"]
    assert "changed since view was built" in moved["refresh"][0]["reasons"][0]


def test_cli_register_captures_baseline_via_connection(workspace, fake_snow_env):
    out = invoke(
        "views", "register", "V_AUTO", "--source", "DB.S.T1",
        "--purpose", "auto-baseline test",
    )  # fmt: skip
    assert out["staleness_baseline_captured"] is True
    assert out["source_last_altered"] == {"DB.S.T1": "2026-08-20 00:00:00"}


def test_cli_views_check_freshness_flags_stale(workspace, fake_snow_env):
    ViewRegistry(workspace.views_dir).register(
        ViewEntry(name="V_OLD", source_tables=["DB.S.T1"]),
        source_last_altered={"DB.S.T1": "2026-01-01 00:00:00"},  # long before fake snow's value
    )
    checked = invoke("views", "check", "--table", "DB.S.T1", "--check-freshness")
    assert [v["name"] for v in checked["refresh"]] == ["V_OLD"]
    # without the flag, no current values exist, so the view sits in reuse
    unchecked = invoke("views", "check", "--table", "DB.S.T1")
    assert [v["name"] for v in unchecked["reuse"]] == ["V_OLD"]


# -- scope entry at session start ----------------------------------------


def test_session_start_scopes_matching_views_strict_mode(workspace, fake_snow_env, registry):
    started = invoke(
        "session", "start", "--workflow", "table-health", "--table", "DB.S.T1",
        "--guard-profile", "moderate", "--strict-scope",
    )  # fmt: skip
    assert started["views_in_scope"] == ["V_T1_DAILY"]
    sid = started["session"]["id"]
    ok = invoke("guard", "check", sid, "-q", "SELECT * FROM V_T1_DAILY")
    assert ok["allowed"] is True
    blocked = invoke("guard", "check", sid, "-q", "SELECT * FROM OTHER.PLACE.TBL")
    assert blocked["allowed"] is False and blocked["rule"] == "out_of_scope"


def test_evidence_via_view_counts_for_checkpoints(workspace, registry):
    from seekql.config import GuardSettings

    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    from seekql.views import enter_session_scope

    enter_session_scope(registry, s, ["DB.S.T1"])
    qid = run_statement(s, "SELECT * FROM V_T1_DAILY", executor=FakeExecutor())["qid"]
    key = s.checkpoints()[0]["key"]
    cp = engine.complete_checkpoint(s, key, [qid], "checked via library view", "agent")
    assert cp["status"] == "complete"


def test_views_use_mid_session(workspace, fake_snow_env, registry):
    registry.register(ViewEntry(name="V_OTHER", source_tables=["DB.S.OTHER"]))
    started = invoke(
        "session", "start", "--workflow", "table-health", "--table", "DB.S.T1",
        "--guard-profile", "moderate", "--strict-scope",
    )  # fmt: skip
    sid = started["session"]["id"]
    assert "V_OTHER" not in started["views_in_scope"]  # doesn't match targets
    used = invoke("views", "use", sid, "V_OTHER")
    assert "V_OTHER" in used["scope"]
    assert invoke("guard", "check", sid, "-q", "SELECT * FROM V_OTHER")["allowed"] is True
    # arbitrary (unregistered) names cannot be scoped in
    bad = runner.invoke(cli_app, ["views", "use", sid, "PROD.SECRET.USERS"])
    assert bad.exit_code == 1
    assert "not in the view registry" in bad.output


# -- auto-registration on applied ddl_snippet ----------------------------


def _view_proposal_payload():
    return {
        "ddl": "CREATE VIEW V_T1_QA AS SELECT ID FROM DB.S.T1",
        "view_name": "V_T1_QA",
        "source_tables": ["db.s.t1"],
        "base_files": ["models/t1.sql"],
        "purpose": "QA rollup",
        "rationale": "needed to isolate the anomaly",
    }


def test_applied_view_proposal_autoregisters_and_scopes(workspace, fake_snow_env):
    from seekql.config import GuardSettings

    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    p = proposals_engine.record_proposal(
        s, "ddl_snippet", "create QA view", _view_proposal_payload(), None
    )
    # not registered while merely proposed/approved — only once actually applied
    assert ViewRegistry(workspace.views_dir).get("V_T1_QA") is None
    proposals_engine.decide(s, p["pid"], approve=True)
    assert ViewRegistry(workspace.views_dir).get("V_T1_QA") is None
    out = proposals_engine.mark_applied(s, p["pid"])
    reg = out["view_registered"]
    assert reg["name"] == "V_T1_QA" and reg["in_session_scope"] is True
    assert reg["staleness_baseline_captured"] is True  # via fake snow metadata
    entry = ViewRegistry(workspace.views_dir).get("V_T1_QA")
    assert entry.source_tables == ["DB.S.T1"]
    assert entry.source_last_altered == {"DB.S.T1": "2026-08-20 00:00:00"}
    assert (workspace.views_dir / entry.ddl_path).read_text().startswith("CREATE VIEW")
    assert "V_T1_QA" in s.scope_tables  # verification queries against it count
    assert any(e["type"] == "view_registered" for e in s.events(10))


def test_unapproved_view_proposal_cannot_register(workspace):
    from seekql.config import GuardSettings

    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    p = proposals_engine.record_proposal(
        s, "ddl_snippet", "create QA view", _view_proposal_payload(), None
    )
    with pytest.raises(ProposalError, match="must be approved"):
        proposals_engine.mark_applied(s, p["pid"])
    assert ViewRegistry(workspace.views_dir).get("V_T1_QA") is None


def test_bad_view_name_rejected_at_proposal_time(workspace):
    from seekql.config import GuardSettings

    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    payload = {**_view_proposal_payload(), "view_name": "../escape"}
    with pytest.raises(ProposalError, match="invalid view_name"):
        proposals_engine.record_proposal(s, "ddl_snippet", "bad", payload, None)


def test_mcp_views_use_and_freshness_param(workspace, fake_snow_env, registry):
    import asyncio

    from seekql.mcp.server import build_server

    server = build_server(workspace)

    def call(name, args):
        result = asyncio.run(server.call_tool(name, args))
        return json.loads(result.content[0].text)

    started = call("session_start", {"workflow": "table-health", "tables": ["DB.S.T1"]})
    assert started["views_in_scope"] == ["V_T1_DAILY"]
    registry.register(ViewEntry(name="V_EXTRA", source_tables=["DB.S.ELSEWHERE"]))
    used = call("views_use", {"session_id": started["session"]["id"], "names": ["V_EXTRA"]})
    assert "V_EXTRA" in used["scope"]
    denied = call("views_use", {"session_id": started["session"]["id"], "names": ["NOT_A_VIEW"]})
    assert "not in the view registry" in denied["error"]
