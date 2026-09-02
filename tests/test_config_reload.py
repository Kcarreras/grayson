"""A long-lived process sees grayson.toml edits: the config cache follows the file."""

from __future__ import annotations

import asyncio
import json
import os

from grayson.config import CONFIG_FILENAME
from grayson.config_edit import set_guard_profile
from grayson.mcp.server import build_server


def _bump_mtime(path) -> None:
    # Same-second edits are the realistic case (console save, then agent call);
    # make sure the test does not pass merely because a second ticked over.
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1))


def test_config_follows_on_disk_edit(workspace):
    assert workspace.config.guard_profiles["generous"].timeout_seconds == 300
    set_guard_profile(workspace.root, "generous", {"timeout_seconds": 800})
    _bump_mtime(workspace.root / CONFIG_FILENAME)
    # no reload_config() call: the edit must be visible on the next plain read
    assert workspace.config.guard_profiles["generous"].timeout_seconds == 800


def test_config_is_cached_between_reads_when_unchanged(workspace):
    first = workspace.config
    assert workspace.config is first


def test_reload_config_still_forces(workspace):
    first = workspace.config
    assert workspace.reload_config() is not first


def test_mcp_session_start_uses_profile_as_edited_after_server_started(workspace, fake_snow_env):
    """The bug: the MCP server outlives a console edit to the profile, and
    sessions it starts snapshot the pre-edit numbers."""
    server = build_server(workspace)
    assert workspace.config.guard_profiles["generous"].timeout_seconds == 300  # server warmed
    set_guard_profile(workspace.root, "generous", {"timeout_seconds": 800})
    _bump_mtime(workspace.root / CONFIG_FILENAME)

    result = asyncio.run(
        server.call_tool(
            "session_start",
            {"workflow": "table-health", "tables": ["DB.S.T1"], "guard_profile": "generous"},
        )
    )
    content = getattr(result, "content", None) or []
    out = json.loads(content[0].text)
    assert out["session"]["guard_profile"] == "generous"
    assert out["session"]["guard"]["timeout_seconds"] == 800
