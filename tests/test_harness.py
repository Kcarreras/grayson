from __future__ import annotations

import json

import pytest

from grayson.harness import generate_harness
from grayson.harness.mcp import SERVER_NAME, apply_mcp, mcp_status, remove_mcp


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


def test_copilot_instructions(tmp_path):
    out = generate_harness(tmp_path, "copilot")
    target = tmp_path / ".github" / "copilot-instructions.md"
    assert target.is_file()
    assert ".github/copilot-instructions.md" in out["written"]
    text = target.read_text()
    assert "checkpoint complete" in text
    assert "grayson:start" in text and "grayson:end" in text


def test_copilot_instructions_preserve_and_idempotent(tmp_path):
    target = tmp_path / ".github" / "copilot-instructions.md"
    target.parent.mkdir(parents=True)
    target.write_text("# House rules\n\nAlways use uv.\n")
    generate_harness(tmp_path, "copilot")
    generate_harness(tmp_path, "copilot")
    text = target.read_text()
    assert "Always use uv." in text
    assert text.count("grayson:start") == 1  # replaced, not duplicated


def test_no_mcp_flag(tmp_path):
    generate_harness(tmp_path, "cursor", with_mcp=False)
    text = (tmp_path / ".cursor" / "rules" / "grayson.mdc").read_text()
    assert "## MCP" not in text


def test_unknown_harness(tmp_path):
    with pytest.raises(ValueError, match="unknown harness"):
        generate_harness(tmp_path, "emacs")


# -- MCP config writers ---------------------------------------------------


@pytest.mark.parametrize(
    ("harness", "rel", "key"),
    [
        ("claude-code", ".mcp.json", "mcpServers"),
        ("cursor", ".cursor/mcp.json", "mcpServers"),
        ("copilot", ".vscode/mcp.json", "servers"),
    ],
)
def test_mcp_apply_status_remove_roundtrip(tmp_path, harness, rel, key):
    assert mcp_status(tmp_path, harness)["configured"] is False
    result = apply_mcp(tmp_path, harness)
    assert result["written"] is True
    data = json.loads((tmp_path / rel).read_text())
    entry = data[key][SERVER_NAME]
    assert entry["command"] == "grayson" and entry["args"] == ["mcp", "serve"]
    status = mcp_status(tmp_path, harness)
    assert status["configured"] is True and status["matches"] is True
    assert apply_mcp(tmp_path, harness)["written"] is False  # idempotent
    assert remove_mcp(tmp_path, harness)["removed"] is True
    assert mcp_status(tmp_path, harness)["configured"] is False


def test_mcp_copilot_entry_is_stdio(tmp_path):
    apply_mcp(tmp_path, "copilot")
    data = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())
    assert data["servers"][SERVER_NAME]["type"] == "stdio"


def test_mcp_preserves_other_servers(tmp_path):
    cfg = tmp_path / ".cursor" / "mcp.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}}))
    apply_mcp(tmp_path, "cursor")
    remove_mcp(tmp_path, "cursor")
    data = json.loads(cfg.read_text())
    assert data["mcpServers"] == {"other": {"command": "other"}}


def test_mcp_codex_gets_guidance(tmp_path):
    out = apply_mcp(tmp_path, "codex")
    assert out["supported"] is False
    assert "~/.codex/config.toml" in out["guidance"]  # user-global — not written
    unknown = mcp_status(tmp_path, "windsurf")
    assert unknown["supported"] is False


def test_mcp_broken_config_surfaces_error(tmp_path):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text("{not json")
    assert "error" in mcp_status(tmp_path, "claude-code")
    with pytest.raises(ValueError):
        apply_mcp(tmp_path, "claude-code")


# -- workflow-author skill --------------------------------------------------


def test_workflow_author_skill_written_per_harness(tmp_path):
    """One canonical SKILL.md body lands at each harness's native skills
    location (the shared open format); Codex, with no skills mechanism, gets a
    second marked AGENTS.md section that coexists with the protocol section."""
    from grayson.harness import generate_harness

    skill_dirs = {
        "claude-code": ".claude/skills/grayson-workflow-author/SKILL.md",
        "cursor": ".cursor/skills/grayson-workflow-author/SKILL.md",
        "copilot": ".github/skills/grayson-workflow-author/SKILL.md",
    }
    for harness, rel in skill_dirs.items():
        root = tmp_path / harness
        root.mkdir()
        written = generate_harness(root, harness)["written"]
        assert rel in written
        text = (root / rel).read_text(encoding="utf-8")
        assert text.startswith("---\nname: grayson-workflow-author\n")
        assert "workflow preview" in text and "MEANINGLESS without" in text
        assert (root / ".grayson" / "WORKFLOW_AUTHOR.md").is_file()

    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    generate_harness(codex_root, "codex")
    agents = (codex_root / "AGENTS.md").read_text(encoding="utf-8")
    assert "<!-- grayson:start -->" in agents
    assert "<!-- grayson-workflow-author:start -->" in agents
    # regenerating replaces each section in place, never duplicates
    generate_harness(codex_root, "codex")
    agents = (codex_root / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.count("<!-- grayson-workflow-author:start -->") == 1
    assert agents.count("<!-- grayson:start -->") == 1
