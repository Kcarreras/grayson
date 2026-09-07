"""Run the generated Cursor hook against real session state and tool payloads."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from grayson.core import file_fixes, proposals
from grayson.core.session import Session
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
        [sys.executable, str(root / ".cursor/hooks/grayson-guard.py")],
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
    result = _hook(
        root, {"hook_event_name": "preToolUse", "tool_name": "MCP", "tool_input": "{bad"}
    )
    assert result["permission"] == "deny"


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
