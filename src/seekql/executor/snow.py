"""Snowflake CLI executor.

Runs statements through the `snow` CLI using a named connection. seekql never
reads or stores credentials — auth is entirely Snowflake CLI's concern. Auth
failures are classified and surfaced as AUTH_REQUIRED so agents pause instead
of retrying into MFA fatigue.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

SNOW_CMD_ENV = "SEEKQL_SNOW_CMD"  # JSON list overriding the snow binary (tests, wrappers)

# Substrings that reliably indicate an auth/session problem. Kept specific so
# ordinary SQL errors are not misclassified as auth_required (which would halt
# the agent with a bogus re-auth prompt).
_AUTH_MARKERS = [
    "authentication token has expired",
    "authentication token is invalid",
    "not authenticated",
    "jwt token is invalid",
    "oauth access token expired",
    "id token is invalid",
    "sso authentication",
    "browser flow",
    "390104",
    "390114",
    "390195",
    "incorrect username or password",
    "could not connect to snowflake",
    "250001",
]
_AUTH_MARKER_RES = [re.compile(r"connection\b.*\bnot found")]
_TIMEOUT_MARKERS = ["statement reached its statement or warehouse timeout", "604 "]


@dataclass
class ExecutionResult:
    status: str  # ok | error | auth_required | timeout
    rows: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    duration_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Executor(Protocol):
    def execute(self, sql: str, timeout_seconds: int = 0) -> ExecutionResult: ...


def get_executor(connection: str, workspace_root: Path | None = None) -> Executor:
    if connection == "sandbox" and workspace_root is not None:
        # Local mock warehouse (no snow CLI): see seekql.sandbox.
        from seekql.sandbox.executor import SandboxExecutor, sandbox_db_path

        return SandboxExecutor(sandbox_db_path(workspace_root))
    return SnowExecutor(connection)


def _snow_command() -> list[str]:
    override = os.environ.get(SNOW_CMD_ENV)
    if override:
        cmd = json.loads(override)
        if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
            raise ValueError(f"{SNOW_CMD_ENV} must be a JSON list of strings")
        return cmd
    return ["snow"]


def classify_failure(stderr: str, stdout: str = "") -> str:
    text = f"{stderr}\n{stdout}".lower()
    if any(marker in text for marker in _AUTH_MARKERS):
        return "auth_required"
    if any(rx.search(text) for rx in _AUTH_MARKER_RES):
        return "auth_required"
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return "timeout"
    return "error"


def parse_snow_json(stdout: str, drop_status_rows: bool = False) -> list[dict]:
    """Parse `snow sql --format json` output.

    Handles both a flat row list (single statement) and a list of result sets
    (multi-statement, e.g. when a timeout ALTER SESSION was prepended). When
    drop_status_rows is set, bare {"status": ...} rows from prepended session
    statements are removed.
    """
    stdout = stdout.strip()
    if not stdout:
        return []
    data = json.loads(stdout)
    if isinstance(data, dict):
        return [data]
    if not isinstance(data, list):
        return []
    if data and all(isinstance(item, list) for item in data):
        rows = data[-1]  # last result set is the agent's statement
        return [r for r in rows if isinstance(r, dict)]
    rows = [r for r in data if isinstance(r, dict)]
    if drop_status_rows:
        rows = [r for r in rows if set(r) != {"status"}]
    return rows


class SnowExecutor:
    def __init__(self, connection: str):
        self.connection = connection

    def execute(self, sql: str, timeout_seconds: int = 0) -> ExecutionResult:
        prepended = False
        statement = sql
        if timeout_seconds > 0:
            statement = (
                f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {int(timeout_seconds)};\n" + sql
            )
            prepended = True
        cmd = [
            *_snow_command(),
            "sql",
            "-q",
            statement,
            "--format",
            "json",
            "--connection",
            self.connection,
        ]
        start = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 — argument vector, no shell
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=(timeout_seconds + 120) if timeout_seconds else 900,
            )
        except FileNotFoundError:
            return ExecutionResult(
                status="error",
                error="`snow` CLI not found on PATH — install Snowflake CLI "
                "(https://docs.snowflake.com/en/developer-guide/snowflake-cli)",
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(status="timeout", error="local subprocess timeout")
        duration_ms = int((time.monotonic() - start) * 1000)

        if proc.returncode != 0:
            status = classify_failure(proc.stderr or "", proc.stdout or "")
            detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
            return ExecutionResult(status=status, duration_ms=duration_ms, error=detail)

        try:
            rows = parse_snow_json(proc.stdout, drop_status_rows=prepended)
        except json.JSONDecodeError as e:
            return ExecutionResult(
                status="error",
                duration_ms=duration_ms,
                error=f"could not parse snow output as JSON: {e}",
            )
        columns = list(rows[0].keys()) if rows else []
        return ExecutionResult(status="ok", rows=rows, columns=columns, duration_ms=duration_ms)


_IDENT_RE = re.compile(r"[A-Z_][A-Z0-9_$]*\Z")  # \Z: true end, not before trailing \n


def metadata_query(tables: list[str]) -> str | None:
    """Build one INFORMATION_SCHEMA query covering fully-qualified target tables.

    Parts are strict-validated as plain identifiers before interpolation.
    """
    by_catalog: dict[str, list[tuple[str, str]]] = {}
    for fq in tables:
        parts = fq.upper().split(".")
        if len(parts) != 3 or not all(_IDENT_RE.match(p) for p in parts):
            continue
        by_catalog.setdefault(parts[0], []).append((parts[1], parts[2]))
    selects = []
    for catalog, pairs in sorted(by_catalog.items()):
        preds = " OR ".join(
            f"(TABLE_SCHEMA = '{schema}' AND TABLE_NAME = '{name}')" for schema, name in pairs
        )
        selects.append(
            "SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, ROW_COUNT, "
            f"TO_VARCHAR(LAST_ALTERED) AS LAST_ALTERED FROM {catalog}.INFORMATION_SCHEMA.TABLES "
            f"WHERE {preds}"
        )
    if not selects:
        return None
    return "\nUNION ALL\n".join(selects)
