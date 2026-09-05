"""Upgrade regressions: existing user data and harness choices survive refreshes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from grayson.cli import app
from grayson.config import GuardSettings, WorkspaceConfig
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.harness.generate import HARNESSES, INSTRUCTION_PATHS, generate_harness
from grayson.harness.update import apply_plan, harness_status, update_harness
from grayson.knowledge import KnowledgeDocError, KnowledgeStore
from grayson.library import migrate_library


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("harness", sorted(HARNESSES))
@pytest.mark.parametrize("with_mcp", [False, True])
def test_harness_refresh_preview_backup_and_idempotence(tmp_path, harness, with_mcp):
    generate_harness(tmp_path, harness, with_mcp)
    target = tmp_path / INSTRUCTION_PATHS[harness]
    # Simulate old generated instructions after a package upgrade.
    target.write_text(
        target.read_text(encoding="utf-8").replace("## Golden rules", "## Old rules"),
        encoding="utf-8",
    )
    before = _snapshot(tmp_path)
    status = harness_status(tmp_path, harness)
    assert status["installed"] and not status["current"]
    assert status["with_mcp"] is with_mcp
    preview = update_harness(tmp_path, harness)
    assert not preview["applied"] and preview["diffs"]
    assert _snapshot(tmp_path) == before
    out = update_harness(tmp_path, harness, apply=True)
    backup = Path(out["backup"])
    assert (backup / INSTRUCTION_PATHS[harness]).read_bytes() == before[INSTRUCTION_PATHS[harness]]
    assert harness_status(tmp_path, harness)["current"]
    after = _snapshot(tmp_path)
    assert update_harness(tmp_path, harness, apply=True)["changed"] == []
    assert generate_harness(tmp_path, harness, with_mcp)["changed"] == []
    assert _snapshot(tmp_path) == after


@pytest.mark.parametrize("harness", ["codex", "claude-code", "copilot"])
def test_refresh_preserves_house_rules_and_replaces_legacy_markers(tmp_path, harness):
    target = tmp_path / INSTRUCTION_PATHS[harness]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# House rules\n\n<!-- seekql:start -->\nold\n<!-- seekql:end -->\n\nKeep this footer.\n",
        encoding="utf-8",
    )
    out = update_harness(tmp_path, harness, apply=True)
    assert out["changed"]
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# House rules\n\n")
    assert "\n\nKeep this footer.\n" in text
    assert "seekql:start" not in text
    assert text.count("<!-- grayson:start -->") == 1


@pytest.mark.parametrize(
    "broken",
    [
        "<!-- grayson:start -->\nmissing end",
        "<!-- grayson:end -->\n<!-- grayson:start -->",
        "<!-- grayson:start --><!-- grayson:end -->" * 2,
        "<!-- grayson-workflow-author:start -->\nmissing end",
    ],
)
def test_damaged_markers_preflight_before_any_write(tmp_path, broken):
    (tmp_path / "AGENTS.md").write_text(broken, encoding="utf-8")
    before = _snapshot(tmp_path)
    with pytest.raises(ValueError, match="markers"):
        generate_harness(tmp_path, "codex")
    assert _snapshot(tmp_path) == before


def test_update_leaves_mcp_and_permissions_byte_identical(tmp_path):
    generate_harness(tmp_path, "claude-code")
    configs = {
        ".mcp.json": b'{"mcpServers":{"grayson":{"url":"https://team.invalid/mcp"}}}',
        ".claude/settings.json": b'{"permissions":{"deny":["my-rule"]}}',
    }
    for rel, content in configs.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(content)
    update_harness(tmp_path, "claude-code", apply=True)
    for rel, content in configs.items():
        assert (tmp_path / rel).read_bytes() == content


def test_refresh_rolls_back_if_a_later_write_fails(tmp_path, monkeypatch):
    import grayson.harness.update as updater

    original = b"user content\r\n"
    (tmp_path / "CLAUDE.md").write_bytes(original)
    real_write = updater.atomic_write_text

    def write(path, text):
        if path == tmp_path / "second.md":
            raise OSError("disk full")
        real_write(path, text)

    monkeypatch.setattr(updater, "atomic_write_text", write)
    with pytest.raises(OSError, match="disk full"):
        apply_plan(tmp_path, {"CLAUDE.md": "replacement", "second.md": "new"})
    assert (tmp_path / "CLAUDE.md").read_bytes() == original
    assert not (tmp_path / "second.md").exists()
    backups = list((tmp_path / ".grayson/harness-backups").glob("*/CLAUDE.md"))
    assert len(backups) == 1 and backups[0].read_bytes() == original


def test_status_does_not_install_anything_and_cli_preview_is_read_only(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["harness", "status", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert all(not h["installed"] for h in json.loads(result.output)["harnesses"])
    assert _snapshot(tmp_path) == {}
    result = runner.invoke(app, ["harness", "update", "codex", "--path", str(tmp_path)])
    assert result.exit_code == 1 and "harness init codex" in result.output
    generate_harness(tmp_path, "codex")
    before = _snapshot(tmp_path)
    result = runner.invoke(app, ["harness", "update", "codex", "--path", str(tmp_path)])
    assert result.exit_code == 0 and not json.loads(result.output)["applied"]
    assert _snapshot(tmp_path) == before


def test_relative_library_is_resolved_from_config_not_cwd(workspace, tmp_path, monkeypatch):
    config = workspace.root / "grayson.toml"
    config.write_text('[library]\npath = "../team-library"\n', encoding="utf-8")
    expected = workspace.root.parent / "team-library"
    expected.mkdir()
    monkeypatch.chdir(tmp_path)
    assert WorkspaceConfig.load(config).library_path == expected
    sub = workspace.root / "nested"
    sub.mkdir()
    monkeypatch.chdir(sub)
    assert workspace.knowledge_dir == expected / "knowledge"


def _legacy_doc(workspace, text):
    store = KnowledgeStore(workspace.knowledge_dir)
    path = store.table_path("DB.S.T")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return store, path


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_migrate_only_adds_stamp_preserving_unknown_data_and_comments(workspace, newline):
    text = (
        "---\n# Keep our comment\ntable: DB.S.T\ncustom: {team: finance}\n"
        "facts:\n- id: f1\n  fact: 'A fact'\n  custom: [a, b]\n"
        "---\n\n# DB.S.T\n\nOur notes.\n"
    ).replace("\n", newline)
    store, path = _legacy_doc(workspace, text)
    dry = migrate_library(workspace, dry_run=True)
    assert dry["migrated"] == ["DB.S.T"] and dry["dry_run"]
    assert path.read_bytes() == text.encode()
    assert migrate_library(workspace)["migrated"] == ["DB.S.T"]
    assert (
        path.read_bytes()
        == text.replace("---" + newline, "---" + newline + "format: 1" + newline, 1).encode()
    )
    assert store.read("DB.S.T")["facts"][0]["custom"] == ["a", "b"]
    assert migrate_library(workspace)["migrated"] == []


@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_migration_preserves_mixed_newlines_and_utf8_bom(workspace, bom):
    text = bom + "---\ntable: DB.S.T\nfacts:\n- id: f1\n  fact: kept\n---\nNotes.\r\n"
    store, path = _legacy_doc(workspace, text)
    assert migrate_library(workspace)["migrated"] == ["DB.S.T"]
    assert path.read_bytes() == text.replace("---\n", "---\nformat: 1\n", 1).encode()
    assert store.read("DB.S.T")["facts"][0]["fact"] == "kept"


@pytest.mark.parametrize(
    "text",
    [
        "---\ntable: DB.S.T\nfacts: []\n",  # missing delimiter
        "---\ntable: []\n---\n",
        "---\ntable: DB.S.T\nfacts: 5\n---\n",
        "---\ntable: DB.S.T\nretired_questions: 42\n---\n",
        "---\ntable: DB.S.T\nformat: 1\nfacts: broken\n---\n",
    ],
)
def test_malformed_library_docs_are_reported_without_rewriting(workspace, text):
    store, path = _legacy_doc(workspace, text)
    with pytest.raises(KnowledgeDocError):
        store.read("DB.S.T")
    report = migrate_library(workspace)
    assert report["errors"] and not report["migrated"]
    assert path.read_bytes() == text.encode()


def test_library_migration_refuses_failed_git_status_before_writing(workspace, monkeypatch):
    import grayson.library as library

    _, path = _legacy_doc(workspace, "---\ntable: DB.S.T\n---\n")
    before = path.read_bytes()
    (workspace.root / ".git").mkdir()
    monkeypatch.setattr(
        library, "_git", lambda *a, **kw: subprocess.CompletedProcess(a, 128, "", "broken git")
    )
    with pytest.raises(RuntimeError, match="cannot check"):
        migrate_library(workspace)
    assert path.read_bytes() == before


def test_executor_construction_failure_finishes_audit_row(workspace, monkeypatch):
    import grayson.core.run as run

    session = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T"],
        guard=GuardSettings(),
        guard_profile="moderate",
    )

    def broken(*args):
        raise ValueError("invalid executor configuration")

    monkeypatch.setattr(run, "get_executor", broken)
    result = run_statement(session, "SELECT * FROM DB.S.T")
    assert result["status"] == "error"
    assert session.query_row(result["qid"])["status"] == "error"
    assert not session.executed_qids()


def test_house_mcp_heading_does_not_change_no_mcp_installation(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "# House rules\n\n## MCP\nOur unrelated server.\n", encoding="utf-8"
    )
    generate_harness(tmp_path, "claude-code", with_mcp=False)
    assert harness_status(tmp_path, "claude-code")["with_mcp"] is False
    assert update_harness(tmp_path, "claude-code")["changed"] == []


def test_harness_update_preserves_existing_crlf(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_bytes(
        b"# House rules\r\n\r\n<!-- grayson:start -->\r\nold\r\n"
        b"<!-- grayson:end -->\r\n\r\nFooter.\r\n"
    )
    update_harness(tmp_path, "claude-code", apply=True)
    content = target.read_bytes()
    assert content.startswith(b"# House rules\r\n\r\n")
    assert content.endswith(b"\r\n\r\nFooter.\r\n")
    assert b"\n" not in content.replace(b"\r\n", b"")
    assert update_harness(tmp_path, "claude-code", apply=True)["changed"] == []


def test_nested_markers_do_not_erase_other_managed_sections(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "<!-- grayson:start -->\n<!-- grayson-workflow-author:start -->\n"
        "valuable custom instructions\n<!-- grayson-workflow-author:end -->\n"
        "<!-- grayson:end -->\n",
        encoding="utf-8",
    )
    before = _snapshot(tmp_path)
    with pytest.raises(ValueError, match="nested"):
        generate_harness(tmp_path, "codex")
    assert _snapshot(tmp_path) == before


def test_library_migration_commit_failure_never_pushes(workspace, monkeypatch):
    import grayson.library as library

    _legacy_doc(workspace, "---\ntable: DB.S.T\n---\n")
    (workspace.root / ".git").mkdir()
    workspace.config.library_auto_push = True
    calls = []

    def git(root, *args, **kwargs):
        calls.append(args)
        if args[0] == "commit":
            return subprocess.CompletedProcess(args, 1, "", "hook refused")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(library, "_git", git)
    with pytest.raises(RuntimeError, match="rollback commit.*hook refused"):
        migrate_library(workspace)
    assert not any(args[0] == "push" for args in calls)
    assert ("add", "--", "knowledge/DB/S/T.md") in calls
    assert calls[-1][-2:] == ("--", "knowledge/DB/S/T.md")


def test_library_migration_preview_works_without_terminal_and_reports_errors(workspace):
    _legacy_doc(workspace, "---\ntable: DB.S.T\n---\n")
    before = _snapshot(workspace.root)
    result = CliRunner().invoke(app, ["library", "migrate", "--dry-run"])
    assert result.exit_code == 0 and json.loads(result.output)["dry_run"]
    assert _snapshot(workspace.root) == before
    _legacy_doc(workspace, "---\ntable: DB.S.T\nformat: 99\n---\n")
    result = CliRunner().invoke(app, ["library", "migrate", "--dry-run"])
    assert result.exit_code == 1 and json.loads(result.output)["errors"]


def test_legacy_workspace_is_not_reinitialized_or_skipped(tmp_path, monkeypatch):
    from grayson.workspace import LegacyWorkspaceError, Workspace

    (tmp_path / "seekql.toml").write_text('[connection]\nname = "sandbox"\n', encoding="utf-8")
    state = tmp_path / ".seekql" / "sessions"
    state.mkdir(parents=True)
    (state / "original.db").write_bytes(b"existing session")
    nested = tmp_path / "nested"
    nested.mkdir()
    before = _snapshot(tmp_path)
    with pytest.raises(LegacyWorkspaceError, match="UPGRADING"):
        Workspace.find(nested)
    with pytest.raises(FileExistsError, match="legacy SeekQL"):
        Workspace.init(tmp_path)
    monkeypatch.chdir(nested)
    result = CliRunner().invoke(app, ["init", "."])
    assert result.exit_code == 1 and "legacy SeekQL" in result.output
    assert _snapshot(tmp_path) == before
