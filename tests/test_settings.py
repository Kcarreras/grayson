"""Settings surface: config edits, CLI, Settings page, MCP read-only, bootstrap."""

from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from grayson.cli import app as cli_app
from grayson.config import WorkspaceConfig
from grayson.config_edit import ConfigError, set_guard_profile, set_values
from grayson.library import link_library
from grayson.ui.server import build_app

runner = CliRunner()
TOKEN = "tok"


def invoke(*args):
    result = runner.invoke(cli_app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def client(workspace):
    return TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")


# -- config_edit ---------------------------------------------------------


def test_set_values_surgical_and_validated(workspace):
    cfg_path = workspace.root / "grayson.toml"
    before = cfg_path.read_text(encoding="utf-8")
    out = set_values(
        workspace.root,
        {"defaults.guard_profile": "strict", "scopes.strict": "true"},
    )
    assert out["changed"] == {"defaults.guard_profile": "strict", "scopes.strict": True}
    cfg = WorkspaceConfig.load(cfg_path)
    assert cfg.default_guard_profile == "strict" and cfg.scopes.strict is True
    # untouched sections keep their exact original lines (comments included)
    after = cfg_path.read_text(encoding="utf-8")
    for line in before.splitlines():
        if line.strip().startswith("[guard_profiles") or "auto_limit" in line:
            assert line in after


def test_set_values_rejects_unknown_and_invalid(workspace):
    with pytest.raises(ConfigError, match="unknown setting"):
        set_values(workspace.root, {"nope.key": "1"})
    with pytest.raises(ConfigError, match="unknown guard profile"):
        set_values(workspace.root, {"defaults.guard_profile": "nonexistent"})
    with pytest.raises(ConfigError, match="true or false"):
        set_values(workspace.root, {"scopes.strict": "maybe"})
    with pytest.raises(ConfigError, match="does not exist"):
        set_values(workspace.root, {"library.path": "/definitely/not/a/dir"})


def test_scopes_allowed_from_comma_string(workspace):
    set_values(workspace.root, {"scopes.allowed": "ANALYTICS.*, RAW.PUBLIC"})
    cfg = WorkspaceConfig.load(workspace.root / "grayson.toml")
    assert cfg.scopes.allowed == ["ANALYTICS.*", "RAW.PUBLIC"]


def test_guard_profile_partial_edit_and_create(workspace):
    out = set_guard_profile(workspace.root, "moderate", {"timeout_seconds": 300})
    assert out["settings"]["timeout_seconds"] == 300
    assert out["settings"]["auto_limit"] == 10000  # untouched fields keep values
    out2 = set_guard_profile(workspace.root, "overnight", {"auto_limit": 0, "budget_cap": 500})
    cfg = WorkspaceConfig.load(workspace.root / "grayson.toml")
    assert cfg.guard_profiles["overnight"].budget_cap == 500
    assert out2["profile"] == "overnight"
    with pytest.raises(ConfigError, match="invalid guard settings"):
        set_guard_profile(workspace.root, "bad", {"auto_limit": -5})


# -- CLI -----------------------------------------------------------------


def test_cli_config_show_set_profile(workspace):
    shown = invoke("config", "show")
    assert shown["default_guard_profile"] == "moderate"
    assert "settable_keys" in shown
    out = invoke("config", "set", "defaults.guard_profile=generous", "scopes.strict=true")
    assert out["config"]["default_guard_profile"] == "generous"
    assert out["config"]["scopes"]["strict"] is True
    prof = invoke("config", "profile", "strict", "--timeout", "30")
    assert prof["settings"]["timeout_seconds"] == 30
    bad = runner.invoke(cli_app, ["config", "set", "defaults.guard_profile=nope"])
    assert bad.exit_code == 1


# -- MCP (read-only) -----------------------------------------------------


def test_mcp_config_show_registered_and_no_mutators(workspace, fake_snow_env):
    import asyncio

    from grayson.mcp.server import build_server

    server = build_server(workspace)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "config_show" in names
    # the agent surface must not carry configuration mutators
    assert not any(n.startswith("config_") and n != "config_show" for n in names)
    result = asyncio.run(server.call_tool("config_show", {}))
    payload = json.loads(result.content[0].text)
    assert payload["default_guard_profile"] == "moderate"


# -- Settings page -------------------------------------------------------


def test_settings_page_renders_and_saves(client, workspace):
    page = client.get(f"/settings?t={TOKEN}")
    assert page.status_code == 200
    assert "Guard profiles" in page.text and "Team library" in page.text
    assert "grayson library link" in page.text  # solo mode shows the bootstrap command
    saved = client.post(
        f"/settings/general?t={TOKEN}",
        data={
            "connection": "sandbox",
            "guard_profile": "strict",
            "strict": "true",
            "allowed": "SANDBOX.*",
        },
        follow_redirects=True,
    )
    assert saved.status_code == 200
    cfg = WorkspaceConfig.load(workspace.root / "grayson.toml")
    assert cfg.connection == "sandbox"
    assert cfg.default_guard_profile == "strict"
    assert cfg.scopes.allowed == ["SANDBOX.*"]


def test_settings_profile_post(client, workspace):
    resp = client.post(
        f"/settings/profile/moderate?t={TOKEN}",
        data={
            "auto_limit": "5000",
            "timeout_seconds": "60",
            "budget_warn": "10",
            "budget_cap": "0",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    cfg = WorkspaceConfig.load(workspace.root / "grayson.toml")
    assert cfg.guard_profiles["moderate"].auto_limit == 5000


def test_settings_bad_value_shows_error(client):
    resp = client.post(
        f"/settings/general?t={TOKEN}",
        data={"connection": "x", "guard_profile": "nope", "allowed": ""},
    )
    assert resp.status_code == 400
    assert "unknown guard profile" in resp.text


def test_theme_toggle_present(client):
    page = client.get(f"/?t={TOKEN}").text
    assert "grayson_theme" in page  # pre-paint stamp + toggle script
    assert 'data-theme="light"' in page  # pinned-theme token block exists


# -- library bootstrap from an empty remote ------------------------------


def test_link_bootstraps_empty_remote_repo(workspace, tmp_path):
    origin = tmp_path / "team-lib.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    clone_dest = tmp_path / "clone"
    result = link_library(workspace, str(origin), dest=clone_dest, auto_push=True)
    assert result["action"] == "cloned"
    assert result["bootstrapped"]["committed"] and result["bootstrapped"]["pushed"]
    # the remote now holds the scaffold: a second link from scratch gets structure
    ls = subprocess.run(
        ["git", "ls-tree", "--name-only", "-r", "HEAD"],
        cwd=origin, capture_output=True, text=True, check=True,
    )  # fmt: skip
    files = set(ls.stdout.split())
    assert "views/registry.yaml" in files and "knowledge/glossary.md" in files
    assert "checks/README.md" in files


def test_link_existing_local_dir_never_commits(workspace, tmp_path):
    local = tmp_path / "local-lib"
    local.mkdir()
    subprocess.run(["git", "init", str(local)], check=True, capture_output=True)
    (local / "unrelated.txt").write_text("someone's work in progress", encoding="utf-8")
    result = link_library(workspace, str(local))
    assert "bootstrapped" not in result  # local links are never auto-committed
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=local, capture_output=True, text=True
    )
    assert "unrelated.txt" in status.stdout  # still uncommitted, untouched
