"""Sandbox executor: emulates the Snowflake path over a local SQLite warehouse.

Selected when the workspace connection is named "sandbox" (see
executor.snow.get_executor). Guarded statements arrive in Snowflake dialect;
tables are stored under their quoted fully-qualified names (e.g.
"SANDBOX.SHOP.ORDERS"), so FQN references are folded to those identifiers and
the statement is transpiled to SQLite via sqlglot. Metadata queries
(INFORMATION_SCHEMA.TABLES), SHOW TABLES, DESCRIBE, and GET_DDL are answered
from the seeded _grayson_meta catalog and SQLite's own table info, so freshness
tracking and schema discovery work exactly as they would against a real
warehouse.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path

import sqlglot
from sqlglot import exp

from grayson.executor.snow import ExecutionResult

SANDBOX_CONNECTION = "sandbox"
SANDBOX_DIR_ENV = "GRAYSON_SANDBOX_DIR"  # override the warehouse store location (tests)

_DESCRIBE_RE = re.compile(
    r"^\s*DESC(?:RIBE)?\s+(?:TABLE\s+|VIEW\s+)?([\w.\"$]+)\s*$", re.IGNORECASE
)
# SELECT GET_DDL('<kind>', '<name>'[, TRUE]) [AS alias] [LIMIT n] — the guard may
# have appended a LIMIT; anything more elaborate is not emulated.
_GET_DDL_RE = re.compile(
    r"^\s*SELECT\s+GET_DDL\s*\(\s*'(\w+)'\s*,\s*'([\w.\"$]+)'\s*(?:,\s*\w+\s*)?\)"
    r"\s*(?:AS\s+\w+\s*)?(?:LIMIT\s+\d+\s*)?$",
    re.IGNORECASE,
)


def sandbox_db_path(workspace_root: Path) -> Path:
    """Warehouse location for a sandbox workspace — deliberately OUTSIDE it.

    The agent works inside the workspace; a warehouse file it could open
    directly would let it bypass the guard entirely (and read the planted
    problems unaudited). Keyed by workspace path so multiple sandboxes coexist.
    """
    base = os.environ.get(SANDBOX_DIR_ENV)
    store = Path(base) if base else Path.home() / ".grayson" / "sandboxes"
    digest = hashlib.sha256(str(workspace_root.resolve()).lower().encode()).hexdigest()[:12]
    return store / f"{digest}.db"


def locate_warehouse(workspace_root: Path) -> Path:
    """The workspace's warehouse path, migrating a legacy in-workspace file.

    Early versions seeded `.grayson/sandbox_warehouse.db` inside the workspace;
    those files are moved to the store on first touch so upgraded installs keep
    working without a reseed.
    """
    target = sandbox_db_path(workspace_root)
    legacy = workspace_root / ".grayson" / "sandbox_warehouse.db"
    if not target.is_file() and legacy.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(target)
    return target


class SandboxSQLError(ValueError):
    pass


class SandboxExecutor:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def execute(self, sql: str, timeout_seconds: int = 0) -> ExecutionResult:
        if not self.db_path.is_file():
            return ExecutionResult(
                status="error",
                error="sandbox warehouse not found. This is a setup problem, not an "
                "analysis problem: pause and ask the user to run `grayson sandbox reset` — "
                "do not run setup commands yourself.",
            )
        start = time.monotonic()
        try:
            rows = self._dispatch(sql)
        except SandboxSQLError as e:
            return ExecutionResult(
                status="error",
                duration_ms=int((time.monotonic() - start) * 1000),
                error=str(e),
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        columns = list(rows[0].keys()) if rows else []
        return ExecutionResult(status="ok", rows=rows, columns=columns, duration_ms=duration_ms)

    # -- dispatch ---------------------------------------------------------

    def _dispatch(self, sql: str) -> list[dict]:
        stripped = sql.strip().rstrip(";").strip()
        upper = stripped.upper()
        if "INFORMATION_SCHEMA.TABLES" in upper:
            return self._metadata_rows()
        if upper.startswith("SHOW"):
            if re.match(r"^SHOW\s+(TERSE\s+)?TABLES\b", upper):
                return self._show_tables()
            raise SandboxSQLError("only SHOW TABLES is supported in the sandbox")
        m = _DESCRIBE_RE.match(stripped)
        if m:
            return self._describe(m.group(1))
        if "GET_DDL" in upper:
            m = _GET_DDL_RE.match(stripped)
            if not m:
                raise SandboxSQLError(
                    "only SELECT GET_DDL('TABLE'|'VIEW'|'SCHEMA'|'DATABASE', '<name>') is "
                    "supported in the sandbox"
                )
            return self._get_ddl(m.group(1).upper(), m.group(2))
        return self._run_select(stripped)

    def _run_select(self, sql: str) -> list[dict]:
        try:
            tree = sqlglot.parse_one(sql, read="snowflake")
        except sqlglot.errors.ParseError as e:
            raise SandboxSQLError(f"could not parse statement: {e}") from e
        for t in tree.find_all(exp.Table):
            if t.db or t.catalog:
                fqn = ".".join(str(p).upper() for p in (t.catalog, t.db, t.name) if p)
                t.set("this", exp.to_identifier(fqn, quoted=True))
                t.set("db", None)
                t.set("catalog", None)
        try:
            sqlite_sql = tree.sql(dialect="sqlite")
        except sqlglot.errors.SqlglotError as e:
            raise SandboxSQLError(f"could not translate statement for the sandbox: {e}") from e
        return self._query(sqlite_sql)

    def _describe(self, target: str) -> list[dict]:
        fqn = target.replace('"', "").upper()
        rows = self._query("SELECT name, type FROM pragma_table_info(?) ORDER BY cid", (fqn,))
        if not rows:
            raise SandboxSQLError(f"table '{fqn}' does not exist in the sandbox")
        return [
            {"name": r["name"], "type": r["type"] or "TEXT", "kind": "COLUMN", "null?": "Y"}
            for r in rows
        ]

    def _get_ddl(self, kind: str, target: str) -> list[dict]:
        """One row, one column, the way Snowflake returns it: the column is named
        after the call and holds CREATE statements for the object(s) named."""
        name = target.replace('"', "").upper()
        if kind in {"TABLE", "VIEW"}:
            fqns = [name]
        elif kind in {"SCHEMA", "DATABASE"}:
            prefix = name + "."
            fqns = [
                str(r["fqn"])
                for r in self._query("SELECT fqn FROM _grayson_meta ORDER BY fqn")
                if str(r["fqn"]).startswith(prefix)
            ]
            if not fqns:
                raise SandboxSQLError(f"{kind.lower()} '{name}' does not exist in the sandbox")
        else:
            raise SandboxSQLError(f"GET_DDL('{kind}', ...) is not supported in the sandbox")
        statements = []
        for fqn in fqns:
            cols = self._describe(fqn)
            body = ",\n".join(f"\t{c['name']} {c['type']}" for c in cols)
            statements.append(f"create or replace TABLE {fqn} (\n{body}\n);")
        return [{f"GET_DDL('{kind}', '{name}')": "\n".join(statements)}]

    def _show_tables(self) -> list[dict]:
        out = []
        for r in self._metadata_rows():
            out.append(
                {
                    "name": r["TABLE_NAME"],
                    "database_name": r["TABLE_CATALOG"],
                    "schema_name": r["TABLE_SCHEMA"],
                    "rows": r["ROW_COUNT"],
                    "kind": "TABLE",
                }
            )
        return out

    def _metadata_rows(self) -> list[dict]:
        rows = self._query("SELECT fqn, row_count, last_altered FROM _grayson_meta ORDER BY fqn")
        out = []
        for r in rows:
            catalog, schema, name = str(r["fqn"]).split(".", 2)
            out.append(
                {
                    "TABLE_CATALOG": catalog,
                    "TABLE_SCHEMA": schema,
                    "TABLE_NAME": name,
                    "ROW_COUNT": r["row_count"],
                    "LAST_ALTERED": r["last_altered"],
                }
            )
        return out

    # -- low level --------------------------------------------------------

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        uri = f"file:{self.db_path.as_posix()}?mode=ro"
        try:
            con = sqlite3.connect(uri, uri=True, timeout=30)
        except sqlite3.OperationalError as e:
            raise SandboxSQLError(str(e)) from e
        try:
            con.row_factory = sqlite3.Row
            cur = con.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            raise SandboxSQLError(str(e)) from e
        finally:
            con.close()
