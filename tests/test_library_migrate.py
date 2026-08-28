"""`grayson library migrate`: deliberate format rewrites, one revertible commit."""

from __future__ import annotations

import subprocess

import pytest

from grayson.knowledge import KNOWLEDGE_FORMAT, KnowledgeStore
from grayson.library import migrate_library


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


def _write_unstamped(workspace, table="DB.S.T1"):
    db, schema, name = table.split(".")
    path = workspace.knowledge_dir / db / schema / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntable: {table}\nfacts:\n- id: f1\n  fact: amounts are gross\n"
        f"  status: proposed\n---\n\n# {table}\n",
        encoding="utf-8",
    )
    return path


def test_migrate_stamps_unstamped_docs(workspace):
    path = _write_unstamped(workspace)
    out = migrate_library(workspace)
    assert out["migrated"] == ["DB.S.T1"]
    assert f"format: {KNOWLEDGE_FORMAT}" in path.read_text()
    # content survived the rewrite
    doc = KnowledgeStore(workspace.knowledge_dir).read("DB.S.T1")
    assert doc["facts"][0]["fact"] == "amounts are gross"
    # not a git repo: no rollback point, and the report says so
    assert "warning" in out


def test_migrate_is_idempotent(workspace):
    _write_unstamped(workspace)
    migrate_library(workspace)
    again = migrate_library(workspace)
    assert again["migrated"] == []
    assert again["up_to_date"] == 1


def test_migrate_refuses_a_dirty_git_tree(workspace):
    _write_unstamped(workspace)
    assert _git("init", cwd=workspace.root).returncode == 0
    with pytest.raises(RuntimeError, match="dirty"):
        migrate_library(workspace)


def test_migrate_lands_as_one_labeled_commit(workspace):
    _write_unstamped(workspace)
    assert _git("init", cwd=workspace.root).returncode == 0
    _git("config", "user.email", "t@example.com", cwd=workspace.root)
    _git("config", "user.name", "t", cwd=workspace.root)
    _git("add", "-A", cwd=workspace.root)
    _git("commit", "-m", "before", cwd=workspace.root)
    out = migrate_library(workspace)
    assert out["migrated"] == ["DB.S.T1"]
    assert out["committed"] is True
    log = _git("log", "--oneline", "-1", cwd=workspace.root).stdout
    assert "grayson library migrate" in log
    # revertibility is the point: the previous state is one commit back
    show = _git("show", "HEAD~1:knowledge/DB/S/T1.md", cwd=workspace.root).stdout
    assert "format:" not in show


def test_migrate_reports_a_too_new_doc_and_continues(workspace):
    _write_unstamped(workspace, "DB.S.T1")
    newer = _write_unstamped(workspace, "DB.S.T2")
    newer.write_text(
        newer.read_text().replace("table: DB.S.T2\n", "table: DB.S.T2\nformat: 99\n"),
        encoding="utf-8",
    )
    out = migrate_library(workspace)
    assert out["migrated"] == ["DB.S.T1"]
    assert out["errors"] and out["errors"][0]["table"] == "DB.S.T2"
    assert "refusing to rewrite" in out["errors"][0]["error"]
