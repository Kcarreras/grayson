"""The reconcile pass and the doctor's standing section."""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from grayson.cli import app
from grayson.config_edit import set_values
from grayson.identity import set_user_id
from grayson.knowledge import KnowledgeStore, actions
from grayson.knowledge.actions import ActionRefused
from grayson.knowledge.reconcile import reconcile_docs
from grayson.library import (
    library_doctor,
    link_library,
    push_library,
    reconcile_library,
    reconcile_root,
    write_library_settings,
)

T = "DB.S.T"
runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


@pytest.fixture
def ks(workspace):
    return KnowledgeStore(workspace.knowledge_dir)


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


def _drift_definition(ks):
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:a", "kind": "dbt_model"})
    ks.add_fact(T, "x", fact_id="x")
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:b", "kind": "dbt_model"})


def test_reconcile_materializes_then_clears(workspace, ks):
    _drift_definition(ks)
    policy = workspace.config.knowledge
    dry = reconcile_docs(ks, workspace.records_dir, policy, dry_run=True)
    assert dry["dry_run"] and dry["materialized"][0]["to"] == "unverified"
    assert dry["touched"] == ["knowledge/DB/S/T.md"] and dry["counts"]["unverified"] == 1
    assert ks.fact(T, "x")["standing"] is None  # a dry run writes nothing
    out = reconcile_docs(ks, workspace.records_dir, policy)
    fact = ks.fact(T, "x")
    assert fact["standing"] == "unverified" and fact["standing_by"] == "reconcile"
    assert "changed" in fact["standing_reason"] and fact["standing_at"]
    assert out["needs_human"][T]["unverified"][0]["fact_id"] == "x"
    again = reconcile_docs(ks, workspace.records_dir, policy)
    assert again["materialized"] == [] and again["touched"] == []  # idempotent
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:a", "kind": "dbt_model"})
    back = reconcile_docs(ks, workspace.records_dir, policy)
    assert back["materialized"][0]["to"] == "current"
    assert ks.fact(T, "x")["standing"] is None


def test_reconcile_never_touches_retired_or_status(workspace, ks):
    ks.add_fact(T, "keep", fact_id="keep", status="data_inferred")
    ks.add_fact(T, "gone", fact_id="gone")
    ks.retire_fact(T, "gone", by="user", reason="r")
    out = reconcile_docs(ks, workspace.records_dir, workspace.config.knowledge)
    assert out["materialized"] == [] and out["counts"] == {
        "current": 1,
        "unverified": 0,
        "stale": 0,
        "retired": 1,
    }
    assert ks.fact(T, "keep")["status"] == "data_inferred"
    assert ks.fact(T, "gone")["standing"] == "retired"


def test_reconcile_folds_and_retires_questions(workspace, ks):
    ks.set_profile(
        T,
        {
            "columns": [{"name": "AMOUNT", "description": "d"}],
            "open_questions": ["Is AMOUNT signed?", "What is the grain?", "what is the grain"],
        },
    )
    ks.sync_columns(T, [{"name": "ID", "type": "NUMBER"}])  # AMOUNT dropped
    out = reconcile_docs(ks, workspace.records_dir, workspace.config.knowledge)
    doc = ks.read(T)
    assert doc["open_questions"] == ["What is the grain?"]
    assert out["questions_folded"] == [{"table": T, "question": "what is the grain"}]
    retired = doc["retired_questions"][0]
    assert retired["by"] == "reconcile" and "AMOUNT" in retired["reason"]
    assert out["questions_retired"][0]["question"] == "Is AMOUNT signed?"


def test_reconcile_reports_contested_and_agent_actions(workspace, ks):
    ks.add_fact(T, "old", fact_id="old")
    ks.confirm_fact(T, "old")
    ks.add_fact(T, "new", fact_id="new", supersedes="old", evidence=["q_1"])
    ks.add_fact(T, "z", fact_id="z")
    ks.retire_fact(T, "z", by="agent", evidence=["q_2"])
    out = reconcile_docs(ks, workspace.records_dir, workspace.config.knowledge, dry_run=True)
    assert out["needs_human"][T]["contested"][0]["kind"] == "supersession"
    assert out["agent_actions"][T][0]["fact_id"] == "z"


def test_doctor_carries_standing_and_never_fails_on_it(workspace, ks):
    _drift_definition(ks)
    report = library_doctor(workspace)
    assert report["ok"] is True
    standing = report["standing"]
    assert standing["counts"]["unverified"] == 1 and standing["would_materialize"] == 1
    assert standing["needs_human"][T]["unverified"][0]["fact_id"] == "x"
    assert report["policy"]["preset"] == "curate"


def test_reconcile_library_is_one_commit_with_via_trailer(workspace, team_lib):
    set_user_id("kcg")
    write_library_settings(team_lib, {"knowledge_policy": "curate"})
    ks = KnowledgeStore(workspace.knowledge_dir)
    _drift_definition(ks)
    with pytest.raises(RuntimeError, match="dirty"):
        reconcile_library(workspace)
    assert reconcile_library(workspace, dry_run=True)["committed"] is False  # a dry run is fine
    push_library(workspace, "seed")
    out = reconcile_library(workspace)
    assert out["committed"] is True and out["materialized"][0]["fact_id"] == "x"
    log = _git("log", "-1", "--format=%B", cwd=team_lib).stdout
    assert "grayson library reconcile: 1 standing change(s)" in log
    assert "Grayson-Via: reconcile" in log and "Grayson-User: kcg" in log
    assert ks.fact(T, "x")["standing"] == "unverified"
    # nothing to do: nothing committed
    assert reconcile_library(workspace)["committed"] is False


def test_reconcile_root_for_ci_uses_the_librarys_own_policy(workspace, team_lib):
    write_library_settings(team_lib, {"knowledge_policy": "autonomous"})
    ks = KnowledgeStore(workspace.knowledge_dir)
    _drift_definition(ks)
    push_library(workspace, "seed")
    out = reconcile_root(team_lib, push=True)
    assert out["policy"]["preset"] == "autonomous" and out["committed"] and out["push"]["ok"]
    assert _git("status", "--porcelain", cwd=team_lib).stdout.strip() == ""


def test_reconcile_action_is_policy_gated_but_dry_run_is_free(workspace, ks):
    _drift_definition(ks)
    set_values(workspace.root, {"knowledge.policy": "propose"})
    workspace.reload_config()
    assert actions.reconcile(workspace, actor="agent", dry_run=True)["dry_run"]
    with pytest.raises(ActionRefused):
        actions.reconcile(workspace, actor="agent")
    assert actions.reconcile(workspace, actor="user", surface="cli")["materialized"]


def test_cli_reconcile(workspace, team_lib):
    set_user_id("kcg")
    _drift_definition(KnowledgeStore(workspace.knowledge_dir))  # the linked clone's store
    push_library(workspace, "seed")
    dry = invoke("library", "reconcile", "--dry-run")
    assert dry["dry_run"] and dry["committed"] is False
    real = invoke("library", "reconcile", "--library", str(team_lib))
    assert real["committed"] is True and real["materialized"]
