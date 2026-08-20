"""Guarded local analysis over cached artifacts.

Agents can re-slice already-fetched data without warehouse round-trips.
The same default-deny posture applies: single SELECT statements only, and
only cached artifact names (q_XXXX) are queryable. The connection is opened
read-only (SQLite URI mode=ro), so even a statement that slipped past the
parser could not write, and extension loading stays disabled.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlglot
from sqlglot import exp

from seekql.cache.store import QID_RE, CacheStore
from seekql.guard.rules import _SET_OPERATION, FORBIDDEN_TYPES


class LocalQueryError(ValueError):
    pass


def query_artifacts(
    data_dir: Path, sql: str, max_rows: int = 10000
) -> tuple[list[str], list[tuple]]:
    """Run a read-only local query where table names are cached qids."""
    store = CacheStore(data_dir)
    available = store.artifact_tables()

    sql = sql.strip().rstrip(";").strip()
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError as e:
        raise LocalQueryError(f"could not parse: {e}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise LocalQueryError("exactly one statement per call")
    tree = statements[0]
    if not isinstance(tree, exp.Select | _SET_OPERATION):
        raise LocalQueryError("only SELECT statements are allowed on cached data")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_TYPES):
            raise LocalQueryError(f"'{type(node).__name__}' is not allowed on cached data")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE) if c.alias_or_name}
    for t in tree.find_all(exp.Table):
        name = (t.name or "").lower()
        if not name and t.this is not None:
            raise LocalQueryError("table functions are not allowed on cached data")
        if name in cte_names:
            continue
        if t.db or t.catalog or not QID_RE.match(name) or name not in available:
            raise LocalQueryError(
                f"unknown table '{t.name}' — cached-data queries may only reference "
                f"artifact ids ({', '.join(sorted(available)) or 'none cached yet'})"
            )

    if not store.db_path.is_file():
        raise LocalQueryError("no cached data yet")
    uri = f"file:{store.db_path.as_posix()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.OperationalError as e:
        raise LocalQueryError(str(e)) from e
    try:
        con.execute("PRAGMA busy_timeout=30000")
        cursor = con.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(max_rows)
        return columns, rows
    except sqlite3.Error as e:
        raise LocalQueryError(str(e)) from e
    finally:
        con.close()
