"""The file write, not just the status label, requires current human approval."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FakeExecutor
from grayson.cli import app
from grayson.core import criteria, file_fixes, proposals
from grayson.core.proposals import ProposalError
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.ui.server import build_app


@pytest.fixture
def session(workspace):
    return Session.create(
        workspace,
        workflow="bug-hunter",
        targets=["DB.S.T1"],
        guard=workspace.config.resolve_profile("moderate"),
        guard_profile="moderate",
    )


def _draft(session, content="select 2;\n"):
    source = session.workspace.root / "model.sql"
    source.write_bytes(b"select 1;\r\n")
    proposal = file_fixes.draft(session, "model.sql", content, "Fix model")
    return source, proposal


def _approve(session, proposal):
    return proposals.decide(
        session, proposal["pid"], True, digest=file_fixes.review_digest(proposal)
    )


@pytest.mark.parametrize("replacement", ["select 2;\r\n-- café\r\n", "select 2;"])
def test_draft_approve_apply_writes_only_after_approval(session, replacement):
    source, p = _draft(session, replacement)
    original = source.read_bytes()
    assert p["status"] == "proposed"
    assert "select 1" in p["payload"]["diff"]
    with pytest.raises(ProposalError, match="approved"):
        file_fixes.apply(session, p["pid"])
    with pytest.raises(ProposalError, match="proposal apply"):
        proposals.mark_applied(session, p["pid"])
    assert source.read_bytes() == original
    _approve(session, p)
    assert source.read_bytes() == original
    result = file_fixes.apply(session, p["pid"])
    assert result["status"] == "applied"
    assert source.read_bytes() == replacement.encode("utf-8")
    assert session.get_meta(f"file_applied:{p['pid']}")
    with pytest.raises(ProposalError, match="approved"):
        file_fixes.apply(session, p["pid"])


def test_create_is_only_written_after_approval(session):
    p = file_fixes.draft(session, "new.sql", "select 1;", "New model")
    source = session.workspace.root / "new.sql"
    assert not source.exists()
    assert p["payload"]["file_change"]["before_sha256"] is None
    _approve(session, p)
    file_fixes.apply(session, p["pid"])
    assert source.read_text() == "select 1;"


@pytest.mark.skipif(os.name != "posix", reason="POSIX creation modes and umask")
@pytest.mark.parametrize(
    "existing_mode, mask, expected",
    [
        (None, 0o022, 0o644),
        (None, 0o002, 0o664),
        (None, 0o077, 0o600),
        (0o640, 0o077, 0o640),
        (0o755, 0o077, 0o755),
    ],
)
def test_apply_respects_creation_umask_and_preserves_existing_mode(
    session, existing_mode, mask, expected
):
    source = session.workspace.root / "model.sql"
    if existing_mode is not None:
        source.write_text("before")
        source.chmod(existing_mode)
    p = file_fixes.draft(session, "model.sql", "after", "Fix model")
    _approve(session, p)
    # Isolate the process-wide umask from other tests and their threads.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys\n"
            "from pathlib import Path\n"
            "from grayson.core import file_fixes\n"
            "from grayson.core.session import Session\n"
            "from grayson.workspace import Workspace\n"
            "os.umask(int(sys.argv[4]))\n"
            "file_fixes.apply(Session(Workspace(Path(sys.argv[1])), sys.argv[2]), sys.argv[3])\n",
            str(session.workspace.root),
            session.id,
            p["pid"],
            str(mask),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert source.read_text() == "after"
    assert stat.S_IMODE(source.stat().st_mode) == expected
    assert session.proposal(p["pid"])["status"] == "applied"


def test_temporary_name_collision_does_not_overwrite_or_delete_other_file(session, monkeypatch):
    p = file_fixes.draft(session, "new.sql", "after", "New model")
    _approve(session, p)
    collision = session.workspace.root / ".new.sql.collision"
    collision.write_text("another writer's file")
    monkeypatch.setattr(file_fixes.secrets, "token_hex", lambda _: "collision")
    with pytest.raises(FileExistsError):
        file_fixes.apply(session, p["pid"])
    assert collision.read_text() == "another writer's file"
    assert not (session.workspace.root / "new.sql").exists()
    assert session.proposal(p["pid"])["status"] == "approved"
    assert not (session.workspace.root / ".grayson/file-apply.lock").exists()


def test_review_distinguishes_lines_without_final_newlines(session):
    source = session.workspace.root / "model.sql"
    source.write_bytes(b"old")
    p = file_fixes.draft(session, "model.sql", "new", "Fix")
    assert "-old\n\\ No newline at end of file\n+new\n" in p["payload"]["diff"]
    assert source.read_bytes() == b"old"


@pytest.mark.parametrize("approved", [False, True])
def test_source_changed_since_draft_is_never_overwritten(session, approved):
    source, p = _draft(session)
    if approved:
        _approve(session, p)
    source.write_text("user's unsaved-work replacement", encoding="utf-8")
    with pytest.raises(ProposalError, match="source file changed"):
        if approved:
            file_fixes.apply(session, p["pid"])
        else:
            _approve(session, p)
    assert source.read_text() == "user's unsaved-work replacement"
    assert not (session.workspace.root / ".grayson/file-apply.lock").exists()


def test_rejection_and_forged_approval_do_not_write(session):
    source, p = _draft(session)
    with pytest.raises(ProposalError, match="user action"):
        proposals.decide(session, p["pid"], True, actor="agent", digest=file_fixes.review_digest(p))
    with pytest.raises(ProposalError, match="changed since review"):
        proposals.decide(session, p["pid"], True, digest="")
    proposals.decide(session, p["pid"], False)
    with pytest.raises(ProposalError, match="approved"):
        file_fixes.apply(session, p["pid"])
    assert source.read_bytes() == b"select 1;\r\n"


def test_changed_payload_cannot_reuse_approval(session):
    source, p = _draft(session)
    _approve(session, p)
    p["payload"]["new_content"] = "select 99;"
    con = session._con()
    try:
        con.execute(
            "UPDATE proposals SET payload=? WHERE pid=?", (json.dumps(p["payload"]), p["pid"])
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(ProposalError, match="changed since approval"):
        file_fixes.apply(session, p["pid"])
    assert source.read_bytes() == b"select 1;\r\n"


@pytest.mark.parametrize(
    "target",
    [
        "../outside.sql",
        "/tmp/file.sql",
        "C:\\file.sql",
        "model.sql:stream",
        ".cursor/hooks.json",
        ".grayson/state.db",
        ".git/config",
        "grayson.toml",
        "missing/model.sql",
    ],
)
def test_control_paths_and_escaping_targets_rejected(session, target):
    with pytest.raises(ProposalError):
        file_fixes.draft(session, target, "content", "Bad target")
    assert session.proposals() == []


def test_hardlinked_file_is_refused(session):
    source = session.workspace.root / "original.sql"
    source.write_text("original")
    os.link(source, session.workspace.root / "alias.sql")
    with pytest.raises(ProposalError, match="hard links"):
        file_fixes.draft(session, "alias.sql", "changed", "Alias")
    assert source.read_text() == "original"


def test_symlink_is_refused(session, tmp_path):
    outside = tmp_path / "outside.sql"
    outside.write_text("original")
    try:
        (session.workspace.root / "alias.sql").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires OS permission")
    with pytest.raises(ProposalError, match="symlinks"):
        file_fixes.draft(session, "alias.sql", "changed", "Alias")
    assert outside.read_text() == "original"


def test_failed_write_does_not_mark_applied_or_leave_lock(session, monkeypatch):
    source, p = _draft(session)
    _approve(session, p)

    def fail_replace(*args):
        raise OSError("disk error")

    monkeypatch.setattr(file_fixes.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk error"):
        file_fixes.apply(session, p["pid"])
    assert source.read_bytes() == b"select 1;\r\n"
    assert session.proposal(p["pid"])["status"] == "approved"
    assert not (session.workspace.root / ".grayson/file-apply.lock").exists()
    assert list(source.parent.glob(".model.sql.*")) == []


def test_workspace_lock_prevents_competing_apply(session):
    source, p = _draft(session)
    _approve(session, p)
    lock = session.workspace.root / ".grayson/file-apply.lock"
    lock.write_text("busy")
    with pytest.raises(ProposalError, match="workspace lock"):
        file_fixes.apply(session, p["pid"])
    assert lock.read_text() == "busy"
    assert source.read_bytes() == b"select 1;\r\n"


def test_managed_file_with_success_criteria(session):
    source, p = _draft(session)
    qid = run_statement(
        session, "SELECT COUNT(*) AS n FROM DB.S.T1", executor=FakeExecutor(rows=[{"n": 1}])
    )["qid"]
    contract = criteria.set_criteria(
        session,
        p["pid"],
        {
            "format": 1,
            "criteria": [
                {
                    "id": "preserve_count",
                    "name": "Preserve count",
                    "source_qid": qid,
                    "expectation": {"kind": "scalar", "column": "n", "value": 1},
                }
            ],
        },
    )
    proposals.decide(session, p["pid"], True, digest=contract["digest"])
    file_fixes.apply(session, p["pid"])
    assert session.get_meta(f"criteria_applied:{p['pid']}")
    assert source.read_text() == "select 2;\n"
    result = criteria.run_verification(session, p["pid"], executor=FakeExecutor(rows=[{"n": 1}]))
    assert result["verdict"] == "pass"


def test_ui_review_then_apply(session, workspace):
    source, p = _draft(session)
    client = TestClient(build_app(workspace, token="test"), base_url="http://127.0.0.1")
    endpoint = f"/session/{session.id}/proposal/{p['pid']}"
    assert client.post(f"{endpoint}/apply?t=test").status_code == 400
    assert client.post(f"{endpoint}/approve?t=test").status_code == 400
    page = client.get(f"/session/{session.id}?t=test").text
    digest = re.search(r'name="digest" value="([a-f0-9]+)"', page).group(1)
    response = client.post(
        f"{endpoint}/approve?t=test", data={"digest": digest}, follow_redirects=False
    )
    assert response.status_code == 303
    assert source.read_bytes() == b"select 1;\r\n"
    assert "Apply approved file fix" in client.get(f"/session/{session.id}?t=test").text
    assert client.post(f"{endpoint}/apply?t=test", follow_redirects=False).status_code == 303
    assert source.read_text() == "select 2;\n"


def test_cli_drafts_without_touching_source_then_applies(session, tmp_path):
    source = session.workspace.root / "model.sql"
    source.write_text("old")
    replacement = tmp_path / "draft.sql"
    replacement.write_text("new")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "proposal",
            "draft-file",
            session.id,
            "--target",
            "model.sql",
            "--content-file",
            str(replacement),
            "--title",
            "Fix",
        ],
    )
    assert result.exit_code == 0, result.output
    p = json.loads(result.output)
    assert source.read_text() == "old"
    assert runner.invoke(app, ["proposal", "apply", session.id, p["pid"]]).exit_code != 0
    _approve(session, p)
    result = runner.invoke(app, ["proposal", "apply", session.id, p["pid"]])
    assert result.exit_code == 0, result.output
    assert source.read_text() == "new"


def _edit(session, edits, **kwargs):
    snapshot = file_fixes.source_snapshot(session, "model.sql")
    return file_fixes.draft(
        session,
        "model.sql",
        None,
        "Targeted fix",
        edits=edits,
        expected_source_sha256=snapshot["sha256"],
        **kwargs,
    )


def test_large_file_four_edits_preserve_every_untouched_byte(session):
    source = session.workspace.root / "model.sql"
    original = "".join(f"SELECT {i} AS value; -- café {'x' * 25}\r\n" for i in range(2696))
    source.write_bytes(original.encode("utf-8"))
    edits = [
        {"old_text": f"SELECT {i} AS", "new_text": f"SELECT {i + 10000} AS"}
        for i in (0, 750, 1700, 2695)
    ]
    p = _edit(session, edits, request_id="four-edits")
    expected = original
    for edit in edits:
        expected = expected.replace(edit["old_text"], edit["new_text"])
    assert p["payload"]["new_content"] == expected
    assert p["payload"]["file_change"]["stats"]["after_lines"] == 2696
    assert len(session.proposals()) == 1
    assert source.read_bytes() == original.encode("utf-8")
    _approve(session, p)
    file_fixes.apply(session, p["pid"])
    assert source.read_bytes() == expected.encode("utf-8")


@pytest.mark.parametrize(
    "edits, message",
    [
        ([{"old_text": "missing", "new_text": "new"}], "exactly once"),
        ([{"old_text": "a", "new_text": "new"}], "exactly once"),
        (
            [{"old_text": "alpha", "new_text": "new"}, {"old_text": "lpha", "new_text": "other"}],
            "overlap",
        ),
        (
            [{"old_text": "alpha", "new_text": "new"}, {"old_text": "new", "new_text": "other"}],
            "exactly once",
        ),
        ([{"old_text": "", "new_text": "new"}], "nonempty"),
        ([{"old_text": "alpha", "new_text": 4}], "string"),
        ([], "nonempty"),
    ],
)
def test_invalid_edit_batch_has_no_partial_proposal(session, edits, message):
    source = session.workspace.root / "model.sql"
    source.write_bytes(b"alpha beta")
    with pytest.raises(ProposalError, match=message):
        _edit(session, edits)
    assert session.proposals() == []
    assert source.read_bytes() == b"alpha beta"


def test_overlapping_occurrences_are_ambiguous(session):
    (session.workspace.root / "model.sql").write_text("aaa")
    with pytest.raises(ProposalError, match="exactly once"):
        _edit(session, [{"old_text": "aa", "new_text": "b"}])


def test_stale_edit_snapshot_is_rejected(session):
    source = session.workspace.root / "model.sql"
    source.write_text("first")
    snapshot = file_fixes.source_snapshot(session, "model.sql")
    source.write_text("second")
    with pytest.raises(ProposalError, match="snapshot mismatch"):
        file_fixes.draft(
            session,
            "model.sql",
            None,
            "Fix",
            edits=[{"old_text": "second", "new_text": "third"}],
            expected_source_sha256=snapshot["sha256"],
        )
    assert session.proposals() == []


@pytest.mark.parametrize(
    "original, replacement",
    [
        ("select 1;", ""),
        ("line\n" * 2696, "header\n"),
        ("x" * 10000, "x" * 100),
    ],
)
def test_truncated_replacement_creates_no_proposal(session, original, replacement):
    source = session.workspace.root / "model.sql"
    source.write_bytes(original.encode())
    with pytest.raises(ProposalError, match="truncated"):
        file_fixes.draft(session, "model.sql", replacement, "Broken")
    assert not session.proposals()
    assert source.read_bytes() == original.encode()


@pytest.mark.parametrize("approved", [False, True])
def test_legacy_truncated_proposal_cannot_be_approved_or_applied(session, approved):
    source = session.workspace.root / "model.sql"
    original = "line\n" * 2696
    source.write_text(original, newline="")
    p = file_fixes.draft(session, "model.sql", original + "tail\n", "Legacy")
    body = p["payload"]
    body["new_content"] = "header\n"
    body["file_change"].pop("mode")
    body["file_change"].pop("stats")
    body["file_change"]["after_sha256"] = file_fixes._sha(b"header\n")
    con = session._con()
    try:
        con.execute(
            "UPDATE proposals SET payload=?, status=? WHERE pid=?",
            (json.dumps(body), "approved" if approved else "proposed", p["pid"]),
        )
        con.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
            (f"file_approval:{p['pid']}", file_fixes.review_digest(p)),
        )
        con.commit()
    finally:
        con.close()
    with pytest.raises(ProposalError, match="truncated"):
        if approved:
            file_fixes.apply(session, p["pid"])
        else:
            _approve(session, p)
    assert source.read_text() == original


def test_retry_and_revision_invalidate_old_approval(session):
    source, old = _draft(session)
    _approve(session, old)
    # Full-content callers get identical retry protection even without a request ID.
    assert file_fixes.draft(session, "model.sql", "select 2;\n", "Fix model")["pid"] == old["pid"]
    edits = [{"old_text": "select 1;", "new_text": "select 3;"}]
    new = _edit(session, edits, request_id="revision", supersedes=old["pid"])
    assert _edit(session, edits, request_id="revision", supersedes=old["pid"])["pid"] == new["pid"]
    assert len(session.proposals()) == 2
    assert session.proposal(old["pid"])["status"] == "superseded"
    assert not session.get_meta(f"file_approval:{old['pid']}")
    with pytest.raises(ProposalError):
        _approve(session, old)
    with pytest.raises(ProposalError, match="approved"):
        file_fixes.apply(session, old["pid"])
    with pytest.raises(ProposalError, match="approved"):
        file_fixes.apply(session, new["pid"])
    assert source.read_bytes() == b"select 1;\r\n"
    _approve(session, new)
    file_fixes.apply(session, new["pid"])
    # Lost response after apply still resolves to the same request without redrafting.
    retry = file_fixes.draft(
        session,
        "model.sql",
        None,
        "Targeted fix",
        edits=edits,
        expected_source_sha256=old["payload"]["file_change"]["before_sha256"],
        request_id="revision",
        supersedes=old["pid"],
    )
    assert retry["pid"] == new["pid"] and retry["status"] == "applied"


def test_request_id_cannot_be_reused_for_different_edits(session):
    _draft(session)
    _edit(session, [{"old_text": "1", "new_text": "3"}], request_id="same")
    with pytest.raises(ProposalError, match="different inputs"):
        _edit(session, [{"old_text": "1", "new_text": "4"}], request_id="same")
    assert len(session.proposals()) == 2


def test_failed_revision_keeps_previous_proposal_approved(session):
    _, old = _draft(session)
    _approve(session, old)
    with pytest.raises(ProposalError):
        _edit(session, [{"old_text": "missing", "new_text": "4"}], supersedes=old["pid"])
    assert session.proposal(old["pid"])["status"] == "approved"
    assert len(session.proposals()) == 1


def test_bulk_deletion_requires_explicit_ui_acknowledgement(session, workspace):
    source = session.workspace.root / "model.sql"
    source.write_text("select 1;")
    p = _edit(session, [{"old_text": "select 1;", "new_text": ""}])
    client = TestClient(build_app(workspace, token="test"), base_url="http://127.0.0.1")
    endpoint = f"/session/{session.id}/proposal/{p['pid']}"
    page = client.get(f"/session/{session.id}?t=test").text
    assert "Large deletion" in page and 'name="acknowledge_deletions"' in page
    data = {"digest": file_fixes.review_digest(p)}
    assert client.post(f"{endpoint}/approve?t=test", data=data).status_code == 400
    assert session.proposal(p["pid"])["status"] == "proposed"
    data["acknowledge_deletions"] = "true"
    assert (
        client.post(f"{endpoint}/approve?t=test", data=data, follow_redirects=False).status_code
        == 303
    )
    assert source.read_text() == "select 1;"
    assert client.post(f"{endpoint}/apply?t=test", follow_redirects=False).status_code == 303
    assert source.read_bytes() == b""


def test_diff_review_escapes_source_and_blocks_stale_actions(session, workspace):
    source, _ = _draft(session)
    p = _edit(session, [{"old_text": "select 1;", "new_text": "<script>alert(1)</script>"}])
    client = TestClient(build_app(workspace, token="test"), base_url="http://127.0.0.1")
    page = client.get(f"/session/{session.id}?t=test").text
    assert 'class="diff-added"' in page and 'class="diff-removed"' in page
    assert '<th scope="col">Old</th>' in page
    assert "&lt;script&gt;" in page and "<script>alert(1)</script>" not in page
    source.write_text("source drift")
    page = client.get(f"/session/{session.id}?t=test").text
    assert "Review blocked" in page
    assert re.search(r"<button[^>]+disabled[^>]*>Approve proposal", page)
    assert session.proposal(p["pid"])["status"] == "proposed"


def test_bulk_deletion_criteria_review_and_revision(session, workspace):
    source = session.workspace.root / "model.sql"
    source.write_text("select 1;")
    p = _edit(session, [{"old_text": "select 1;", "new_text": ""}])
    qid = run_statement(
        session, "SELECT COUNT(*) AS n FROM DB.S.T1", executor=FakeExecutor(rows=[{"n": 1}])
    )["qid"]
    contract = criteria.set_criteria(
        session,
        p["pid"],
        {
            "format": 1,
            "criteria": [
                {
                    "id": "preserve_count",
                    "name": "Preserve count",
                    "source_qid": qid,
                    "expectation": {"kind": "scalar", "column": "n", "value": 1},
                }
            ],
        },
    )
    client = TestClient(build_app(workspace, token="test"), base_url="http://127.0.0.1")
    page = client.get(f"/session/{session.id}/criteria/{p['pid']}?t=test").text
    assert 'class="diff-removed"' in page
    assert 'name="acknowledge_deletions"' in page
    with pytest.raises(ProposalError, match="acknowledge"):
        criteria.approve(session, p["pid"], contract["digest"], "user")
    criteria.approve(session, p["pid"], contract["digest"], "user", acknowledge_deletions=True)
    new = _edit(session, [{"old_text": "1", "new_text": "3"}], supersedes=p["pid"])
    assert not session.get_meta(f"criteria_approval:{p['pid']}")
    with pytest.raises(ProposalError, match="approved"):
        file_fixes.apply(session, p["pid"])
    assert new["status"] == "proposed"


def test_draft_rechecks_source_before_publishing(session, monkeypatch):
    source, old = _draft(session)
    _approve(session, old)
    real_save = file_fixes._save_draft

    def changed(*args):
        source.write_text("another editor saved")
        return real_save(*args)

    monkeypatch.setattr(file_fixes, "_save_draft", changed)
    with pytest.raises(ProposalError, match="source file changed"):
        _edit(session, [{"old_text": "1", "new_text": "3"}], supersedes=old["pid"])
    assert len(session.proposals()) == 1
    assert session.proposal(old["pid"])["status"] == "approved"
    assert not (session.workspace.root / ".grayson/file-apply.lock").exists()


def test_revision_cannot_race_an_apply(session):
    source, old = _draft(session)
    _approve(session, old)
    lock = session.workspace.root / ".grayson/file-apply.lock"
    lock.write_text("apply in progress")
    with pytest.raises(ProposalError, match="operation is in progress"):
        _edit(session, [{"old_text": "1", "new_text": "3"}], supersedes=old["pid"])
    assert lock.read_text() == "apply in progress"
    assert session.proposal(old["pid"])["status"] == "approved"
    assert source.read_bytes() == b"select 1;\r\n"


def test_cli_targeted_edits(session, tmp_path):
    source = session.workspace.root / "model.sql"
    source.write_bytes(b"select 1;\r\n-- keep\r\n")
    edits_file = tmp_path / "edits.json"
    edits_file.write_text(json.dumps([{"old_text": "select 1;", "new_text": "select 2;"}]))
    runner = CliRunner()
    snapshot = runner.invoke(
        app, ["proposal", "file-snapshot", session.id, "--target", "model.sql"]
    )
    assert snapshot.exit_code == 0, snapshot.output
    result = runner.invoke(
        app,
        [
            "proposal",
            "draft-edits",
            session.id,
            "--target",
            "model.sql",
            "--source-sha256",
            json.loads(snapshot.output)["sha256"],
            "--edits-file",
            str(edits_file),
            "--request-id",
            "cli",
            "--title",
            "Fix",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["payload"]["new_content"] == "select 2;\r\n-- keep\r\n"
    assert source.read_bytes() == b"select 1;\r\n-- keep\r\n"


def test_diff_rows_keep_markers_line_numbers_and_missing_newlines():
    from grayson.ui.diffs import diff_rows

    rows = diff_rows(file_fixes._review_diff("-- comment\nold", "++ changed\nnew", "model.sql"))
    removed = [r for r in rows if r["kind"] == "removed"]
    added = [r for r in rows if r["kind"] == "added"]
    assert [r["old"] for r in removed] == [1, 2]
    assert [r["new"] for r in added] == [1, 2]
    assert removed[0]["text"] == "--- comment"
    assert added[0]["text"] == "+++ changed"
    assert sum(r["text"] == "\\ No newline at end of file" for r in rows) == 2
