from __future__ import annotations

import pytest

from seekql.config import GuardSettings
from seekql.core.session import Session
from seekql.history import suggest_guard_profile
from seekql.knowledge import KnowledgeStore
from seekql.library import extract_library, init_library, library_status
from seekql.views import ViewEntry, ViewRegistry

# -- last-used guard profile --------------------------------------------


def test_suggest_none_when_no_history(workspace):
    assert suggest_guard_profile(workspace, ["DB.S.T"]) is None


def test_suggest_last_used_profile(workspace):
    Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T"],
        guard=GuardSettings(),
        guard_profile="strict",
    )
    assert suggest_guard_profile(workspace, ["DB.S.T"]) == "strict"
    assert suggest_guard_profile(workspace, ["DB.S.OTHER"]) is None


# -- library scaffolding & extraction -----------------------------------


def test_init_library(tmp_path):
    lib = init_library(tmp_path / "teamlib")
    assert (lib / "knowledge" / "glossary.md").is_file()
    assert (lib / "views" / "registry.yaml").is_file()
    assert (lib / "workflows").is_dir()
    assert (lib / "README.md").is_file()


def test_status_solo_mode(workspace):
    assert library_status(workspace)["linked"] is False


def test_extract_library(workspace, tmp_path):
    # seed some assets in the workspace
    KnowledgeStore(workspace.knowledge_dir).add_fact("DB.S.T", "a fact", fact_id="f1")
    ViewRegistry(workspace.views_dir).register(ViewEntry(name="V", source_tables=["DB.S.T"]))
    result = extract_library(workspace, tmp_path / "extracted")
    dest = tmp_path / "extracted"
    assert (dest / "knowledge" / "DB" / "S" / "T.md").is_file()
    assert (dest / "views" / "registry.yaml").is_file()
    assert any("T.md" in c for c in result["copied"])


# -- linked library resolution ------------------------------------------


def test_workspace_resolves_linked_library(tmp_path, monkeypatch):
    from seekql.workspace import Workspace

    lib = init_library(tmp_path / "lib")
    ws = Workspace.init(tmp_path / "ws")
    config = (ws.root / "seekql.toml").read_text()
    config += f'\n[library]\npath = "{lib.as_posix()}"\n'
    (ws.root / "seekql.toml").write_text(config)
    ws2 = Workspace(ws.root)
    assert ws2.knowledge_dir == lib / "knowledge"
    assert ws2.views_dir == lib / "views"


def test_missing_linked_library_raises(tmp_path):
    from seekql.workspace import Workspace

    ws = Workspace.init(tmp_path / "ws2")
    config = (ws.root / "seekql.toml").read_text()
    missing = (tmp_path / "does_not_exist").as_posix()
    config += f'\n[library]\npath = "{missing}"\n'
    (ws.root / "seekql.toml").write_text(config)
    with pytest.raises(FileNotFoundError):
        _ = Workspace(ws.root).knowledge_dir
