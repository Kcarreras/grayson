"""Harness permission guard: deny rules blocking the agent's bypass paths.

The query guard is airtight only for statements that pass through grayson; an
agent with arbitrary shell access could call `snow` directly with the user's
own credentials, read those credentials and connect without `snow` at all, or
read `.grayson/` state files. Where the harness has a
machine-writable permission config (Claude Code's `.claude/settings.json`,
VS Code / Copilot's `chat.tools.terminal.autoApprove` in
`.vscode/settings.json`, Cursor's `.cursor/hooks.json` + hook script),
grayson can write deny rules that turn "please don't" into a permission
prompt a human sees — or, for Cursor hooks, a hard deny.

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

#: VS Code / Copilot agent mode: entries grayson manages in
#: `chat.tools.terminal.autoApprove` (.vscode/settings.json). `false` forces a
#: human-visible approval prompt for matching terminal commands. This setting
#: governs the terminal only — Copilot's own file tools can still read
#: `.grayson/`, so that path stays prose-guarded (protocol) + audit-reconciled.
COPILOT_AUTOAPPROVE_RULES: dict[str, bool] = {
    "snow": False,  # direct Snowflake CLI use — never auto-approved
    "/\\.grayson\\b/": False,  # shell commands touching session state/audit files
}

#: the manual setup path, per harness. For harnesses grayson cannot write
#: config for this is the only path; for Cursor it is the alternative offered
#: when the machine-written hook is declined (copy/paste or define your own).
HARNESS_GUIDANCE = {
    "cursor": (
        "Manual Cursor setup — two layers, both configured by a human:\n"
        "1. Command denylist — in Cursor's agent/terminal settings (app "
        "settings, not a repo file), add `snow` to the command denylist so it "
        "is never auto-run: any direct warehouse call surfaces as a prompt a "
        "human sees.\n"
        "2. Hooks — a `beforeShellExecution` hook in .cursor/hooks.json can "
        "hard-deny commands matching `snow`, reads of ~/.snowflake/ (the "
        "connection details are all an agent needs to reach the warehouse "
        "without `snow` at all), or paths under .grayson/; "
        "`grayson harness guard apply --harness cursor` writes exactly this "
        "(hook + script) if you'd rather not hand-roll it, or see Cursor's "
        "hooks documentation for the script contract to define your own. A hook "
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


def guard_rules_display(harness: str) -> list[str]:
    """The exact rules a guard apply would write, rendered for a consent prompt."""
    if harness == "copilot":
        return [
            f'chat.tools.terminal.autoApprove  "{k}": {str(v).lower()}'
            for k, v in COPILOT_AUTOAPPROVE_RULES.items()
        ]
    if harness == "cursor":
        return [
            f"{ev} hook → {_CURSOR_SCRIPT_REL} (hard-deny, fail-closed: `snow`/"
            "`snowsql`, connector imports, Snowflake credential and private-key "
            "reads, and `.grayson/` access)"
            for ev in _CURSOR_HOOK_EVENTS
        ]
    return list(GUARD_DENY_RULES)


def _settings_path(root: Path) -> Path:
    return root / ".claude" / "settings.json"


def _vscode_settings_path(root: Path) -> Path:
    return root / ".vscode" / "settings.json"


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


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def guard_status(root: Path, harness: str = "claude-code") -> dict:
    """Which of grayson's deny rules are present in the harness config."""
    if harness == "copilot":
        return _copilot_guard_status(root)
    if harness == "cursor":
        return _cursor_guard_status(root)
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
    if harness == "copilot":
        return _copilot_apply_guard(root)
    if harness == "cursor":
        return _cursor_apply_guard(root)
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
    if harness == "copilot":
        return _copilot_remove_guard(root)
    if harness == "cursor":
        return _cursor_remove_guard(root)
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


# -- copilot (VS Code agent mode) -----------------------------------------
#
# `.vscode/settings.json` may legally contain comments (JSONC); grayson only
# reads/writes plain JSON, so a commented file surfaces the parse error with
# "fix it by hand first" rather than silently rewriting it.

_AUTOAPPROVE_KEY = "chat.tools.terminal.autoApprove"

_COPILOT_NOTE = (
    "terminal commands only — Copilot's file tools are not governed by this "
    "setting, and the cloud coding agent has its own environment; friction + "
    "visibility, pair with a read-only Snowflake role for the guarantee that "
    "survives a bypass"
)


def _copilot_guard_status(root: Path) -> dict:
    path = _vscode_settings_path(root)
    try:
        auto = _load(path).get(_AUTOAPPROVE_KEY, {})
    except ValueError as e:
        return {"harness": "copilot", "supported": True, "file": str(path), "error": str(e)}
    if not isinstance(auto, dict):
        auto = {}
    present = [k for k, v in COPILOT_AUTOAPPROVE_RULES.items() if auto.get(k) == v]
    missing = [k for k in COPILOT_AUTOAPPROVE_RULES if k not in present]
    return {
        "harness": "copilot",
        "supported": True,
        "file": str(path),
        "applied": not missing,
        "present": present,
        "missing": missing,
    }


def _copilot_apply_guard(root: Path) -> dict:
    path = _vscode_settings_path(root)
    data = _load(path)
    auto = data.setdefault(_AUTOAPPROVE_KEY, {})
    if not isinstance(auto, dict):
        raise ValueError(f"{path}: '{_AUTOAPPROVE_KEY}' must be an object")
    added = [k for k, v in COPILOT_AUTOAPPROVE_RULES.items() if auto.get(k) != v]
    for k in added:
        auto[k] = COPILOT_AUTOAPPROVE_RULES[k]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {
        "harness": "copilot",
        "supported": True,
        "file": str(path),
        "added": added,
        "rules": guard_rules_display("copilot"),
        "note": _COPILOT_NOTE,
    }


def _copilot_remove_guard(root: Path) -> dict:
    path = _vscode_settings_path(root)
    if not path.is_file():
        return {"harness": "copilot", "supported": True, "file": str(path), "removed": []}
    data = _load(path)
    auto = data.get(_AUTOAPPROVE_KEY, {})
    if not isinstance(auto, dict):
        return {"harness": "copilot", "supported": True, "file": str(path), "removed": []}
    # exactly our key/value pairs: an entry the user re-pointed (e.g. flipped
    # to true) is theirs now and stays
    removed = [k for k, v in COPILOT_AUTOAPPROVE_RULES.items() if auto.get(k) == v]
    if removed:
        for k in removed:
            del auto[k]
        if not auto:
            del data[_AUTOAPPROVE_KEY]
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"harness": "copilot", "supported": True, "file": str(path), "removed": removed}


# -- cursor (IDE agent hooks) ---------------------------------------------
#
# Cursor's project-level hooks (.cursor/hooks.json) can HARD-deny agent
# actions — stronger than a permission prompt. grayson writes the wiring plus
# an executable hook script; declining the write leaves the manual path
# (HARNESS_GUIDANCE["cursor"]: the app-settings command denylist, or a
# hand-rolled hook). Hooks need a recent Cursor IDE and do not govern the
# `cursor-agent` CLI.

_CURSOR_HOOK_EVENTS = ("beforeShellExecution", "beforeReadFile")
_CURSOR_SCRIPT_REL = ".cursor/hooks/grayson-guard.py"
_CURSOR_HOOK_COMMAND = f"./{_CURSOR_SCRIPT_REL}"
# failClosed: Cursor's default is fail-OPEN — a crashed or slow guard waves
# the action through, the wrong way for a guard to fail. With failClosed a
# timeout denies, so the generous timeout costs a confusing refusal at worst,
# never a hole.
_CURSOR_HOOK_ENTRY = {"command": _CURSOR_HOOK_COMMAND, "failClosed": True, "timeout": 10}

_CURSOR_NOTE = (
    "hard-deny, but version-dependent (recent Cursor IDE; hooks need an "
    "executable script, so POSIX) and IDE-only — the cursor-agent CLI has its "
    "own permission config; the hook fails CLOSED (a malformed event or a "
    "crashed guard denies); pair with a read-only Snowflake role for the "
    "guarantee that survives a bypass"
)


def _cursor_entry_is_ours(entry: object) -> bool:
    """Ours by command, whatever the entry's other keys: older grayson wrote
    `{command}` alone, this one adds failClosed/timeout, and apply upgrades
    the old shape in place instead of stacking a second registration."""
    return isinstance(entry, dict) and entry.get("command") == _CURSOR_HOOK_COMMAND


_CURSOR_HOOK_SCRIPT = r'''#!/usr/bin/env python3
"""grayson harness guard hook for Cursor (managed by `grayson harness guard`).

Hard-denies agent shell commands invoking the Snowflake CLI (`snow`/`snowsql`)
or reaching Snowflake through a connector import or its REST API, reads of
Snowflake credential stores and private keys (~/.snowflake/, ~/.snowsql/,
connections.toml, *.p8/*.pem/*.key — the connection details there are all an
agent needs to reach the warehouse without `snow` at all), and shell or file
access touching `.grayson/` state — except PROTOCOL.md and WORKFLOW_AUTHOR.md,
which grayson writes there to be read. Warehouse access must go through
grayson, where it is parsed, capped, and audited.

Commands are normalized before matching (quotes and backslashes stripped), so
`sn""ow` and `sn\ow` collapse to `snow` before the check. Fails CLOSED: the
hook entry registers failClosed, and an unreadable payload denies explicitly —
a crashed or slow guard must not wave the action through. Still friction +
visibility, not containment (docs/SECURITY.md).
"""

import json
import re
import sys

#: shell tokens that mean "reaching the warehouse directly", checked against
#: the normalized command
COMMAND_DENY = [
    (
        re.compile(r"(^|[\s;&|(`/])snow(sql)?\b", re.IGNORECASE),
        "direct `snow` use is blocked: all warehouse access goes through grayson",
    ),
    (
        re.compile(r"snowflake[._-]connector|import\s+snowflake|snowflake\.com/api", re.IGNORECASE),
        "reaching Snowflake around grayson (connector import / REST API) is "
        "blocked: all warehouse access goes through grayson",
    ),
]

#: credential stores, matched in commands and read paths alike
CREDENTIAL_RE = re.compile(r"\.snowflake\b|\.snowsql\b|connections\.toml", re.IGNORECASE)
CREDENTIAL_WHY = (
    "Snowflake credentials/config are off-limits: the connection details "
    "are all an agent needs to reach the warehouse without `snow`"
)

#: private-key suffixes, checked against read paths only (a command mentioning
#: `.key` is too ambiguous to hard-deny; the read of the file itself is not)
KEY_SUFFIXES = (".p8", ".pem", ".key")

STATE_RE = re.compile(r"\.grayson\b", re.IGNORECASE)
#: reference docs grayson writes into .grayson/ to be read
STATE_ALLOWED = (".grayson/protocol.md", ".grayson/workflow_author.md")
STATE_WHY = ".grayson/ state is read via grayson tools only"


def normalize(text):
    r"""Collapse the quoting tricks that defeat naive matching: `sn""ow`,
    `sn\ow` and `sn'ow'` are all `snow` to the shell, so they are to us too.
    Not complete — nothing is — but it removes the evasions that cost nothing."""
    stripped = re.sub(r"""["'\\]""", "", text)
    return re.sub(r"\s+", " ", stripped).strip()


def state_denied(text):
    lowered = text.replace("\\", "/").lower()
    if STATE_RE.search(lowered) is None:
        return False
    return not any(allowed in lowered for allowed in STATE_ALLOWED)


def check_command(command):
    normalized = normalize(command)
    for pattern, why in COMMAND_DENY:
        if pattern.search(normalized):
            return why
    if CREDENTIAL_RE.search(normalized):
        return CREDENTIAL_WHY
    if state_denied(normalized):
        return STATE_WHY
    return None


def check_path(path):
    normalized = normalize(path).replace("\\", "/")
    if CREDENTIAL_RE.search(normalized):
        return CREDENTIAL_WHY
    if normalized.lower().endswith(KEY_SUFFIXES):
        return (
            "private key files are off-limits: under key-pair auth the key is "
            "all an agent needs to reach the warehouse without `snow`"
        )
    if state_denied(normalized):
        return STATE_WHY
    return None


def respond(why):
    if why is None:
        payload = {"permission": "allow"}
    else:
        message = f"grayson guard: {why}"
        # both key spellings: Cursor's examples differ between camelCase and
        # snake_case across versions, and the wrong one is silently ignored
        payload = {
            "permission": "deny",
            "userMessage": message,
            "agentMessage": f"grayson guard denied this: {why}",
            "user_message": message,
            "agent_message": f"grayson guard denied this: {why}",
        }
    print(json.dumps(payload))


def main() -> None:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("event is not an object")
    except Exception:
        # the hook entry registers failClosed, so a crash would deny anyway;
        # being explicit keeps Cursor's hook log readable
        respond("could not read the hook payload (denied rather than waved through)")
        return
    why = None
    command = event.get("command")
    if command:
        why = check_command(str(command))
    for key in ("file_path", "path"):
        if why is None and event.get(key):
            why = check_path(str(event[key]))
    respond(why)


if __name__ == "__main__":
    main()
'''


def _cursor_hooks_path(root: Path) -> Path:
    return root / ".cursor" / "hooks.json"


def _cursor_script_path(root: Path) -> Path:
    return root / _CURSOR_SCRIPT_REL


def _cursor_guard_status(root: Path) -> dict:
    path = _cursor_hooks_path(root)
    try:
        hooks = _load(path).get("hooks", {})
    except ValueError as e:
        return {"harness": "cursor", "supported": True, "file": str(path), "error": str(e)}
    if not isinstance(hooks, dict):
        hooks = {}
    present = [
        ev
        for ev in _CURSOR_HOOK_EVENTS
        if isinstance(hooks.get(ev), list) and any(_cursor_entry_is_ours(e) for e in hooks[ev])
    ]
    missing = [ev for ev in _CURSOR_HOOK_EVENTS if ev not in present]
    script_present = _cursor_script_path(root).is_file()
    return {
        "harness": "cursor",
        "supported": True,
        "file": str(path),
        "script": str(_cursor_script_path(root)),
        "script_present": script_present,
        "applied": not missing and script_present,
        "present": present,
        "missing": missing,
    }


def _cursor_apply_guard(root: Path) -> dict:
    path = _cursor_hooks_path(root)
    data = _load(path)
    data.setdefault("version", 1)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: 'hooks' must be an object")
    added, upgraded = [], []
    for ev in _CURSOR_HOOK_EVENTS:
        entries = hooks.setdefault(ev, [])
        if not isinstance(entries, list):
            raise ValueError(f"{path}: 'hooks.{ev}' must be a list")
        ours = [i for i, e in enumerate(entries) if _cursor_entry_is_ours(e)]
        if not ours:
            entries.append(dict(_CURSOR_HOOK_ENTRY))
            added.append(ev)
        elif entries[ours[0]] != _CURSOR_HOOK_ENTRY:
            # an entry an older grayson wrote (no failClosed/timeout yet):
            # upgrade in place rather than stacking a second registration
            entries[ours[0]] = dict(_CURSOR_HOOK_ENTRY)
            upgraded.append(ev)
    script = _cursor_script_path(root)
    script_written = (
        not script.is_file() or script.read_text(encoding="utf-8") != _CURSOR_HOOK_SCRIPT
    )
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(_CURSOR_HOOK_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    _write_json(path, data)
    return {
        "harness": "cursor",
        "supported": True,
        "file": str(path),
        "added": added,
        "upgraded": upgraded,
        "script": str(script),
        "script_written": script_written,
        "rules": guard_rules_display("cursor"),
        "note": _CURSOR_NOTE,
    }


def _cursor_remove_guard(root: Path) -> dict:
    path = _cursor_hooks_path(root)
    removed: list[str] = []
    if path.is_file():
        data = _load(path)
        hooks = data.get("hooks", {})
        if isinstance(hooks, dict):
            for ev in _CURSOR_HOOK_EVENTS:
                entries = hooks.get(ev)
                # by command, so entries an older grayson wrote go too
                if isinstance(entries, list) and any(_cursor_entry_is_ours(e) for e in entries):
                    kept = [e for e in entries if not _cursor_entry_is_ours(e)]
                    removed.append(ev)
                    if kept:
                        hooks[ev] = kept
                    else:
                        del hooks[ev]
            if removed:
                _write_json(path, data)
    # delete the script only if it is still byte-for-byte ours — an edited
    # script is the user's now and stays (they own its removal)
    script = _cursor_script_path(root)
    script_removed = False
    if script.is_file() and script.read_text(encoding="utf-8") == _CURSOR_HOOK_SCRIPT:
        script.unlink()
        script_removed = True
    return {
        "harness": "cursor",
        "supported": True,
        "file": str(path),
        "removed": removed,
        "script_removed": script_removed,
    }
