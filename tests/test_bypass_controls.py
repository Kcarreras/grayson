"""Bypass controls: harness deny rules, HTTP bearer wall, audit reconciliation."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from grayson.audit import reconcile, reconcile_check_result
from grayson.cli import app
from grayson.executor.snow import ExecutionResult
from grayson.harness.permissions import (
    COPILOT_AUTOAPPROVE_RULES,
    GUARD_DENY_RULES,
    apply_guard,
    guard_status,
    remove_guard,
)

runner = CliRunner()


# -- harness guard permissions -------------------------------------------


def test_apply_status_remove_roundtrip(tmp_path):
    assert guard_status(tmp_path)["applied"] is False
    result = apply_guard(tmp_path)
    assert result["added"] == GUARD_DENY_RULES
    status = guard_status(tmp_path)
    assert status["applied"] is True and status["missing"] == []
    assert apply_guard(tmp_path)["added"] == []  # idempotent
    assert remove_guard(tmp_path)["removed"] == GUARD_DENY_RULES
    assert guard_status(tmp_path)["applied"] is False


def test_user_settings_preserved(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "env": {"FOO": "bar"},
                "permissions": {"deny": ["Bash(rm:*)"], "allow": ["Bash(ls:*)"]},
            }
        ),
        encoding="utf-8",
    )
    apply_guard(tmp_path)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["env"] == {"FOO": "bar"}
    assert "Bash(rm:*)" in data["permissions"]["deny"]
    assert all(r in data["permissions"]["deny"] for r in GUARD_DENY_RULES)
    remove_guard(tmp_path)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"]["deny"] == ["Bash(rm:*)"]  # only ours removed
    assert data["permissions"]["allow"] == ["Bash(ls:*)"]


def test_copilot_apply_status_remove_roundtrip(tmp_path):
    assert guard_status(tmp_path, harness="copilot")["applied"] is False
    result = apply_guard(tmp_path, harness="copilot")
    assert result["added"] == list(COPILOT_AUTOAPPROVE_RULES)
    status = guard_status(tmp_path, harness="copilot")
    assert status["applied"] is True and status["missing"] == []
    assert apply_guard(tmp_path, harness="copilot")["added"] == []  # idempotent
    assert remove_guard(tmp_path, harness="copilot")["removed"] == list(COPILOT_AUTOAPPROVE_RULES)
    assert guard_status(tmp_path, harness="copilot")["applied"] is False


def test_copilot_user_settings_preserved(tmp_path):
    settings = tmp_path / ".vscode" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "editor.formatOnSave": True,
                "chat.tools.terminal.autoApprove": {"rm": False, "ls": True},
            }
        ),
        encoding="utf-8",
    )
    apply_guard(tmp_path, harness="copilot")
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["editor.formatOnSave"] is True
    auto = data["chat.tools.terminal.autoApprove"]
    assert auto["rm"] is False and auto["ls"] is True
    assert auto["snow"] is False
    remove_guard(tmp_path, harness="copilot")
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["chat.tools.terminal.autoApprove"] == {"rm": False, "ls": True}  # only ours removed


def test_copilot_repointed_entry_is_kept(tmp_path):
    # a user who flipped our entry to auto-approve owns it now: remove leaves it
    apply_guard(tmp_path, harness="copilot")
    settings = tmp_path / ".vscode" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["chat.tools.terminal.autoApprove"]["snow"] = True
    settings.write_text(json.dumps(data), encoding="utf-8")
    removed = remove_guard(tmp_path, harness="copilot")["removed"]
    assert "snow" not in removed
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["chat.tools.terminal.autoApprove"]["snow"] is True


def test_unwritable_harnesses_get_specific_guidance(tmp_path):
    codex = guard_status(tmp_path, harness="codex")
    assert codex["supported"] is False
    assert "workspace-write" in codex["guidance"]  # sandbox blocks network egress
    assert "mcp_servers" in codex["guidance"]  # MCP runs outside the sandbox
    unknown = guard_status(tmp_path, harness="windsurf")
    assert "read-only Snowflake role" in unknown["guidance"]  # generic fallback


def test_cursor_manual_guidance_still_names_both_layers():
    from grayson.harness.permissions import harness_guidance

    guidance = harness_guidance("cursor")
    assert "command denylist" in guidance
    assert "beforeShellExecution" in guidance  # hooks: the hard-deny layer


def test_cursor_hooks_apply_status_remove_roundtrip(tmp_path):
    assert guard_status(tmp_path, harness="cursor")["applied"] is False
    result = apply_guard(tmp_path, harness="cursor")
    assert result["added"] == ["beforeShellExecution", "beforeReadFile"]
    assert result["script_written"] is True
    hooks_file = tmp_path / ".cursor" / "hooks.json"
    script = tmp_path / ".cursor" / "hooks" / "grayson-guard.py"
    assert script.is_file() and script.stat().st_mode & 0o111  # executable
    data = json.loads(hooks_file.read_text())
    entry = {"command": "./.cursor/hooks/grayson-guard.py"}
    assert entry in data["hooks"]["beforeShellExecution"]
    assert entry in data["hooks"]["beforeReadFile"]
    status = guard_status(tmp_path, harness="cursor")
    assert status["applied"] is True and status["missing"] == []
    again = apply_guard(tmp_path, harness="cursor")
    assert again["added"] == [] and again["script_written"] is False  # idempotent
    removed = remove_guard(tmp_path, harness="cursor")
    assert removed["removed"] == ["beforeShellExecution", "beforeReadFile"]
    assert removed["script_removed"] is True and not script.exists()
    assert guard_status(tmp_path, harness="cursor")["applied"] is False


def test_cursor_hooks_preserve_user_entries(tmp_path):
    hooks_file = tmp_path / ".cursor" / "hooks.json"
    hooks_file.parent.mkdir(parents=True)
    hooks_file.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {"beforeShellExecution": [{"command": "./mine.sh"}]},
            }
        ),
        encoding="utf-8",
    )
    apply_guard(tmp_path, harness="cursor")
    remove_guard(tmp_path, harness="cursor")
    data = json.loads(hooks_file.read_text())
    assert data["hooks"] == {"beforeShellExecution": [{"command": "./mine.sh"}]}


def test_cursor_edited_script_is_kept_on_remove(tmp_path):
    apply_guard(tmp_path, harness="cursor")
    script = tmp_path / ".cursor" / "hooks" / "grayson-guard.py"
    script.write_text(script.read_text() + "\n# user tweak\n", encoding="utf-8")
    result = remove_guard(tmp_path, harness="cursor")
    assert result["script_removed"] is False
    assert script.is_file()  # edited script is the user's now


@pytest.mark.parametrize(
    ("event", "verdict"),
    [
        ({"command": "snow sql -q 'select 1'"}, "deny"),
        ({"command": "uv run snow --help"}, "deny"),
        ({"command": "cat .grayson/audit.jsonl"}, "deny"),
        ({"file_path": "/repo/.grayson/sessions/s_0001.json"}, "deny"),
        ({"command": "ls -la"}, "allow"),
        ({"command": "echo snowflake-is-a-word"}, "allow"),  # substring, not the CLI
        ({"file_path": "/repo/src/main.py"}, "allow"),
        ({}, "allow"),  # unknown shape fails open
    ],
)
def test_cursor_hook_script_verdicts(tmp_path, event, verdict):
    import subprocess
    import sys as _sys

    apply_guard(tmp_path, harness="cursor")
    script = tmp_path / ".cursor" / "hooks" / "grayson-guard.py"
    proc = subprocess.run(
        [_sys.executable, str(script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(proc.stdout)["permission"] == verdict


def test_cursor_hook_script_fails_open_on_garbage(tmp_path):
    import subprocess
    import sys as _sys

    apply_guard(tmp_path, harness="cursor")
    script = tmp_path / ".cursor" / "hooks" / "grayson-guard.py"
    proc = subprocess.run(
        [_sys.executable, str(script)],
        input="{not json",
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(proc.stdout)["permission"] == "allow"


def test_broken_settings_surfaces_error(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    assert "error" in guard_status(tmp_path)
    with pytest.raises(ValueError):
        apply_guard(tmp_path)


def test_cli_harness_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["harness", "guard", "apply"])
    assert result.exit_code == 0
    assert json.loads(result.output)["added"] == GUARD_DENY_RULES
    result = runner.invoke(app, ["harness", "guard", "status"])
    assert json.loads(result.output)["applied"] is True


def test_cli_harness_init_with_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["harness", "init", "claude-code", "--path", str(tmp_path), "--guard-permissions"]
    )
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out["guard_permissions"]["added"] == GUARD_DENY_RULES
    # harnesses without a writable config get the concrete setup steps instead
    result = runner.invoke(app, ["harness", "init", "codex", "--path", str(tmp_path)])
    out = json.loads(result.output)
    assert "hint" not in out
    assert "workspace-write" in out["guard_guidance"]
    assert "~/.codex/config.toml" in out["mcp_guidance"]  # MCP config is user-global
    # cursor without consent: manual copy/paste guidance, nothing written
    result = runner.invoke(app, ["harness", "init", "cursor", "--path", str(tmp_path)])
    assert "command denylist" in json.loads(result.output)["guard_guidance"]
    assert not (tmp_path / ".cursor" / "hooks.json").exists()


def test_cli_harness_init_cursor_with_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app, ["harness", "init", "cursor", "--path", str(tmp_path), "--guard-permissions"]
    )
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out["guard_permissions"]["added"] == ["beforeShellExecution", "beforeReadFile"]
    assert "guard_guidance" not in out  # took the machine-written path
    assert (tmp_path / ".cursor" / "hooks.json").is_file()
    assert (tmp_path / ".cursor" / "hooks" / "grayson-guard.py").is_file()


def test_cli_harness_init_copilot_full(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "harness",
            "init",
            "copilot",
            "--path",
            str(tmp_path),
            "--guard-permissions",
            "--mcp-config",
        ],
    )
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert ".github/copilot-instructions.md" in out["written"]
    assert out["guard_permissions"]["added"] == list(COPILOT_AUTOAPPROVE_RULES)
    assert out["mcp_config"]["written"] is True
    assert (tmp_path / ".vscode" / "settings.json").is_file()
    assert (tmp_path / ".vscode" / "mcp.json").is_file()


def test_cli_harness_init_writes_nothing_without_consent(tmp_path, monkeypatch):
    # non-interactive, no flags: instruction file only — hints, no config writes
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["harness", "init", "copilot", "--path", str(tmp_path)])
    out = json.loads(result.output)
    assert "guard_permissions" not in out and "mcp_config" not in out
    assert "hint" in out and "mcp_hint" in out
    assert not (tmp_path / ".vscode").exists()


def test_cli_harness_mcp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["harness", "mcp", "apply", "--harness", "cursor"])
    assert result.exit_code == 0
    assert json.loads(result.output)["written"] is True
    result = runner.invoke(app, ["harness", "mcp", "status", "--harness", "cursor"])
    out = json.loads(result.output)
    assert out["configured"] is True and out["matches"] is True
    result = runner.invoke(app, ["harness", "mcp", "remove", "--harness", "cursor"])
    assert json.loads(result.output)["removed"] is True


# -- HTTP bearer wall ----------------------------------------------------


def test_bearer_auth_wall():
    from fastapi.testclient import TestClient

    from grayson.mcp.server import BearerAuthASGI

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    client = TestClient(BearerAuthASGI(inner, "sekrit"))
    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/mcp", headers={"Authorization": "bearer sekrit"}).status_code == 401
    ok = client.get("/mcp", headers={"Authorization": "Bearer sekrit"})
    assert ok.status_code == 200 and ok.text == "ok"


def test_bearer_wall_denies_websocket_scopes():
    # The wall default-denies by scope type: a websocket upgrade must be closed,
    # not passed through to the app because only `http` was inspected.
    import asyncio

    from grayson.mcp.server import BearerAuthASGI

    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["type"])

    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    wall = BearerAuthASGI(inner, "sekrit")
    asyncio.run(wall({"type": "websocket", "path": "/mcp"}, receive, send))
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert reached == []


def test_healthz_answers_without_token():
    from fastapi.testclient import TestClient

    from grayson.mcp.server import BearerAuthASGI, HealthzASGI

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    client = TestClient(HealthzASGI(BearerAuthASGI(inner, "sekrit")))
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    # everything else still hits the wall
    assert client.get("/mcp").status_code == 401
    assert client.post("/healthz").status_code == 401  # liveness is GET/HEAD only


def test_mcp_serve_rejects_empty_library_value():
    # The container entrypoint passes --library "$GRAYSON_LIBRARY_URL"; unset env
    # must produce an error naming the env var, not workspace-discovery noise.
    result = runner.invoke(app, ["mcp", "serve", "--knowledge-only", "--library", ""])
    assert result.exit_code == 1
    assert "GRAYSON_LIBRARY_URL" in result.output


def test_http_flags_mutually_exclusive(workspace):
    result = runner.invoke(
        app, ["mcp", "serve", "--http", "--no-token", "--token", "x", "--knowledge-only"]
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


# -- audit reconcile -----------------------------------------------------


class HistoryExecutor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, timeout_seconds=0):
        assert "QUERY_HISTORY" in sql
        return ExecutionResult(status="ok", rows=self.rows, columns=[])


def _history_row(text, qid="q"):
    return {
        "QUERY_ID": qid,
        "QUERY_TEXT": text,
        "USER_NAME": "KC",
        "START_TIME": "2026-08-26 10:00:00",
        "EXECUTION_STATUS": "SUCCESS",
    }


def test_reconcile_classifies(workspace, fake_snow_env):
    from grayson.config import GuardSettings
    from grayson.core.run import run_statement
    from grayson.core.session import Session

    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    from conftest import FakeExecutor

    run_statement(s, "SELECT id FROM DB.S.T1", executor=FakeExecutor())
    executed = s.executed_statements()[0]["sql"]

    history = [
        _history_row(executed, "q1"),  # went through grayson
        _history_row("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 120", "q2"),
        _history_row("SELECT * FROM DB.S.SECRETS", "q3"),  # bypass
    ]
    report = reconcile(workspace, hours=24, executor=HistoryExecutor(history))
    assert report["matched_grayson"] == 1
    assert report["grayson_internal"] == 1
    assert [u["query_id"] for u in report["unmatched"]] == ["q3"]
    assert report["ok"] is False

    check = reconcile_check_result(report)
    assert check["status"] == "warn"
    assert "SECRETS" not in json.dumps(check)  # verdict only — no statement text


def test_reconcile_clean_passes(workspace):
    report = reconcile(workspace, hours=24, executor=HistoryExecutor([]))
    assert report["ok"] is True
    assert reconcile_check_result(report)["status"] == "pass"


def test_reconcile_refuses_sandbox(tmp_path, monkeypatch):
    from grayson.workspace import Workspace

    ws = Workspace.init(tmp_path / "sb")
    cfg = ws.root / "grayson.toml"
    cfg.write_text(cfg.read_text().replace('name = "default"', 'name = "sandbox"'))
    assert "error" in reconcile(Workspace(ws.root))


def test_guard_covers_the_credentials_not_just_the_binary():
    """Denying `snow` while leaving its credentials readable is a half-measure:
    connection details plus a key file are all an agent needs to reach the
    warehouse through the Python connector, with no `snow` call to match on."""
    assert "Bash(snow:*)" in GUARD_DENY_RULES
    assert any("snowflake" in r for r in GUARD_DENY_RULES if r.startswith("Read("))


def test_manual_and_cursor_guidance_name_the_credentials_path():
    from grayson.harness.permissions import MANUAL_GUIDANCE, harness_guidance

    assert ".snowflake" in MANUAL_GUIDANCE
    assert ".snowflake" in harness_guidance("cursor")
