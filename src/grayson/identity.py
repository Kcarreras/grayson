"""Per-user identity for traceability.

A short alphanumeric id, set once after install (`grayson user set <id>`),
stored per user in ~/.grayson/config.toml — never in the committed workspace
config. It stamps knowledge facts (`author`) and library commit messages
(`Grayson-User:` trailer) so shared-library history answers "who wrote this"
even when teammates push from shared machines or CI. GRAYSON_USER_ID in the
environment overrides the file (useful for agents running under a service
account).
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def user_config_path() -> Path:
    base = os.environ.get("GRAYSON_CONFIG_DIR", "").strip()
    root = Path(base) if base else Path.home() / ".grayson"
    return root / "config.toml"


def get_user_id() -> str | None:
    """The configured user id, or None when unset. Env beats file."""
    env = os.environ.get("GRAYSON_USER_ID", "").strip()
    if env:
        return env if _ID_RE.match(env) else None
    path = user_config_path()
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return None
    value = str(data.get("user", {}).get("id", "")).strip()
    return value if _ID_RE.match(value) else None


def set_user_id(value: str) -> str:
    """Validate and persist the user id. Returns the stored id."""
    value = value.strip()
    if not _ID_RE.match(value):
        raise ValueError(
            "user id must be 1-32 characters: letters, digits, '-' or '_' "
            "(starting with a letter or digit)"
        )
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Rewrite only the [user] section; any other sections in the file survive.
    lines: list[str] = []
    if path.is_file():
        kept, skipping = [], False
        for ln in path.read_text(encoding="utf-8").splitlines():
            stripped = ln.strip()
            if stripped.startswith("["):
                skipping = stripped == "[user]"
            if not skipping:
                kept.append(ln)
        lines = kept
    text = "\n".join(lines).rstrip()
    block = f'[user]\nid = "{value}"\n'
    path.write_text((text + "\n\n" if text else "") + block, encoding="utf-8")
    return value
