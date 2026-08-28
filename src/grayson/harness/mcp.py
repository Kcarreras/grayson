"""Harness MCP config writers.

grayson's MCP server (`grayson mcp serve`, stdio) mirrors the CLI one-to-one.
Where a harness keeps MCP config in a project-level file, grayson can write
the server entry itself. Same consent contract as the permission guard:
nothing here runs automatically — `grayson harness init` OFFERS it, `grayson
harness mcp apply|remove|status` manages it, and only the `grayson` server
entry is ever touched, so user-authored servers in the same file are kept.

Codex is guidance-only by design: its MCP config lives in the user-global
`~/.codex/config.toml`, and grayson writes repo files only.
"""

from __future__ import annotations

from pathlib import Path

from grayson.harness.permissions import _load, _write_json

SERVER_NAME = "grayson"

_STDIO = {"command": "grayson", "args": ["mcp", "serve"]}

#: harness -> (config file relative to repo root, top-level servers key, entry)
MCP_TARGETS: dict[str, tuple[str, str, dict]] = {
    "claude-code": (".mcp.json", "mcpServers", dict(_STDIO)),
    "cursor": (".cursor/mcp.json", "mcpServers", dict(_STDIO)),
    "copilot": (".vscode/mcp.json", "servers", {"type": "stdio", **_STDIO}),
}

MCP_GUIDANCE = {
    "codex": (
        "Codex reads MCP config from the user-global ~/.codex/config.toml, not "
        "a repo file, so grayson does not write it. Add:\n"
        "  [mcp_servers.grayson]\n"
        '  command = "grayson"\n'
        '  args = ["mcp", "serve"]\n'
        "Remember MCP servers run outside the Codex sandbox — under a "
        "network-disabled sandbox this makes MCP the warehouse path."
    ),
}

MCP_MANUAL_GUIDANCE = (
    "this harness has no project-level MCP config file grayson knows; register "
    "`grayson mcp serve` (stdio) in its MCP settings by hand"
)


def mcp_guidance(harness: str) -> str:
    return MCP_GUIDANCE.get(harness, MCP_MANUAL_GUIDANCE)


def mcp_status(root: Path, harness: str = "claude-code") -> dict:
    """Whether grayson's server entry is present in the harness MCP config."""
    if harness not in MCP_TARGETS:
        return {"harness": harness, "supported": False, "guidance": mcp_guidance(harness)}
    rel, key, entry = MCP_TARGETS[harness]
    path = root / rel
    try:
        servers = _load(path).get(key, {})
    except ValueError as e:
        return {"harness": harness, "supported": True, "file": str(path), "error": str(e)}
    current = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    return {
        "harness": harness,
        "supported": True,
        "file": str(path),
        "configured": current is not None,
        "matches": current == entry,
        "entry": entry,
    }


def apply_mcp(root: Path, harness: str = "claude-code") -> dict:
    """Write grayson's server entry (idempotent; other servers untouched)."""
    if harness not in MCP_TARGETS:
        return {"harness": harness, "supported": False, "guidance": mcp_guidance(harness)}
    rel, key, entry = MCP_TARGETS[harness]
    path = root / rel
    data = _load(path)
    servers = data.setdefault(key, {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: '{key}' must be an object")
    written = servers.get(SERVER_NAME) != entry
    servers[SERVER_NAME] = entry
    _write_json(path, data)
    return {
        "harness": harness,
        "supported": True,
        "file": str(path),
        "written": written,
        "entry": entry,
        "note": "stdio server — the agent's environment needs `grayson` on PATH",
    }


def remove_mcp(root: Path, harness: str = "claude-code") -> dict:
    """Remove exactly grayson's server entry; user-authored servers are kept."""
    if harness not in MCP_TARGETS:
        return {"harness": harness, "supported": False, "guidance": mcp_guidance(harness)}
    rel, key, _ = MCP_TARGETS[harness]
    path = root / rel
    if not path.is_file():
        return {"harness": harness, "supported": True, "file": str(path), "removed": False}
    data = _load(path)
    servers = data.get(key, {})
    removed = isinstance(servers, dict) and SERVER_NAME in servers
    if removed:
        del servers[SERVER_NAME]
        _write_json(path, data)
    return {"harness": harness, "supported": True, "file": str(path), "removed": removed}
