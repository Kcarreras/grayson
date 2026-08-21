"""Result cache: SQLite-backed artifacts with JSON sidecars carrying freshness metadata.

Pure-stdlib storage (no native DLLs beyond Python itself) so it runs under
locked-down Windows Application Control policies common on work machines.
Each executed query's rows land as table q_XXXX in the session's results.db;
the sidecar records when it ran and what the sources looked like then.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from seekql.util import read_json, sql_hash, utcnow, write_json

QID_RE = re.compile(r"^q_[0-9]{4,}$")


class CacheStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "results.db"

    def sidecar_path(self, qid: str) -> Path:
        return self.data_dir / f"{qid}.json"

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def save(
        self,
        qid: str,
        rows: list[dict],
        sql: str,
        source_tables: list[str],
        truncated: bool,
        source_last_altered: dict[str, str] | None = None,
        worker: str | None = None,
    ) -> dict:
        if not QID_RE.match(qid):
            raise ValueError(f"invalid artifact id: {qid!r}")
        columns = list(rows[0].keys()) if rows else []
        if rows:
            self._write_table(qid, columns, rows)
        sidecar = {
            "qid": qid,
            "executed_at": utcnow(),
            "query_hash": sql_hash(sql),
            "sql": sql,
            "source_tables": sorted({t.upper() for t in source_tables}),
            "row_count": len(rows),
            "truncated": truncated,
            "columns": columns,
            "source_last_altered": source_last_altered or {},
            "worker": worker,
            "artifact": qid if rows else None,
        }
        write_json(self.sidecar_path(qid), sidecar)
        return sidecar

    def _write_table(self, qid: str, columns: list[str], rows: list[dict]) -> None:
        quoted = [_quote_ident(c) for c in columns]
        con = self._con()
        try:
            con.execute(f"DROP TABLE IF EXISTS {_quote_ident(qid)}")
            con.execute(f"CREATE TABLE {_quote_ident(qid)} ({', '.join(quoted)})")
            placeholders = ", ".join("?" for _ in columns)
            con.executemany(
                f"INSERT INTO {_quote_ident(qid)} VALUES ({placeholders})",  # noqa: S608
                [tuple(_coerce(row.get(c)) for c in columns) for row in rows],
            )
            con.commit()
        finally:
            con.close()

    def list_sidecars(self) -> list[dict]:
        out = []
        for path in sorted(self.data_dir.glob("q_*.json")):
            try:
                out.append(read_json(path))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def get(self, qid: str) -> dict | None:
        if not QID_RE.match(qid):
            return None
        path = self.sidecar_path(qid)
        return read_json(path) if path.is_file() else None

    def find(self, tables: list[str] | None = None, query_hash: str | None = None) -> list[dict]:
        """Match cached artifacts by source-table overlap and/or exact query hash."""
        wanted = {t.upper() for t in tables} if tables else None
        results = []
        for sc in self.list_sidecars():
            if query_hash and sc.get("query_hash") != query_hash:
                continue
            if wanted and not wanted.issubset(set(sc.get("source_tables", []))):
                continue
            results.append(sc)
        return results

    def artifact_tables(self) -> set[str]:
        if not self.db_path.is_file():
            return set()
        con = self._con()
        try:
            rows = con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            return {r[0] for r in rows if QID_RE.match(r[0])}
        finally:
            con.close()

    def preview(self, qid: str, limit: int = 10) -> list[dict]:
        if not QID_RE.match(qid) or qid not in self.artifact_tables():
            return []
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                f"SELECT * FROM {_quote_ident(qid)} LIMIT ?",
                (int(limit),),  # noqa: S608
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def drop_all_data(self) -> int:
        """Delete all cached rows (sidecars and audit trail are kept)."""
        tables = self.artifact_tables()
        if not tables:
            return 0
        con = self._con()
        try:
            for t in tables:
                con.execute(f"DROP TABLE {_quote_ident(t)}")
            con.commit()
            con.execute("VACUUM")
        finally:
            con.close()
        return len(tables)


def compare_artifacts(store: CacheStore, before_qid: str, after_qid: str) -> dict:
    """Deterministic before/after comparison of two cached result sets.

    Used for verification: e.g. an anomaly-count query that should drop to zero,
    or a parity check whose mismatch set should shrink. Reports row-count delta
    and, for small identically-shaped sets, whether values are identical.
    """
    before = store.get(before_qid)
    after = store.get(after_qid)
    missing = [q for q, sc in [(before_qid, before), (after_qid, after)] if sc is None]
    if missing:
        raise KeyError(f"unknown artifact(s): {missing}")
    b_rows = store.preview(before_qid, limit=1000)
    a_rows = store.preview(after_qid, limit=1000)
    b_count, a_count = before["row_count"], after["row_count"]
    same_columns = before["columns"] == after["columns"]
    identical = same_columns and b_count == a_count and b_count <= 1000 and b_rows == a_rows
    return {
        "before": {"qid": before_qid, "row_count": b_count, "columns": before["columns"]},
        "after": {"qid": after_qid, "row_count": a_count, "columns": after["columns"]},
        "row_count_delta": a_count - b_count,
        "same_columns": same_columns,
        "identical": identical,
        "before_empty": b_count == 0,
        "after_empty": a_count == 0,
    }


def staleness(sidecar: dict, current_last_altered: dict[str, str]) -> str:
    """fresh | stale | unknown — compare captured vs current LAST_ALTERED."""
    captured: dict[str, str] = sidecar.get("source_last_altered") or {}
    if not captured:
        return "unknown"
    verdicts = []
    for table, then in captured.items():
        now = current_last_altered.get(table.upper())
        if now is None:
            verdicts.append("unknown")
        elif str(now) != str(then):
            return "stale"
        else:
            verdicts.append("fresh")
    return "fresh" if verdicts and all(v == "fresh" for v in verdicts) else "unknown"


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _coerce(value: object) -> object:
    if value is None or isinstance(value, int | float | str | bytes):
        return value
    if isinstance(value, bool):
        return int(value)
    return json.dumps(value, default=str)
