"""Deliberate, validated edits to grayson.toml — the user-facing settings surface.

grayson.toml holds the rails (guard profiles, scopes, connection, library
pointer), so edits go through here with validation, and only via human
surfaces: the `grayson config` CLI and the console's Settings page. The MCP
server exposes configuration read-only on purpose — an agent that can loosen
its own guards has no guards.

Edits are surgical: only the section being changed is rewritten (with its
canonical comments regenerated); every other line of the file is preserved.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from grayson.config import BUILTIN_PROFILES, CONFIG_FILENAME, GuardSettings, WorkspaceConfig


class ConfigError(ValueError):
    """Invalid settings change; message says what to fix."""


#: user-settable keys → (section, key, coercion)
SETTABLE: dict[str, tuple[str, str, str]] = {
    "connection.name": ("connection", "name", "str"),
    "defaults.guard_profile": ("defaults", "guard_profile", "str"),
    "scopes.strict": ("scopes", "strict", "bool"),
    "scopes.allowed": ("scopes", "allowed", "list"),
    "library.auto_push": ("library", "auto_push", "bool"),
    "library.path": ("library", "path", "path"),
}

_SECTION_COMMENTS: dict[str, list[str]] = {
    "connection": ["# snow CLI named connection ('sandbox' routes to the local mock warehouse)"],
    "defaults": [],
    "scopes": [
        '# db.schema globs agents may read without warnings, e.g. ["ANALYTICS.*"]',
        "# strict = true blocks out-of-scope reads instead of warning",
    ],
    "library": ["# team library repo clone (docs/SPEC.md s11a); auto_push syncs every write"],
}


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _replace_section(text: str, header: str, body_lines: list[str]) -> str:
    """Replace (or append) one exact `[header]` section, leaving the rest alone."""
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == f"[{header}]":
            replaced = True
            out.extend(body_lines)
            i += 1
            # skip the old section body (up to the next section header)
            while i < len(lines) and not lines[i].strip().startswith("["):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.extend(body_lines)
    return "\n".join(out).rstrip() + "\n"


def _section_block(header: str, values: dict[str, Any]) -> list[str]:
    block = [*_SECTION_COMMENTS.get(header, []), f"[{header}]"]
    for key, value in values.items():
        if header == "library" and key == "path":
            # forward slashes: backslashes are escape characters in TOML strings
            value = Path(str(value)).as_posix()
        block.append(f"{key} = {_toml_value(value)}")
    return block


def _raw(root: Path) -> tuple[Path, dict]:
    cfg_path = root / CONFIG_FILENAME
    if not cfg_path.is_file():
        raise ConfigError(f"no {CONFIG_FILENAME} in {root}")
    try:
        return cfg_path, tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{CONFIG_FILENAME} is not valid TOML: {e}") from e


def _coerce(key: str, kind: str, value: Any) -> Any:
    if kind == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "on", "yes", "1"}:
            return True
        if text in {"false", "off", "no", "0"}:
            return False
        raise ConfigError(f"{key} must be true or false, got {value!r}")
    if kind == "list":
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [part.strip() for part in str(value).split(",") if part.strip()]
    if kind == "path":
        path = Path(str(value)).expanduser()
        if not path.is_dir():
            raise ConfigError(f"{key}: directory does not exist: {path}")
        return path
    return str(value).strip()


def _validate(root: Path, dotted: str, coerced: Any) -> None:
    if dotted == "defaults.guard_profile":
        cfg = WorkspaceConfig.load(root / CONFIG_FILENAME)
        if coerced not in cfg.guard_profiles:
            known = ", ".join(sorted(cfg.guard_profiles))
            raise ConfigError(f"unknown guard profile '{coerced}' (known: {known})")
    if dotted == "connection.name" and not coerced:
        raise ConfigError("connection.name cannot be empty")


def set_values(root: Path, changes: dict[str, Any]) -> dict:
    """Apply {dotted_key: value} changes to grayson.toml. Returns what changed."""
    unknown = sorted(set(changes) - set(SETTABLE))
    if unknown:
        raise ConfigError(f"unknown setting(s): {unknown} (settable: {sorted(SETTABLE)})")
    cfg_path, data = _raw(root)
    applied: dict[str, Any] = {}
    by_section: dict[str, dict[str, Any]] = {}
    for dotted, value in changes.items():
        section, key, kind = SETTABLE[dotted]
        coerced = _coerce(dotted, kind, value)
        _validate(root, dotted, coerced)
        by_section.setdefault(section, dict(data.get(section, {})))[key] = coerced
        applied[dotted] = coerced.as_posix() if isinstance(coerced, Path) else coerced
    text = cfg_path.read_text(encoding="utf-8")
    for section, values in by_section.items():
        text = _replace_section(text, section, _section_block(section, values))
    cfg_path.write_text(text, encoding="utf-8")
    return {"changed": applied}


def set_workflow_defaults(
    root: Path,
    workflow: str,
    guard_profile: str | None = None,
    strict_scope: bool | None = None,
) -> dict:
    """Set (or clear) one workflow's per-workspace session defaults.

    Each call states the workflow's full defaults: a None field inherits the
    normal resolution and is not written; both None removes the section. The
    console settings form maps onto this directly — every save is the whole
    row as shown."""
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", workflow):
        raise ConfigError(
            f"workflow name {workflow!r} must be 1-64 lowercase letters, digits or '-'"
        )
    if guard_profile is not None:
        cfg = WorkspaceConfig.load(root / CONFIG_FILENAME)
        if guard_profile not in cfg.guard_profiles:
            known = ", ".join(sorted(cfg.guard_profiles))
            raise ConfigError(f"unknown guard profile '{guard_profile}' (known: {known})")
    cfg_path, _data = _raw(root)
    values: dict[str, Any] = {}
    if guard_profile is not None:
        values["guard_profile"] = guard_profile
    if strict_scope is not None:
        values["strict_scope"] = strict_scope
    header = f"workflow_defaults.{workflow}"
    text = cfg_path.read_text(encoding="utf-8")
    # an empty block deletes the section: no defaults left means no section
    text = _replace_section(text, header, _section_block(header, values) if values else [])
    cfg_path.write_text(text, encoding="utf-8")
    return {"workflow": workflow, "defaults": values}


def set_guard_profile(root: Path, name: str, updates: dict[str, Any]) -> dict:
    """Create or edit one named guard profile (partial updates allowed)."""
    if not name.replace("_", "").replace("-", "").isalnum():
        raise ConfigError(f"profile name {name!r} must be alphanumeric (plus - or _)")
    cfg_path, data = _raw(root)
    current = dict(BUILTIN_PROFILES.get(name, {}))
    current.update(data.get("guard_profiles", {}).get(name, {}))
    merged = {**current, **{k: v for k, v in updates.items() if v is not None}}
    try:
        settings = GuardSettings(**merged)
    except (ValidationError, TypeError) as e:
        raise ConfigError(f"invalid guard settings for '{name}': {e}") from e
    text = cfg_path.read_text(encoding="utf-8")
    text = _replace_section(
        text,
        f"guard_profiles.{name}",
        _section_block(f"guard_profiles.{name}", settings.model_dump()),
    )
    cfg_path.write_text(text, encoding="utf-8")
    return {"profile": name, "settings": settings.model_dump()}


def config_summary(root: Path) -> dict:
    """The current configuration, resolved — the read surface for CLI/MCP/UI."""
    cfg = WorkspaceConfig.load(root / CONFIG_FILENAME)
    return {
        "workspace": str(root),
        "connection": cfg.connection,
        "default_guard_profile": cfg.default_guard_profile,
        "guard_profiles": {
            name: gs.model_dump() for name, gs in sorted(cfg.guard_profiles.items())
        },
        "scopes": cfg.scopes.model_dump(),
        "workflow_defaults": {
            name: wd.model_dump(exclude_none=True)
            for name, wd in sorted(cfg.workflow_defaults.items())
        },
        "library": {
            "path": str(cfg.library_path) if cfg.library_path else None,
            "auto_push": cfg.library_auto_push,
        },
        "settable_keys": sorted(SETTABLE),
    }
