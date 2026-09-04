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


# -- upgrading a library written before standing existed --------------------------


def _strip_lifecycle(ks, *tables):
    """Leave the docs as an older grayson wrote them: no anchors, no kind."""
    for table in tables:
        doc = ks.read(table)
        for f in doc["facts"]:
            f["anchors"] = []
            f["kind"] = None
        ks.save(table, doc)


def _publish_record(workspace, sid: str, pid: str) -> None:
    folder = workspace.records_dir / sid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{pid}.json").write_text(
        json.dumps(
            {
                "format": 1,
                "kind": "proposal",
                "session_id": sid,
                "id": pid,
                "verdict": "pass",
                "title": "fix",
                "ts": "2026-06-01T12:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def test_anchor_missing_baselines_old_facts(workspace, ks):
    from grayson.knowledge import StandingContext, effective_standing

    ks.set_profile(T, {"columns": [{"name": "AMOUNT"}]})
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:a", "kind": "dbt_model"})
    ks.add_fact(T, "AMOUNT is gross", fact_id="amt")
    ks.add_fact(
        T,
        "Verified fix: dedupe",
        fact_id="verified_fix_p_001_20260601_120000_ab12",
        status="data_inferred",
    )
    ks.add_fact(
        T,
        "Verified fix: record gone",
        fact_id="verified_fix_p_002_20260601_120000_ab12",
        status="data_inferred",
    )
    ks.add_fact(T, "gone", fact_id="gone")
    ks.retire_fact(T, "gone", by="user", reason="r")
    ks.add_fact("DB.S.U", "free text on a bare doc", fact_id="free")
    _strip_lifecycle(ks, T, "DB.S.U")
    _publish_record(workspace, "20260601-120000-ab12", "p_001")
    policy = workspace.config.knowledge

    dry = reconcile_docs(ks, workspace.records_dir, policy, dry_run=True, anchor_missing=True)
    assert [a["fact_id"] for a in dry["anchored"]] == [
        "amt",
        "verified_fix_p_001_20260601_120000_ab12",
        "verified_fix_p_002_20260601_120000_ab12",
    ]
    assert dry["unanchorable"] == 1 and ks.fact(T, "amt")["anchors"] == []  # dry: unwritten

    out = reconcile_docs(ks, workspace.records_dir, policy, anchor_missing=True)
    amt = ks.fact(T, "amt")
    assert {"kind": "column", "name": "AMOUNT"} in amt["anchors"]
    assert {"kind": "definition", "key": "m.sql", "hash": "sha256:a"} in amt["anchors"]
    assert amt["anchored_by"] == "reconcile" and amt["anchored_at"] and amt["kind"] is None
    fix = ks.fact(T, "verified_fix_p_001_20260601_120000_ab12")
    assert fix["kind"] == "verified_fix"
    assert {
        "kind": "record",
        "session": "20260601-120000-ab12",
        "id": "p_001",
        "record_kind": "proposal",
    } in fix["anchors"]
    orphan = ks.fact(T, "verified_fix_p_002_20260601_120000_ab12")
    assert orphan["kind"] == "verified_fix"  # folds in briefings from now on
    assert not any(a["kind"] == "record" for a in orphan["anchors"])  # nothing to point at
    by_id = {a["fact_id"]: a for a in out["anchored"]}
    assert by_id["verified_fix_p_001_20260601_120000_ab12"]["record"] is True
    assert by_id["verified_fix_p_002_20260601_120000_ab12"]["record"] is False
    assert ks.fact(T, "gone")["anchored_by"] is None  # retired: untouched
    assert ks.fact("DB.S.U", "free")["anchored_by"] is None  # nothing to anchor to
    again = reconcile_docs(ks, workspace.records_dir, policy, anchor_missing=True)
    assert again["anchored"] == [] and again["unanchorable"] == 1  # idempotent
    # standing works from here on
    ks.upsert_definition(T, {"path": "m.sql", "hash": "sha256:b", "kind": "dbt_model"})
    doc = ks.read(T)
    amt = next(f for f in doc["facts"] if f["id"] == "amt")
    assert effective_standing(amt, doc, StandingContext())[0] == "unverified"


def test_confirmed_supersession_reads_as_done_and_reconcile_executes_it(workspace, ks):
    from grayson.knowledge import StandingContext, annotate_doc, effective_standing
    from grayson.util import utcnow

    ks.add_fact(T, "old", fact_id="old")
    ks.confirm_fact(T, "old")
    ks.add_fact(T, "new", fact_id="new", supersedes="old", evidence=["q_1"])
    # an older grayson's confirm: the status flips, nothing executes
    doc = ks.read(T)
    new = next(f for f in doc["facts"] if f["id"] == "new")
    new.update(status="user_confirmed", confirmed_by="kcg", confirmed_at=utcnow())
    ks.save(T, doc)
    assert ks.fact(T, "old")["superseded_by"] is None
    # read time: the human vouched, so the pair is done, not contested
    doc = ks.read(T)
    old = next(f for f in doc["facts"] if f["id"] == "old")
    assert effective_standing(old, doc, StandingContext()) == ("retired", "superseded by new")
    assert annotate_doc(doc, StandingContext())["contested"] == []
    policy = workspace.config.knowledge
    dry = reconcile_docs(ks, workspace.records_dir, policy, dry_run=True)
    assert dry["supersessions_executed"] == [{"table": T, "fact_id": "old", "by": "new"}]
    assert ks.fact(T, "old")["superseded_by"] is None
    reconcile_docs(ks, workspace.records_dir, policy)
    old = ks.fact(T, "old")
    assert old["superseded_by"] == "new" and old["retired_by"] == "reconcile"
    assert old["standing_reason"] == "superseded by new (confirmed by kcg)"
    again = reconcile_docs(ks, workspace.records_dir, policy)
    assert again["supersessions_executed"] == []


def test_doctor_counts_unanchored_facts(workspace, ks):
    ks.set_profile(T, {"columns": [{"name": "AMOUNT"}]})
    ks.add_fact(T, "AMOUNT is gross", fact_id="amt")
    _strip_lifecycle(ks, T)
    standing = library_doctor(workspace)["standing"]
    assert standing["unanchored_facts"] == 1 and "--anchor-missing" in standing["hint"]
    reconcile_docs(ks, workspace.records_dir, workspace.config.knowledge, anchor_missing=True)
    standing = library_doctor(workspace)["standing"]
    assert standing["unanchored_facts"] == 0 and "--anchor-missing" not in standing["hint"]


def test_cli_reconcile_anchor_missing(workspace, ks):
    ks.set_profile(T, {"columns": [{"name": "AMOUNT"}]})
    ks.add_fact(T, "AMOUNT is gross", fact_id="amt")
    _strip_lifecycle(ks, T)
    dry = invoke("library", "reconcile", "--dry-run", "--anchor-missing")
    assert dry["anchor_missing"] and [a["fact_id"] for a in dry["anchored"]] == ["amt"]
    assert ks.fact(T, "amt")["anchors"] == []
