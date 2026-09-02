"""Library admins: named by a human when the library is made, changed by an
admin at a terminal, reported wherever the library is described — and honest
about being a guard rail over declared identity, not access control."""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest
from typer.testing import CliRunner

from grayson.cli import app
from grayson.identity import set_user_id
from grayson.library import (
    init_library,
    library_admins,
    library_doctor,
    library_status,
    link_library,
    read_library_settings,
    set_library_admins,
    settings_last_change,
    write_library_settings,
)

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def invoke_err(*args) -> dict:
    result = runner.invoke(app, list(args))
    assert result.exit_code != 0, result.output
    return json.loads(result.stderr or result.output)


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


@pytest.fixture
def team_lib(workspace, tmp_path):
    """A linked, auto-pushing clone of a bare origin — the team setup."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    assert _git("init", "--bare", str(origin)).returncode == 0
    clone = tmp_path / "lib-clone"
    link_library(workspace, str(origin), clone, auto_push=True)
    _git("config", "user.email", "t@example.com", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    workspace.reload_config()
    return clone


def test_init_writes_no_admins_unless_told(tmp_path):
    lib = init_library(tmp_path / "a")
    assert read_library_settings(lib) == {"admins": []}
    assert library_admins(lib) == []
    lib2 = init_library(tmp_path / "b", ["kcg"])
    assert library_admins(lib2) == ["kcg"]
    init_library(lib2, ["other"])  # re-scaffolding (a teammate linking) never resets them
    assert library_admins(lib2) == ["kcg"]


def test_admins_fail_closed_on_bad_input(tmp_path):
    lib = init_library(tmp_path / "a")
    (lib / "library.toml").write_text('[library]\nadmins = "kcg"\n')  # not a list
    assert library_admins(lib) == []
    (lib / "library.toml").write_text("not = [toml")
    assert library_admins(lib) == []
    with pytest.raises(ValueError, match="not valid TOML"):
        read_library_settings(lib)
    write_library_settings(lib, {"admins": ["kcg", "bad id", 7]})
    assert library_admins(lib) == ["kcg"]


def test_set_admins_bootstrap_then_admin_only(workspace, team_lib):
    set_user_id("kcg")
    assert library_admins(team_lib) == []
    # an empty list is the bootstrap case: whoever is at the terminal claims it
    out = set_library_admins(workspace, add="kcg")
    assert out["admins"] == ["kcg"] and out["library_sync"]["ok"]
    body = _git("log", "-1", "--format=%B", cwd=team_lib).stdout
    assert "grayson library admins: add kcg" in body and "Grayson-User: kcg" in body
    assert settings_last_change(team_lib)["user_id"] == "kcg"
    # from then on, admins only
    set_user_id("bob")
    with pytest.raises(PermissionError, match="only a library admin"):
        set_library_admins(workspace, add="bob")
    assert library_admins(team_lib) == ["kcg"]
    set_user_id("kcg")
    assert set_library_admins(workspace, add="bob")["admins"] == ["kcg", "bob"]
    assert set_library_admins(workspace, add="bob")["changed"] is False
    with pytest.raises(ValueError, match="not an admin"):
        set_library_admins(workspace, remove="nobody")
    with pytest.raises(ValueError, match="admin id must be"):
        set_library_admins(workspace, add="not valid")
    assert set_library_admins(workspace, remove="kcg")["admins"] == ["bob"]
    status = library_status(workspace)
    assert status["admins"] == ["bob"] and status["admins_changed"]["user_id"] == "kcg"
    assert library_doctor(workspace)["settings"]["ok"]


def test_no_user_id_cannot_set_admins(workspace, team_lib):
    with pytest.raises(PermissionError, match="set your user id"):
        set_library_admins(workspace, add="x")


def test_doctor_flags_a_broken_settings_file(workspace, team_lib):
    (team_lib / "library.toml").write_text('[library]\nadmins = ["ok", "bad id"]\n')
    report = library_doctor(workspace)
    assert not report["ok"] and "bad id" in report["settings"]["errors"][0]
    (team_lib / "library.toml").write_text("[library\n")
    report = library_doctor(workspace)
    assert not report["ok"] and "not valid TOML" in report["settings"]["errors"][0]


def test_cli_admins_is_a_terminal_action(workspace, team_lib, monkeypatch, tmp_path):
    set_user_id("kcg")
    err = invoke_err("library", "admins", "add", "kcg")["error"]
    assert "changing the library admins" in err and "interactive terminal" in err
    listed = invoke("library", "admins", "list")
    assert listed["admins"] == [] and listed["you"] == "kcg"
    assert listed["changed"]["user_id"] is None  # the scaffold commit carried no trailer
    import grayson.cli as cli

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)
    assert invoke("library", "admins", "add", "kcg")["admins"] == ["kcg"]
    assert invoke("library", "admins", "list")["changed"]["user_id"] == "kcg"
    assert invoke("library", "admins", "remove", "kcg")["admins"] == []
    # init names the admins only when told (a scripted run never guesses one)
    made = invoke("library", "init", str(tmp_path / "fresh"), "--admin", "kcg", "--admin", "bob")
    assert made["admins"] == ["kcg", "bob"] and library_admins(tmp_path / "fresh") == ["kcg", "bob"]
    assert invoke("library", "init", str(tmp_path / "quiet"))["admins"] == []


def test_knowledge_server_reports_admins(team_lib):
    from grayson.mcp.knowledge_server import build_knowledge_server

    write_library_settings(team_lib, {"admins": ["kcg"]})
    server = build_knowledge_server(team_lib)
    result = asyncio.run(server.call_tool("library_info", {}))
    content = getattr(result, "content", None) or []
    info = json.loads(content[0].text)
    assert info["admins"] == ["kcg"] and info["mode"].startswith("knowledge-only")
