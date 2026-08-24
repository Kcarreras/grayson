from __future__ import annotations

import pytest

from grayson.harness import generate_harness


def test_cursor_rule(tmp_path):
    generate_harness(tmp_path, "cursor")
    rule = tmp_path / ".cursor" / "rules" / "grayson.mdc"
    assert rule.is_file()
    text = rule.read_text()
    assert "grayson" in text and "checkpoint complete" in text
    assert (tmp_path / ".grayson" / "PROTOCOL.md").is_file()


def test_claude_code_section(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# My project\n\nExisting content.\n")
    generate_harness(tmp_path, "claude-code")
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "Existing content." in text
    assert "grayson:start" in text and "grayson:end" in text


def test_claude_code_section_idempotent(tmp_path):
    generate_harness(tmp_path, "claude-code")
    generate_harness(tmp_path, "claude-code")
    text = (tmp_path / "CLAUDE.md").read_text()
    assert text.count("grayson:start") == 1  # replaced, not duplicated


def test_codex_agents_md(tmp_path):
    generate_harness(tmp_path, "codex")
    assert (tmp_path / "AGENTS.md").is_file()


def test_no_mcp_flag(tmp_path):
    generate_harness(tmp_path, "cursor", with_mcp=False)
    text = (tmp_path / ".cursor" / "rules" / "grayson.mdc").read_text()
    assert "## MCP" not in text


def test_unknown_harness(tmp_path):
    with pytest.raises(ValueError, match="unknown harness"):
        generate_harness(tmp_path, "emacs")
