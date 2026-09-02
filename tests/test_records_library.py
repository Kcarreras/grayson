"""Records compound across the team: accepted findings and verified fixes
publish into the library, and teammates read them from their own workspaces."""

from __future__ import annotations

import json

import pytest

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core import engine, proposals
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.identity import set_user_id
from grayson.library import set_library_config
from grayson.records import get_record, search_library_records, search_records
from grayson.workspace import Workspace


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


def _finding(session, title="Dup rows"):
    qid = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    f = engine.record_finding(
        session,
        {
            "title": title,
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
    )
    return f["fid"], qid


def test_accept_publishes_to_library(workspace, session):
    set_user_id("kcg")
    fid, _ = _finding(session)
    assert not any(workspace.records_dir.rglob("*.json"))  # nothing until accepted
    session.accept_finding(fid)
    path = workspace.records_dir / session.id / f"{fid}.json"
    assert path.is_file()
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["kind"] == "finding" and doc["accepted"] is True
    assert doc["format"] == 1  # stamped like knowledge docs — see docs/LIBRARY.md
    assert doc["author"] == "kcg"
    assert doc["record"]["payload"]["extra"]["root_cause"] == "join fan-out"


def test_rejected_findings_do_not_publish(workspace, session):
    fid, _ = _finding(session)
    session.reject_finding(fid, "not convinced")
    assert not any(workspace.records_dir.rglob("*.json"))


def test_verification_publishes_proposal(workspace, session):
    fid, before = _finding(session)
    p = proposals.record_proposal(session, "ddl_snippet", "fix join", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    after = run_statement(
        session, "SELECT * FROM DB.S.T1 WHERE dup > 1", executor=FakeExecutor(rows=[])
    )["qid"]
    proposals.verify(session, p["pid"], before, after, "pass", "anomaly gone")
    path = workspace.records_dir / session.id / f"{p['pid']}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["kind"] == "proposal" and doc["verdict"] == "pass"
    assert doc["record"]["verification"]["comparison"]["after_empty"] is True


def test_supersession_republishes_old_finding(workspace, session):
    f1, _ = _finding(session, title="First read")
    session.accept_finding(f1)
    qid = run_statement(session, "SELECT id FROM DB.S.T1", executor=FakeExecutor())["qid"]
    f2 = engine.record_finding(
        session,
        {
            "title": "Corrected read",
            "severity": "high",
            "confidence": "high",
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "The first finding misread the grain.",
            "evidence": [qid],
            "supersedes": f1,
            "extra": {
                "resolution": "root_caused",
                "root_cause": "grain misread",
                "blast_radius": "same rows",
                "alternatives_tested": "one ruled out",
            },
        },
    )["fid"]
    session.accept_finding(f2)
    old = json.loads(
        (workspace.records_dir / session.id / f"{f1}.json").read_text(encoding="utf-8")
    )
    assert old["superseded_by"] == f2  # the library copy no longer reads as current


def test_teammate_workspace_sees_published_records(workspace, session, tmp_path):
    """The compounding loop: A accepts in their workspace; B searches from theirs."""
    set_user_id("kcg")
    fid, _ = _finding(session)
    session.accept_finding(fid)
    # both workspaces point at the same library ("clone" shared for the test)
    set_library_config(workspace.root, workspace.root, auto_push=False)
    ws_b = Workspace.init(tmp_path / "teammate")
    set_library_config(ws_b.root, workspace.root, auto_push=False)
    ws_b = Workspace(ws_b.root)

    rows = search_records(ws_b, "duplicate")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "library" and row["author"] == "kcg"

    full = get_record(ws_b, row["session_id"], "finding", row["id"])
    assert full["source"] == "library"
    assert full["record"]["payload"]["summary"].startswith("Duplicate rows")


def test_local_session_wins_over_library_copy(workspace, session):
    fid, _ = _finding(session)
    session.accept_finding(fid)
    rows = search_records(workspace, "duplicate")
    assert len(rows) == 1  # deduped: published copy does not double the local row
    assert rows[0]["source"] == "session"


def test_library_search_is_verdict_scoped(workspace, session):
    fid, _ = _finding(session)
    session.accept_finding(fid)
    rows = search_library_records(workspace.records_dir, "fan-out")
    assert len(rows) == 1
    assert "payload" not in rows[0]  # summaries only; records_get has the full record


def test_published_records_carry_their_evidence_queries(workspace, session):
    # A query id is a per-session counter and its SQL lives in local state; a
    # published record carries the cited statements so the evidence survives
    # the trip to the library.
    fid, qid = _finding(session)
    session.accept_finding(fid)
    path = workspace.records_dir / session.id / f"{fid}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    ev = doc["evidence_queries"]
    assert [e["qid"] for e in ev] == [qid]
    assert ev[0]["session_id"] == session.id
    assert "SELECT * FROM DB.S.T1" in ev[0]["sql"]
    assert ev[0]["tables"] == ["DB.S.T1"] and ev[0]["status"] == "executed"
    # proposal: the before/after pair
    p = proposals.record_proposal(session, "ddl_snippet", "fix join", {"ddl": "SELECT 1"}, fid)
    proposals.decide(session, p["pid"], approve=True)
    after = run_statement(
        session, "SELECT * FROM DB.S.T1 WHERE dup > 1", executor=FakeExecutor(rows=[])
    )["qid"]
    proposals.verify(session, p["pid"], qid, after, "pass", "anomaly gone")
    pdoc = json.loads((workspace.records_dir / session.id / f"{p['pid']}.json").read_text())
    assert [e["qid"] for e in pdoc["evidence_queries"]] == [qid, after]
    # list rows stay light; the full record carries the snapshot either way
    rows = search_library_records(workspace.records_dir)
    assert all("evidence_queries" not in r for r in rows)
    local = get_record(workspace, session.id, "finding", fid)
    assert [e["qid"] for e in local["evidence_queries"]] == [qid]


# -- removal: the author's action, or an admin's ----------------------------


def _git(*args, cwd=None):
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


@pytest.fixture
def team_lib(workspace, tmp_path):
    """A linked, auto-pushing clone of a bare origin — the team setup."""
    from grayson.library import link_library

    origin = tmp_path / "origin.git"
    origin.mkdir()
    assert _git("init", "--bare", str(origin)).returncode == 0
    clone = tmp_path / "lib-clone"
    link_library(workspace, str(origin), clone, auto_push=True)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    workspace.reload_config()
    return clone


def _rec(folder, name, author, kind="finding"):
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"kind": kind, "id": name.removesuffix(".json"), "title": "t", "author": author}
    (folder / name).write_text(json.dumps(doc))


def test_deletion_verdict_rules(tmp_path):
    from grayson.records import deletion_verdict

    d = tmp_path / "records"
    nothing = deletion_verdict(d, "s1", "kcg", [])
    assert not nothing["allowed"] and "nothing is published" in nothing["reason"]
    _rec(d / "s1", "f_001.json", "kcg")
    _rec(d / "s1", "report.json", "kcg", "report")
    (d / "s1" / "report.md").write_text("# report")  # rides along, not counted
    mine = deletion_verdict(d, "s1", "kcg", [])
    assert mine["allowed"] and mine["as"] == "author (kcg)" and mine["count"] == 2
    theirs = deletion_verdict(d, "s1", "bob", [])
    assert not theirs["allowed"] and "published by kcg, not you (bob)" in theirs["reason"]
    assert deletion_verdict(d, "s1", "bob", ["bob"])["as"] == "library admin"
    anon = deletion_verdict(d, "s1", None, [])
    assert not anon["allowed"] and "no user id is set" in anon["reason"] and "kcg" in anon["reason"]
    # an authorless record makes the whole set an admin's to remove
    _rec(d / "s1", "p_001.json", None, "proposal")
    unowned = deletion_verdict(d, "s1", "kcg", [])
    assert not unowned["allowed"] and "no author" in unowned["reason"]
    assert deletion_verdict(d, "s1", "kcg", ["kcg"])["allowed"]
    # solo mode: no team to protect
    assert deletion_verdict(d, "s1", None, [], solo=True)["allowed"]


def test_delete_session_records_is_one_attributed_commit(workspace, session, team_lib, tmp_path):
    from grayson.records import delete_session_records, session_records

    set_user_id("kcg")
    fid, _ = _finding(session)
    session.accept_finding(fid)  # publishes as kcg, auto-pushed
    folder = workspace.records_dir / session.id
    assert (folder / f"{fid}.json").is_file()
    assert [r["author"] for r in session_records(workspace.records_dir, session.id)] == ["kcg"]
    # a teammate is refused, and nothing moves
    set_user_id("bob")
    with pytest.raises(PermissionError, match="published by kcg, not you"):
        delete_session_records(workspace, session.id, "tidy")
    assert folder.is_dir()
    # the author removes it: one commit, the reason and the trailer, pushed
    set_user_id("kcg")
    out = delete_session_records(workspace, session.id, "started against the wrong table")
    assert out["count"] == 1 and out["removed"] == [f"{fid}.json"] and out["as"] == "author (kcg)"
    assert out["library_sync"]["ok"] and out["library_sync"]["committed"]
    assert not folder.exists()
    body = _git("log", "-1", "--format=%B", cwd=team_lib).stdout
    assert f"grayson records: remove {session.id} (1 record(s))" in body
    assert "started against the wrong table" in body and "Grayson-User: kcg" in body
    assert f"remove {session.id}" in _git("log", "--format=%s", cwd=tmp_path / "origin.git").stdout
    assert not search_library_records(workspace.records_dir)
    # the local session keeps its own copy of the finding
    assert session.finding(fid)["accepted"]
    with pytest.raises(PermissionError, match="nothing is published"):
        delete_session_records(workspace, session.id, "again")


def test_admin_removes_a_teammates_records(workspace, session, team_lib):
    from grayson.library import write_library_settings
    from grayson.records import delete_session_records

    set_user_id("kcg")
    fid, _ = _finding(session)
    session.accept_finding(fid)
    write_library_settings(team_lib, {"admins": ["boss"]})
    set_user_id("boss")
    out = delete_session_records(workspace, session.id, "cleanup")
    assert out["as"] == "library admin" and not (workspace.records_dir / session.id).exists()


def test_removal_without_auto_push_waits_for_library_push(workspace, session, team_lib):
    from grayson.config_edit import set_values
    from grayson.library import push_library, repo_status
    from grayson.records import delete_session_records

    set_user_id("kcg")
    fid, _ = _finding(session)
    session.accept_finding(fid)
    set_values(workspace.root, {"library.auto_push": False})
    workspace.reload_config()
    out = delete_session_records(workspace, session.id, "no longer relevant")
    assert out["library_sync"]["committed"] and "library push" in out["library_sync"]["detail"]
    assert repo_status(team_lib)["ahead"] == 1  # committed locally, not yet pushed
    assert push_library(workspace, "grayson: library update")["ok"]
    assert repo_status(team_lib)["ahead"] == 0


def test_solo_workspace_removes_its_own_records(workspace, session):
    from grayson.records import delete_session_records

    fid, _ = _finding(session)
    session.accept_finding(fid)  # no user id, no library: solo mode
    assert (workspace.records_dir / session.id / f"{fid}.json").is_file()
    out = delete_session_records(workspace, session.id)
    assert out["as"].startswith("solo workspace")
    assert out["library_sync"] == {
        "ok": True,
        "committed": False,
        "detail": "library is not a git repo",
    }
    assert not (workspace.records_dir / session.id).exists()


def test_cli_records_delete_and_session_delete_library(workspace, session, team_lib, monkeypatch):
    from typer.testing import CliRunner

    from grayson.cli import app

    runner = CliRunner()
    set_user_id("kcg")
    fid, _ = _finding(session)
    session.accept_finding(fid)
    # a shell-out cannot: user actions need a terminal
    refused = runner.invoke(app, ["records", "delete", session.id, "--yes"])
    assert refused.exit_code == 1 and "removing published records" in refused.output
    assert (workspace.records_dir / session.id).is_dir()
    # a teammate is told whose they are before any prompt
    set_user_id("bob")
    refused = runner.invoke(app, ["records", "delete", session.id, "--yes"])
    assert refused.exit_code == 1 and "published by kcg, not you (bob)" in refused.output
    set_user_id("kcg")
    import grayson.cli as cli

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)
    done = runner.invoke(app, ["records", "delete", session.id, "--reason", "restarted"])
    assert done.exit_code == 0, done.output
    assert json.loads(done.output)["count"] == 1
    assert not (workspace.records_dir / session.id).exists()
    # session delete --library takes the records with the session, in one go
    fid2, _ = _finding(session, "Second")
    session.accept_finding(fid2)
    assert (workspace.records_dir / session.id).is_dir()
    gone = runner.invoke(app, ["session", "delete", session.id, "--yes", "--library"])
    assert gone.exit_code == 0, gone.output
    out = json.loads(gone.output)
    assert out["deleted"] == session.id and out["library"]["count"] == 1
    assert not (workspace.records_dir / session.id).exists()
    assert session.id not in workspace.list_session_ids()
