"""The fact lifecycle: retire, supersede, restore, dismiss, resolve — the
evidence rule in the store, the policy in the actions layer, one commit per
action in a team library."""

from __future__ import annotations

import subprocess

import pytest

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.config_edit import set_values
from grayson.core import engine
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.identity import set_user_id
from grayson.knowledge import KnowledgeStore, StandingContext, actions, effective_standing
from grayson.knowledge.actions import ActionRefused
from grayson.library import link_library, set_library_policy

T = "DB.S.T"


@pytest.fixture
def ks(workspace):
    return KnowledgeStore(workspace.knowledge_dir)


@pytest.fixture
def session(workspace):
    s = Session.create(
        workspace,
        workflow="bug-hunter",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    return s


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


@pytest.fixture
def team_lib(workspace, tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    assert _git("init", "--bare", str(origin)).returncode == 0
    clone = tmp_path / "lib-clone"
    link_library(workspace, str(origin), clone, auto_push=True)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    workspace.reload_config()
    return clone


def _preset(workspace, name: str) -> None:
    set_values(workspace.root, {"knowledge.policy": name})
    workspace.reload_config()


# -- the store: evidence rule, execution rule -----------------------------------


def test_agent_retire_needs_evidence_and_a_person_needs_a_reason(ks):
    ks.add_fact(T, "x", fact_id="x")
    with pytest.raises(ValueError, match="evidence"):
        ks.retire_fact(T, "x", by="agent")
    with pytest.raises(ValueError, match="reason"):
        ks.retire_fact(T, "x", by="user")
    f = ks.retire_fact(T, "x", by="agent", evidence=["q_0002"])
    assert f["standing"] == "retired" and f["retired_by"] == "agent"
    assert "q_0002" in f["evidence"] and f["standing_reason"].startswith("retired by agent")
    with pytest.raises(ValueError, match="already"):
        ks.retire_fact(T, "x", by="user", reason="again")
    with pytest.raises(KeyError):
        ks.retire_fact(T, "nope", by="user", reason="r")


def test_restore_reanchors_and_clears(ks):
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:a", "kind": "dbt_model"})
    ks.add_fact(T, "x", fact_id="x")
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:b", "kind": "dbt_model"})
    doc = ks.read(T)
    assert effective_standing(doc["facts"][0], doc, StandingContext())[0] == "unverified"
    f = ks.restore_fact(T, "x", by="user")
    assert f["restored_by"] == "user" and f["restored_at"]
    assert any(a.get("hash") == "sha256:b" for a in f["anchors"])
    doc = ks.read(T)
    assert effective_standing(doc["facts"][0], doc, StandingContext())[0] == "current"


def test_supersession_is_pending_until_a_human_confirms(ks):
    ks.add_fact(T, "old", fact_id="old")
    ks.confirm_fact(T, "old")
    with pytest.raises(ValueError, match="evidence"):
        ks.add_fact(T, "new", fact_id="new", supersedes="old")  # an agent, no evidence
    with pytest.raises(KeyError):
        ks.add_fact(T, "new", fact_id="new", supersedes="ghost", evidence=["q_1"])
    new = ks.add_fact(T, "new", fact_id="new", supersedes="old", evidence=["q_0001"])
    assert new["supersedes"] == "old" and ks.fact(T, "old")["superseded_by"] is None
    # a proposed fact cannot displace a confirmed one under data_inferred trust
    with pytest.raises(ValueError, match="cannot displace"):
        ks.execute_supersession(T, "new", by="agent", trust="data_inferred")
    assert ks.fact(T, "old")["superseded_by"] is None
    # the human's confirm executes it
    confirmed = ks.confirm_fact(T, "new")
    assert confirmed["status"] == "user_confirmed"
    old = ks.fact(T, "old")
    assert old["superseded_by"] == "new" and old["standing"] == "retired"
    assert old["retired_by"] == "user" and old["standing_reason"] == "superseded by new"
    # supersede the head of the chain, not a retired predecessor
    with pytest.raises(ValueError, match="head of the chain"):
        ks.add_fact(T, "newer", fact_id="newer", supersedes="old", evidence=["q_0002"])


def test_agent_supersession_executes_when_trust_admits_it(ks):
    ks.add_fact(T, "old", fact_id="old")
    ks.confirm_fact(T, "old")
    ks.add_fact(T, "new", fact_id="new", supersedes="old", evidence=["q_1"], status="data_inferred")
    out = ks.execute_supersession(T, "new", by="agent", trust="data_inferred")
    assert out["superseded"]["superseded_by"] == "new"
    assert out["superseded"]["retired_by"] == "agent"
    # first wins: nothing re-points an already superseded fact
    ks.add_fact(T, "third", fact_id="third", supersedes="new", evidence=["q_2"])
    doc = ks.read(T)
    doc["facts"][2]["supersedes"] = "old"  # hand-edit the target back to a superseded fact
    ks.save(T, doc)
    with pytest.raises(ValueError, match="already superseded"):
        ks.execute_supersession(T, "third", by="user")


def test_restoring_a_superseded_fact_marks_the_pair_compatible(ks):
    ks.add_fact(T, "old", fact_id="old")
    ks.add_fact(T, "new", fact_id="new", supersedes="old", evidence=["q_1"])
    ks.confirm_fact(T, "new")
    assert ks.fact(T, "old")["superseded_by"] == "new"
    ks.restore_fact(T, "old", by="user")
    old, new = ks.fact(T, "old"), ks.fact(T, "new")
    assert old["superseded_by"] is None and old["standing"] is None
    assert "new" in old["compatible_with"] and "old" in new["compatible_with"]
    assert ks.read(T)["resolutions"][0]["facts"] == ["new", "old"]


def test_dismiss_question_and_resolve_pair(ks):
    ks.set_profile(T, {"open_questions": ["Is AMOUNT signed?", "What is the grain?"]})
    with pytest.raises(ValueError, match="reason"):
        ks.dismiss_question(T, "signed", "", by="agent")
    out = ks.dismiss_question(T, "signed", "AMOUNT was dropped", by="agent")
    assert out["question"] == "Is AMOUNT signed?" and out["open_questions_left"] == 1
    doc = ks.read(T)
    assert doc["open_questions"] == ["What is the grain?"]
    assert doc["retired_questions"][0]["by"] == "agent"
    ks.add_fact(T, "a", fact_id="a")
    ks.add_fact(T, "b", fact_id="b")
    with pytest.raises(ValueError):
        ks.resolve_contested(T, "a", "a")
    entry = ks.resolve_contested(T, "a", "b", by="user", note="both hold")
    assert entry["facts"] == ["a", "b"] and ks.fact(T, "a")["compatible_with"] == ["b"]


def test_reanchor_all_leaves_retired_alone(ks):
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:a", "kind": "dbt_model"})
    ks.add_fact(T, "x", fact_id="x")
    ks.add_fact(T, "y", fact_id="y")
    ks.retire_fact(T, "y", by="user", reason="gone")
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:b", "kind": "dbt_model"})
    out = ks.reanchor(T)
    assert out["reanchored"] == ["x"]
    assert ks.fact(T, "y")["standing"] == "retired"


# -- the actions layer: policy -------------------------------------------------------


def test_actions_follow_the_policy(workspace, ks):
    ks.add_fact(T, "x", fact_id="x")
    _preset(workspace, "propose")
    with pytest.raises(ActionRefused) as refused:
        actions.retire(workspace, T, "x", evidence=["q_0001"], actor="agent")
    assert "propose" in str(refused.value) and "user action" in str(refused.value)
    assert refused.value.policy.actor("retire") == "user"
    # a person is never refused
    out = actions.retire(workspace, T, "x", reason="wrong", actor="user", surface="console")
    assert out["fact"]["standing"] == "retired" and out["fact"]["retired_by"] == "user"
    _preset(workspace, "curate")
    ks.add_fact(T, "y", fact_id="y")
    out = actions.retire(workspace, T, "y", evidence=["q_0001"], actor="agent")
    assert out["fact"]["retired_by"] == "agent" and out["policy_actor"] == "agent"
    with pytest.raises(ActionRefused, match="judgment"):
        actions.restore(workspace, T, "y", actor="agent")
    _preset(workspace, "autonomous")
    assert actions.restore(workspace, T, "y", actor="agent")["fact"]["restored_by"] == "agent"


def test_supersede_action_pending_or_executed_by_policy(workspace, ks):
    ks.add_fact(T, "old", fact_id="old")
    ks.confirm_fact(T, "old")
    _preset(workspace, "propose")
    out = actions.supersede(workspace, T, "old", "new text", evidence=["q_1"], actor="agent")
    assert out["executed"] is False and "user action" in out["pending"]
    assert ks.fact(T, "old")["superseded_by"] is None
    assert ks.fact(T, out["fact"]["id"])["supersedes"] == "old"
    _preset(workspace, "curate")
    ks.add_fact(T, "old2", fact_id="old2")
    ks.confirm_fact(T, "old2")
    hyp = actions.supersede(workspace, T, "old2", "a hypothesis", evidence=["q_2"], actor="agent")
    assert hyp["executed"] is False and "cannot displace" in hyp["pending"]
    ks.add_fact(T, "old3", fact_id="old3")
    ks.confirm_fact(T, "old3")
    inferred = actions.supersede(
        workspace, T, "old3", "measured", evidence=["q_3"], status="data_inferred", actor="agent"
    )
    assert inferred["executed"] is True
    assert ks.fact(T, "old3")["superseded_by"] == inferred["fact"]["id"]
    # a person superseding confirms the correction in the same step
    human = actions.supersede(workspace, T, "old", "the truth", actor="user", surface="console")
    assert human["executed"] and human["fact"]["status"] == "user_confirmed"
    assert ks.fact(T, "old")["superseded_by"] == human["fact"]["id"]


def test_session_evidence_must_have_executed_there(workspace, ks, session):
    ks.add_fact("DB.S.T1", "x", fact_id="x")
    with pytest.raises(ValueError, match="did not execute"):
        actions.retire(
            workspace, "DB.S.T1", "x", evidence=["q_0099"], actor="agent", session_id=session.id
        )
    qid = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    out = actions.retire(
        workspace, "DB.S.T1", "x", evidence=[qid, "iid_7"], actor="agent", session_id=session.id
    )
    assert out["fact"]["standing"] == "retired"


def test_dismiss_and_resolve_actions_gated(workspace, ks):
    ks.set_profile(T, {"open_questions": ["moot?"]})
    ks.add_fact(T, "a", fact_id="a")
    ks.add_fact(T, "b", fact_id="b")
    _preset(workspace, "curate")
    assert (
        actions.dismiss_question(workspace, T, "moot", "answered", actor="agent")["by"] == "agent"
    )
    with pytest.raises(ActionRefused):
        actions.resolve(workspace, T, "a", "b", actor="agent")
    assert actions.resolve(workspace, T, "a", "b", note="both", actor="user")["by"] == "user"


def test_show_carries_standing_contested_and_policy(workspace, ks):
    ks.add_fact(T, "old", fact_id="old")
    ks.confirm_fact(T, "old")
    ks.add_fact(T, "new", fact_id="new", supersedes="old", evidence=["q_1"])
    out = actions.show(workspace, T)
    assert {f["id"]: f["standing"] for f in out["facts"]} == {"old": "current", "new": "current"}
    assert out["contested"][0]["kind"] == "supersession"
    assert out["policy"]["actions"]["retire"] == "agent" and out["completeness"]


# -- one commit per action ---------------------------------------------------------


def test_lifecycle_action_is_one_attributed_commit(workspace, team_lib):
    set_user_id("kcg")
    set_library_policy(workspace, preset="curate")  # no admins yet: the linking human may
    ks = KnowledgeStore(workspace.knowledge_dir)
    ks.add_fact(T, "x", fact_id="x")
    out = actions.retire(workspace, T, "x", evidence=["q_1"], actor="agent", surface="mcp")
    assert out["library_sync"]["committed"] and out["library_sync"]["ok"]
    log = _git("log", "-1", "--format=%B", cwd=team_lib).stdout
    assert "retire x on DB.S.T" in log
    assert "Grayson-User: kcg" in log and "Grayson-Via: mcp-agent" in log
    # only the doc's path rode along
    files = _git("show", "--name-only", "--format=", "HEAD", cwd=team_lib).stdout.split()
    assert files == ["knowledge/DB/S/T.md"]
