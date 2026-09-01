"""Team library linking, auto-push, and the structured knowledge profile."""

from __future__ import annotations

import json
import subprocess

from typer.testing import CliRunner

from grayson.cli import app
from grayson.workspace import Workspace

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


def test_link_local_path(workspace, tmp_path):
    lib = tmp_path / "team-lib"
    lib.mkdir()
    out = invoke("library", "link", str(lib))
    assert out["action"] == "linked existing directory"
    assert (lib / "knowledge").is_dir()  # scaffolded
    # a fresh Workspace picks up the new config, and knowledge writes land in the library
    assert Workspace(workspace.root).config.library_path == lib.resolve()
    invoke("knowledge", "add", "DB.S.T1", "--fact", "one row per id")
    assert (lib / "knowledge" / "DB" / "S" / "T1.md").is_file()


def test_link_missing_local_path_fails(workspace, tmp_path):
    result = runner.invoke(app, ["library", "link", str(tmp_path / "nope")])
    assert result.exit_code == 1


def test_link_clone_and_auto_push(workspace, tmp_path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    assert _git("init", "--bare", str(origin)).returncode == 0
    clone_dest = tmp_path / "lib-clone"
    out = invoke("library", "link", str(origin), "--dest", str(clone_dest), "--auto-push")
    assert out["action"] == "cloned"
    assert out["auto_push"] is True
    # identity for the test commit (isolated from any global config)
    _git("config", "user.email", "t@example.com", cwd=clone_dest)
    _git("config", "user.name", "t", cwd=clone_dest)

    added = invoke("knowledge", "add", "DB.S.T1", "--fact", "amounts are gross, not net")
    assert added["library_sync"]["ok"] is True
    log = _git("log", "--oneline", cwd=origin)
    assert "grayson knowledge" in log.stdout


def test_knowledge_set_profile_and_completeness(workspace):
    doc = invoke(
        "knowledge",
        "set",
        "DB.S.T1",
        "--json",
        json.dumps(
            {
                "grain": "one row per order (ORDER_ID)",
                "columns": [
                    {"name": "ORDER_ID", "type": "NUMBER", "description": "surrogate key"},
                    {"name": "AMOUNT", "type": "NUMBER"},
                ],
                "freshness": "daily by 06:00 UTC",
                "open_questions": ["is AMOUNT gross or net?"],
            }
        ),
    )
    comp = doc["completeness"]
    assert comp["base_complete"] is False
    assert comp["columns_described"] == 1 and comp["columns_total"] == 2
    assert any("column_descriptions" in m for m in comp["missing"])
    assert "relationships" in comp["missing"]
    assert comp["open_questions"] == 1
    # filling the gaps flips base_complete
    done = invoke(
        "knowledge",
        "set",
        "DB.S.T1",
        "--json",
        json.dumps(
            {
                "columns": [
                    {"name": "ORDER_ID", "type": "NUMBER", "description": "surrogate key"},
                    {"name": "AMOUNT", "type": "NUMBER", "description": "order total"},
                ],
                "relationships": [
                    {"to": "DB.S.CUSTOMERS", "on": "CUSTOMER_ID", "cardinality": "many-to-one"}
                ],
                "definition_files": ["models/orders.sql"],
            }
        ),
    )
    assert done["completeness"]["base_complete"] is True
    # profile survives round-trip alongside facts
    invoke("knowledge", "add", "DB.S.T1", "--fact", "refunds appear as negative amounts")
    shown = invoke("knowledge", "show", "DB.S.T1")
    assert shown["grain"].startswith("one row per order")
    assert shown["completeness"]["facts"] == 1


def test_knowledge_set_rejects_unknown_and_bad_fields(workspace):
    bad_key = runner.invoke(app, ["knowledge", "set", "DB.S.T1", "--json", '{"grian": "typo"}'])
    assert bad_key.exit_code == 1
    assert "unknown profile fields" in bad_key.output
    bad_cols = runner.invoke(
        app, ["knowledge", "set", "DB.S.T1", "--json", '{"columns": ["ORDER_ID"]}']
    )
    assert bad_cols.exit_code == 1


def test_rejected_push_rebases_and_retries(workspace, tmp_path):
    # Two analysts auto-pushing to one library: the second push is rejected
    # non-fast-forward; push_library rebases the small library commit onto the
    # teammate's and retries, so the write publishes without manual git.
    origin = tmp_path / "origin.git"
    origin.mkdir()
    assert _git("init", "--bare", str(origin)).returncode == 0
    clone_dest = tmp_path / "lib-clone"
    invoke("library", "link", str(origin), "--dest", str(clone_dest), "--auto-push")
    _git("config", "user.email", "t@example.com", cwd=clone_dest)
    _git("config", "user.name", "t", cwd=clone_dest)

    # a teammate pushes first
    other = tmp_path / "other-clone"
    assert _git("clone", str(origin), str(other)).returncode == 0
    _git("config", "user.email", "o@example.com", cwd=other)
    _git("config", "user.name", "o", cwd=other)
    (other / "note.md").write_text("teammate got here first\n", encoding="utf-8")
    _git("add", "-A", cwd=other)
    _git("commit", "-m", "teammate note", cwd=other)
    assert _git("push", "-u", "origin", "HEAD", cwd=other).returncode == 0

    added = invoke("knowledge", "add", "DB.S.T1", "--fact", "amounts are gross, not net")
    sync = added["library_sync"]
    assert sync["ok"] is True
    assert sync["rebased"] is True
    log = _git("log", "--oneline", cwd=origin)
    assert "teammate note" in log.stdout and "grayson knowledge" in log.stdout


def test_repo_status_throttles_fetch(tmp_path, monkeypatch):
    from grayson import library as lib_mod

    origin = tmp_path / "origin.git"
    origin.mkdir()
    assert _git("init", "--bare", str(origin)).returncode == 0
    seed = tmp_path / "seed"
    assert _git("clone", str(origin), str(seed)).returncode == 0
    _git("config", "user.email", "t@example.com", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    assert _git("push", "-u", "origin", "HEAD", cwd=seed).returncode == 0

    clone = tmp_path / "clone"
    assert _git("clone", str(origin), str(clone)).returncode == 0

    calls: list[str] = []
    real_git = lib_mod._git

    def counting_git(repo, *args, **kwargs):
        calls.append(args[0])
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(lib_mod, "_git", counting_git)
    first = lib_mod.repo_status(clone)
    assert first["fetch_cached"] is False and calls.count("fetch") == 1
    # within the TTL the fresh FETCH_HEAD stands in for another network trip
    second = lib_mod.repo_status(clone)
    assert second["fetch_cached"] is True and calls.count("fetch") == 1
    assert second["fetch_ok"] is True
