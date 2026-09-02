"""Shared helpers: time, ids, hashing, small file IO."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_session_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().upper()


def sql_hash(sql: str) -> str:
    return hashlib.sha256(normalize_sql(sql).encode()).hexdigest()[:16]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a same-directory temp file + rename, so an interrupted write
    (container kill, full disk) leaves the old file intact, never a truncated
    one. Library formats are read by humans and other grayson versions —
    a half-written doc is worse than a stale one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, default=str))


_OBJECT_NAME_RE = re.compile(r'^(?:"[^"]+"|[A-Za-z_][\w$]*)(?:\.(?:"[^"]+"|[A-Za-z_][\w$]*)){0,2}$')


def is_object_name(value: str) -> bool:
    """Whether `value` is one warehouse object name: up to three dot-separated
    identifiers, each bare or double-quoted. Free text ("the webinar table")
    is not, and must never be registered as a name nothing will match."""
    return bool(_OBJECT_NAME_RE.match(str(value).strip()))


def parse_table_list(value: str) -> list[str]:
    """Table names from free text a human typed: comma/whitespace separated,
    uppercased, deduplicated, order kept."""
    return list(dict.fromkeys(t.strip().upper() for t in re.split(r"[,\s]+", value) if t.strip()))


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve candidate and require it to live under root (no traversal escapes)."""
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"path {candidate} escapes workspace root {root}")
    return resolved
