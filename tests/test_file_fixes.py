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


@pytest.mark.parametrize("replacement", ["select 2;\r\n-- café\r\n", ""])
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
