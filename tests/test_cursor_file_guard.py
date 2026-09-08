"""Run the generated Cursor hook against real session state and tool payloads."""

from __future__ import annotations

import json
import os
import runpy
import sqlite3
import subprocess
import sys
from io import StringIO

import pytest

from grayson.core import file_fixes, proposals
from grayson.core.session import Session
from grayson.harness.mcp import apply_mcp
from grayson.harness.permissions import apply_guard, guard_status


@pytest.fixture
def investigation(workspace):
    apply_guard(workspace.root, "cursor")
    return Session.create(
        workspace,
        workflow="bug-hunter",
        targets=[],
        guard=workspace.config.resolve_profile("moderate"),
        guard_profile="moderate",
    )


def _hook(root, event):
    result = subprocess.run(
        [
            os.environ.get("GRAYSON_TEST_GUARD_PYTHON", sys.executable),
            str(root / ".cursor/hooks/grayson-guard.py"),
        ],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "tool", ["Write", "Edit", "StrReplace", "Delete", "ApplyPatch", "UnknownTool"]
)
def test_direct_writes_denied_even_before_any_proposal(investigation, tool):
    assert investigation.proposals() == []
    result = _hook(
        investigation.workspace.root,
        {
            "hook_event_name": "preToolUse",
            "tool_name": tool,
            "tool_input": {"file_path": "model.sql", "contents": "changed"},
        },
    )
    assert result["permission"] == "deny"
    assert "proposal_draft_file" in result["agent_message"]


@pytest.mark.parametrize(
    "command",
    [
        "python edit.py",
        "git apply fix.patch",
        "echo x > model.sql",
        "powershell -Command Set-Content model.sql x",
        "grayson proposal approve s p",
        "curl http://127.0.0.1:8501/approve",
        "npm test",
    ],
)
@pytest.mark.parametrize("hook", ["beforeShellExecution", "preToolUse"])
def test_shell_denied_including_indirect_writes(investigation, command, hook):
    event = {"hook_event_name": hook, "tool_name": "Shell"}
    if hook == "preToolUse":
        event["tool_input"] = {"command": command}
    else:
        event["command"] = command
    assert _hook(investigation.workspace.root, event)["permission"] == "deny"


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_native_read_tools_remain_available(investigation, tool):
    event = {
        "hook_event_name": "preToolUse",
        "tool_name": tool,
        "tool_input": {"path": "model.sql"},
    }
    assert _hook(investigation.workspace.root, event)["permission"] == "allow"
    event["tool_input"]["path"] = ".grayson/sessions/session/state.db"
    assert _hook(investigation.workspace.root, event)["permission"] == "deny"


@pytest.mark.parametrize(
    "server,permission", [("grayson", "allow"), ("filesystem", "deny"), ("", "deny")]
)
def test_mcp_identity_checked_before_execution(investigation, server, permission):
    event = {
        "hook_event_name": "beforeMCPExecution",
        "mcp_server_name": server,
        "tool_name": "proposal_draft_file",
        "tool_input": json.dumps({"target_file": "model.sql"}),
        "command": "grayson mcp serve",
    }
    assert _hook(investigation.workspace.root, event)["permission"] == permission


@pytest.mark.parametrize("index", [0, 2])
@pytest.mark.parametrize("tool", ["knowledge_sync", "query_run", "session_close"])
def test_cursor_project_identity_stays_available_after_session_start(investigation, index, tool):
    root = investigation.workspace.root
    apply_mcp(root, "cursor")
    event = {
        "hook_event_name": "beforeMCPExecution",
        "mcp_server_name": f"project-{index}-{root.name}-grayson",
        "tool_name": tool,
        "tool_input": {"session_id": investigation.id},
        "command": "grayson mcp serve",
    }
    result = _hook(root, event)
    assert result["permission"] == "allow", result
    assert (
        _hook(root, {"hook_event_name": "preToolUse", "tool_name": "Write"})["permission"] == "deny"
    )


@pytest.mark.parametrize("config_kind", ["missing", "malformed", "unregistered", "collision"])
def test_qualified_identity_requires_unambiguous_local_registration(investigation, config_kind):
    root = investigation.workspace.root
    name = f"project-0-{root.name}-grayson"
    path = root / ".cursor/mcp.json"
    if config_kind == "malformed":
        path.write_text("{bad", encoding="utf-8")
    elif config_kind == "unregistered":
        path.write_text(json.dumps({"mcpServers": {"filesystem": {}}}), encoding="utf-8")
    elif config_kind == "collision":
        path.write_text(json.dumps({"mcpServers": {"grayson": {}, name: {}}}), encoding="utf-8")
    result = _hook(
        root,
        {
            "hook_event_name": "beforeMCPExecution",
            "mcp_server_name": name,
            "tool_name": "query_run",
            "command": "grayson mcp serve",
        },
    )
    assert result["permission"] == "deny"


@pytest.mark.parametrize(
    "name",
    [
        "project-0-other-workspace-grayson",
        "project-0-ws-filesystem",
        "project-0-ws-not-grayson",
        "project-0-ws-grayson-extra",
        "project-0-ws-grayson\n",
        "untrusted-grayson",
        "project-x-ws-grayson",
        "",
        None,
        ["grayson"],
    ],
)
def test_other_servers_cannot_claim_grayson_by_tool_or_command(investigation, name):
    root = investigation.workspace.root
    apply_mcp(root, "cursor")
    result = _hook(
        root,
        {
            "hook_event_name": "beforeMCPExecution",
            "mcp_server_name": name,
            "tool_name": "mcp_grayson_query_run",
            "command": "grayson mcp serve",
        },
    )
    assert result["permission"] == "deny"


def test_reported_cursor_identity_for_hyphenated_workspace(tmp_path):
    root = tmp_path / "sql-qa-workspace"
    session = root / ".grayson/sessions/example"
    session.mkdir(parents=True)
    con = sqlite3.connect(session / "state.db")
    con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
    con.execute("INSERT INTO meta VALUES ('stage', 'investigation')")
    con.commit()
    con.close()
    apply_guard(root, "cursor")
    apply_mcp(root, "cursor")
    result = _hook(
        root,
        {
            "hook_event_name": "beforeMCPExecution",
            "mcp_server_name": "project-0-sql-qa-workspace-grayson",
            "tool_name": "knowledge_sync",
        },
    )
    assert result["permission"] == "allow"


def test_approved_proposal_uses_controlled_apply_not_a_general_write_unlock(investigation):
    root = investigation.workspace.root
    source = root / "model.sql"
    source.write_text("old")
    p = file_fixes.draft(investigation, "model.sql", "new", "Fix")
    proposals.decide(investigation, p["pid"], True, digest=file_fixes.review_digest(p))
    assert (
        _hook(
            root,
            {
                "hook_event_name": "preToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": "model.sql", "contents": "new"},
            },
        )["permission"]
        == "deny"
    )
    assert (
        _hook(
            root,
            {
                "hook_event_name": "beforeMCPExecution",
                "mcp_server_name": "grayson",
                "tool_name": "proposal_apply",
                "tool_input": {"session_id": investigation.id, "pid": p["pid"]},
            },
        )["permission"]
        == "allow"
    )
    file_fixes.apply(investigation, p["pid"])
    assert source.read_text() == "new"


def test_closing_all_sessions_releases_editing_guard(investigation):
    event = {
        "hook_event_name": "preToolUse",
        "tool_name": "Write",
        "tool_input": {"path": "model.sql"},
    }
    root = investigation.workspace.root
    assert _hook(root, event)["permission"] == "deny"
    investigation.set_meta("stage", "closed")
    assert _hook(root, event)["permission"] == "allow"


def test_unreadable_session_and_bad_tool_input_fail_closed(investigation):
    root = investigation.workspace.root
    investigation.set_meta("stage", "closed")
    (root / ".grayson/sessions/incomplete").mkdir()
    result = _hook(root, {"hook_event_name": "preToolUse", "tool_name": "Write", "tool_input": {}})
    assert result["permission"] == "deny"
    assert "session state" in result["agent_message"]
    assert ".grayson/sessions/incomplete/state.db" in result["agent_message"]
    assert "unable to open database file" in result["agent_message"]
    assert not (root / ".grayson/sessions/incomplete/state.db").exists()
    result = _hook(
        root, {"hook_event_name": "preToolUse", "tool_name": "MCP", "tool_input": "{bad"}
    )
    assert result["permission"] == "deny"


@pytest.mark.parametrize("stage,permission", [("closed", "allow"), ("investigation", "deny")])
def test_wal_database_without_sidecars(tmp_path, stage, permission):
    session = tmp_path / ".grayson/sessions/example"
    session.mkdir(parents=True)
    database = session / "state.db"
    con = sqlite3.connect(database)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO meta VALUES ('stage', ?)", (stage,))
        con.commit()
    finally:
        con.close()
    assert not (session / "state.db-wal").exists()
    assert not (session / "state.db-shm").exists()
    apply_guard(tmp_path, "cursor")
    result = _hook(tmp_path, {"hook_event_name": "preToolUse", "tool_name": "Write"})
    assert result["permission"] == permission
    result = _hook(
        tmp_path, {"hook_event_name": "beforeMCPExecution", "mcp_server_name": "grayson"}
    )
    assert result["permission"] == "allow"


@pytest.mark.parametrize("failure_at", ["connect", "select"])
@pytest.mark.parametrize("with_error_code", [False, True])
@pytest.mark.parametrize("state_dir", [".grayson", ".seekql"])
def test_readonly_cantopen_fallback_reads_live_wal_and_keeps_guard(
    tmp_path, monkeypatch, capsys, failure_at, with_error_code, state_dir
):
    root = tmp_path / "workspace # café"
    session = root / state_dir / "sessions" / "example"
    session.mkdir(parents=True)
    database = session / "state.db"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    writer.execute("INSERT INTO meta VALUES ('stage', 'closed')")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Main database says closed; the committed WAL now says investigation open.
    writer.execute("UPDATE meta SET value='investigation' WHERE key='stage'")
    writer.commit()
    apply_guard(root, "cursor")
    hook = runpy.run_path(str(root / ".cursor/hooks/grayson-guard.py"))
    connect = sqlite3.connect
    attempts = []
    closed = []

    def cantopen():
        error = sqlite3.OperationalError("unable to open database file")
        if with_error_code:
            error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
        return error

    class Connection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("SELECT value FROM meta"):
                if self.readonly and failure_at == "select":
                    raise cantopen()
                # The fallback must protect records even on its writable handle.
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    super().execute("UPDATE meta SET value='closed'")
            return super().execute(sql, *args)

        def close(self):
            closed.append(self.readonly)
            super().close()

    def simulated_connect(database_uri, **kwargs):
        readonly = database_uri.endswith("?mode=ro")
        assert readonly or database_uri.endswith("?mode=rw")
        assert "immutable" not in database_uri
        attempts.append("ro" if readonly else "rw")
        if readonly and failure_at == "connect":
            raise cantopen()
        con = connect(database_uri, **kwargs, factory=Connection)
        con.readonly = readonly
        return con

    monkeypatch.setattr(sqlite3, "connect", simulated_connect)

    def invoke(event):
        monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(event)))
        hook["main"]()
        return json.loads(capsys.readouterr().out)

    try:
        # Recovery allows Grayson to operate but must not unlock direct edits.
        result = invoke({"hook_event_name": "beforeMCPExecution", "mcp_server_name": "grayson"})
        assert result["permission"] == "allow", result
        write_event = {"hook_event_name": "preToolUse", "tool_name": "Write"}
        assert invoke(write_event)["permission"] == "deny"
        writer.execute("UPDATE meta SET value='closed' WHERE key='stage'")
        writer.commit()
        assert invoke(write_event)["permission"] == "allow"
        assert attempts == ["ro", "rw"] * 3
        assert closed == ([True, False] if failure_at == "select" else [False]) * 3
    finally:
        writer.close()


def test_successful_readonly_connection_never_requires_write_access(investigation, monkeypatch):
    root = investigation.workspace.root
    hook = runpy.run_path(str(root / ".cursor/hooks/grayson-guard.py"))
    connect = sqlite3.connect

    def readonly_only(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro")
        return connect(database_uri, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", readonly_only)
    assert hook["investigation_open"]()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "no_stage", "no_table"])
def test_invalid_database_still_denies_grayson_mcp(tmp_path, damage):
    root = tmp_path
    session = root / ".grayson/sessions/broken"
    session.mkdir(parents=True)
    database = session / "state.db"
    if damage == "corrupt":
        database.write_bytes(b"not a SQLite database")
    elif damage in {"no_stage", "no_table"}:
        con = sqlite3.connect(database)
        if damage == "no_stage":
            con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        con.close()
    apply_guard(root, "cursor")
    result = _hook(root, {"hook_event_name": "beforeMCPExecution", "mcp_server_name": "grayson"})
    assert result["permission"] == "deny"
    assert ".grayson/sessions/broken/state.db" in result["agent_message"]
    if damage == "missing":
        assert not database.exists()


def test_status_does_not_claim_edited_or_stale_guard_is_current(workspace):
    apply_guard(workspace.root, "cursor")
    assert guard_status(workspace.root, "cursor")["applied"]
    (workspace.root / ".cursor/hooks/grayson-guard.py").write_text("print('allow')")
    assert not guard_status(workspace.root, "cursor")["applied"]


@pytest.mark.skipif(os.name != "nt", reason="Windows command launcher")
def test_windows_launcher_executes_the_guard(investigation):
    result = subprocess.run(
        ["cmd.exe", "/c", str(investigation.workspace.root / ".cursor/hooks/grayson-guard.cmd")],
        input=json.dumps({"hook_event_name": "preToolUse", "tool_name": "Write", "tool_input": {}}),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout)["permission"] == "deny"
