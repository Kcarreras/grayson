"""Harness permission guard: deny rules blocking the agent's bypass paths.

The query guard is airtight only for statements that pass through grayson; an
agent with arbitrary shell access could call `snow` directly with the user's
own credentials, read those credentials and connect without `snow` at all, or
read `.grayson/` state files. Where the harness has a
machine-readable permission config (Claude Code's `.claude/settings.json`),
grayson can write deny rules that turn "please don't" into a permission
prompt a human sees.

Deliberately consent-based: nothing here runs automatically. `grayson harness
init` OFFERS it, `grayson harness guard apply|remove|status` manages it, and
the exact rules are shown before they are written. This is friction and
visibility, not containment — the warehouse-side read-only role remains the
control that survives a full bypass (docs/SECURITY.md).
"""

from __future__ import annotations

import json
from pathlib import Path

#: the rules grayson manages (exactly these strings are added and removed, so
#: user-authored rules in the same file are never touched).
GUARD_DENY_RULES = [
    "Bash(snow:*)",  # direct Snowflake CLI use — all warehouse access goes through grayson
    # Blocking the `snow` binary while leaving its credentials readable is a
    # half-measure: the connection details (and any key-pair private key stored
    # beside them) are all an agent needs to reach the warehouse through the
    # Python connector or the REST API, with no `snow` invocation to match on.
    "Read(~/.snowflake/**)",
    "Read(~/.snowsql/**)",  # the legacy snowsql client's config lives here too
    "Read(.grayson/**)",  # session state, cache, audit — read via grayson tools only
    "Edit(.grayson/**)",
    "Write(.grayson/**)",
]

#: harnesses grayson cannot (yet) write config for get concrete, per-harness
#: setup instructions naming their real enforcement mechanism — not a shrug.
HARNESS_GUIDANCE = {
    "cursor": (
        "Cursor has two layers for this, both set up by a human:\n"
        "1. Command denylist — in Cursor's agent/terminal settings, add `snow` "
        "to the command denylist so it is never auto-run: any direct warehouse "
        "call surfaces as a prompt a human sees.\n"
        "2. Hooks (where available) — a `beforeShellExecution` hook in "
        ".cursor/hooks.json can hard-deny commands matching `snow`, reads of "
        "~/.snowflake/ (the connection details are all an agent needs to reach "
        "the warehouse without `snow` at all), or paths under .grayson/; see "
        "Cursor's hooks documentation for the exact hook script contract. A hook "
        "beats a denylist here because it can default-deny and normalize the "
        "command rather than matching one literal string.\n"
        "Note the denylist and hooks govern the IDE agent; `cursor-agent` (the "
        "Cursor CLI) has its own permission config, set separately — for "
        "CLI-driven use, lean on the MCP server as the interface and configure "
        "the CLI's own allow/deny rules to block `snow`.\n"
        "Point the agent at grayson via the Cursor rule (`grayson harness init "
        "cursor` — the CLI reads project rules too) and/or the MCP server, and "
        "pair with a read-only Snowflake role — the control that holds "
        "regardless of harness settings."
    ),
    "codex": (
        "Codex's OS-level sandbox is the enforcement layer:\n"
        "1. Keep the default `workspace-write` sandbox with network access "
        "disabled — shell commands then cannot reach the warehouse at all, so "
        "a direct `snow` call fails by construction. Avoid "
        "`danger-full-access` and never-ask approval for grayson work.\n"
        "2. Register grayson as an MCP server in ~/.codex/config.toml "
        '(`[mcp_servers.grayson] command = "grayson", args = ["mcp", '
        '"serve"]`): MCP servers run OUTSIDE the sandbox, so the guarded '
        "path reaches Snowflake while the bypass path does not. Note this "
        "makes MCP the warehouse path — the grayson CLI's query commands "
        "would be sandboxed away from the network too.\n"
        "Pair with a read-only Snowflake role — the control that holds "
        "regardless of harness settings."
    ),
}

MANUAL_GUIDANCE = (
    "this harness has no machine-writable permission config grayson knows; "
    "configure its command allowlist to deny `snow` (and warehouse SDK "
    "invocations) for agent sessions, deny reads of ~/.snowflake/ so the "
    "credentials cannot simply be used without `snow`, and pair it with a "
    "read-only Snowflake role — that role is the control that holds even "
    "without harness support"
)


def harness_guidance(harness: str) -> str:
    return HARNESS_GUIDANCE.get(harness, MANUAL_GUIDANCE)


def _settings_path(root: Path) -> Path:
    return root / ".claude" / "settings.json"


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON — fix it by hand first: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def guard_status(root: Path, harness: str = "claude-code") -> dict:
    """Which of grayson's deny rules are present in the harness config."""
    if harness != "claude-code":
        return {"harness": harness, "supported": False, "guidance": harness_guidance(harness)}
    path = _settings_path(root)
    try:
        deny = _load(path).get("permissions", {}).get("deny", [])
    except ValueError as e:
        return {"harness": harness, "supported": True, "file": str(path), "error": str(e)}
    present = [r for r in GUARD_DENY_RULES if r in deny]
    missing = [r for r in GUARD_DENY_RULES if r not in deny]
    return {
        "harness": harness,
        "supported": True,
        "file": str(path),
        "applied": not missing,
        "present": present,
        "missing": missing,
    }


def apply_guard(root: Path, harness: str = "claude-code") -> dict:
    """Add grayson's deny rules (idempotent; other settings untouched)."""
    if harness != "claude-code":
        return {"harness": harness, "supported": False, "guidance": harness_guidance(harness)}
    path = _settings_path(root)
    data = _load(path)
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise ValueError(f"{path}: 'permissions' must be an object")
    deny = perms.setdefault("deny", [])
    if not isinstance(deny, list):
        raise ValueError(f"{path}: 'permissions.deny' must be a list")
    added = [r for r in GUARD_DENY_RULES if r not in deny]
    deny.extend(added)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {
        "harness": harness,
        "supported": True,
        "file": str(path),
        "added": added,
        "rules": GUARD_DENY_RULES,
        "note": "friction + visibility, not containment — pair with a read-only "
        "Snowflake role for the guarantee that survives a bypass. These rules match "
        "tool calls and command strings, so they stop the direct path, not a determined "
        "one; a key-pair private key stored outside ~/.snowflake is not covered and "
        "cannot be, since its location is yours to choose.",
    }


def remove_guard(root: Path, harness: str = "claude-code") -> dict:
    """Remove exactly grayson's deny rules; user-authored rules are kept."""
    if harness != "claude-code":
        return {"harness": harness, "supported": False, "guidance": harness_guidance(harness)}
    path = _settings_path(root)
    if not path.is_file():
        return {"harness": harness, "supported": True, "file": str(path), "removed": []}
    data = _load(path)
    deny = data.get("permissions", {}).get("deny", [])
    removed = [r for r in deny if r in GUARD_DENY_RULES]
    if removed:
        data["permissions"]["deny"] = [r for r in deny if r not in GUARD_DENY_RULES]
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"harness": harness, "supported": True, "file": str(path), "removed": removed}
