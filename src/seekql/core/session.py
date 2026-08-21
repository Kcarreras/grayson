"""Session state: SQLite-backed (WAL) so parallel workers never corrupt state."""

from __future__ import annotations

import json
import secrets
import shutil
import sqlite3
from pathlib import Path

from seekql.cache.store import CacheStore
from seekql.config import GuardSettings
from seekql.util import new_session_id, utcnow
from seekql.workspace import Workspace

STAGES = ["setup", "analysis", "synthesis", "review", "fixes", "verification", "closed"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, actor TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workers(
    id TEXT PRIMARY KEY, label TEXT, joined_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queries(
    qid TEXT PRIMARY KEY,
    worker TEXT, ts TEXT NOT NULL, status TEXT NOT NULL,
    guard_rule TEXT, reason TEXT, warnings TEXT,
    sql_raw TEXT NOT NULL, sql_executed TEXT,
    duration_ms INTEGER, row_count INTEGER, truncated INTEGER,
    tables_json TEXT, error TEXT, label TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints(
    key TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    evidence TEXT, note TEXT, completed_by TEXT, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS findings(
    fid TEXT PRIMARY KEY, ts TEXT NOT NULL, worker TEXT,
    schema_name TEXT NOT NULL, severity TEXT, confidence TEXT,
    title TEXT NOT NULL, accepted INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL
);
"""


class Session:
    def __init__(self, workspace: Workspace, session_id: str):
        self.workspace = workspace
        self.id = session_id
        self.dir = workspace.session_dir(session_id)
        if not self.dir.is_dir():
            raise FileNotFoundError(f"session '{session_id}' not found")
        self.cache = CacheStore(self.dir / "data")

    # -- lifecycle -------------------------------------------------------

    @classmethod
    def create(
        cls,
        workspace: Workspace,
        *,
        workflow: str,
        targets: list[str],
        guard: GuardSettings,
        guard_profile: str,
        title: str = "",
        workers: int = 1,
        strict_scope: bool | None = None,
        connection: str | None = None,
    ) -> Session:
        session_id = new_session_id()
        sdir = workspace.session_dir(session_id)
        sdir.mkdir(parents=True)
        for sub in ("data", "queries", "interventions", "findings", "proposals"):
            (sdir / sub).mkdir()
        con = _connect(sdir / "state.db")
        try:
            con.executescript(_SCHEMA)
            meta = {
                "id": session_id,
                "title": title,
                "workflow": workflow,
                "stage": "setup",
                "created_at": utcnow(),
                "targets": json.dumps([t.upper() for t in targets]),
                "guard": guard.model_dump_json(),
                "guard_profile": guard_profile,
                "workers_planned": str(workers),
                "strict_scope": json.dumps(
                    workspace.config.scopes.strict if strict_scope is None else strict_scope
                ),
                "connection": connection or workspace.config.connection,
                "scope_extra": json.dumps([]),
            }
            con.executemany("INSERT INTO meta(key, value) VALUES(?, ?)", meta.items())
            con.commit()
        finally:
            con.close()
        session = cls(workspace, session_id)
        session.log_event("user", "session_created", {"workflow": workflow, "targets": targets})
        return session

    def close_db(self) -> None:  # placeholder for symmetry; connections are per-call
        pass

    # -- db helpers ------------------------------------------------------

    def _con(self) -> sqlite3.Connection:
        return _connect(self.dir / "state.db")

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        con = self._con()
        try:
            row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else default
        finally:
            con.close()

    def set_meta(self, key: str, value: str) -> None:
        con = self._con()
        try:
            con.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            con.commit()
        finally:
            con.close()

    # -- properties ------------------------------------------------------

    @property
    def stage(self) -> str:
        return self.get_meta("stage", "setup") or "setup"

    @property
    def workflow(self) -> str:
        return self.get_meta("workflow", "") or ""

    @property
    def targets(self) -> list[str]:
        return json.loads(self.get_meta("targets", "[]") or "[]")

    @property
    def guard_settings(self) -> GuardSettings:
        raw = self.get_meta("guard")
        return GuardSettings.model_validate_json(raw) if raw else GuardSettings()

    @property
    def strict_scope(self) -> bool:
        return bool(json.loads(self.get_meta("strict_scope", "false") or "false"))

    @property
    def connection(self) -> str:
        return self.get_meta("connection", "default") or "default"

    @property
    def scope_tables(self) -> set[str]:
        extra = set(json.loads(self.get_meta("scope_extra", "[]") or "[]"))
        return {t.upper() for t in self.targets} | {t.upper() for t in extra}

    def add_scope(self, tables: list[str]) -> None:
        extra = set(json.loads(self.get_meta("scope_extra", "[]") or "[]"))
        extra.update(t.upper() for t in tables)
        self.set_meta("scope_extra", json.dumps(sorted(extra)))

    def set_stage(self, stage: str, actor: str = "user") -> None:
        if stage not in STAGES:
            raise ValueError(f"unknown stage '{stage}' (stages: {', '.join(STAGES)})")
        self.set_meta("stage", stage)
        self.log_event(actor, "stage_changed", {"stage": stage})

    # -- events ----------------------------------------------------------

    def log_event(self, actor: str, event_type: str, payload: dict) -> None:
        con = self._con()
        try:
            con.execute(
                "INSERT INTO events(ts, actor, type, payload) VALUES(?, ?, ?, ?)",
                (utcnow(), actor, event_type, json.dumps(payload, default=str)),
            )
            con.commit()
        finally:
            con.close()

    def events(self, limit: int = 100) -> list[dict]:
        con = self._con()
        try:
            rows = con.execute(
                "SELECT ts, actor, type, payload FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"ts": r[0], "actor": r[1], "type": r[2], "payload": json.loads(r[3])} for r in rows
            ]
        finally:
            con.close()

    # -- workers ---------------------------------------------------------

    def worker_join(self, label: str = "") -> str:
        worker_id = f"w-{secrets.token_hex(3)}"
        con = self._con()
        try:
            con.execute(
                "INSERT INTO workers(id, label, joined_at) VALUES(?, ?, ?)",
                (worker_id, label, utcnow()),
            )
            con.commit()
        finally:
            con.close()
        self.log_event(worker_id, "worker_joined", {"label": label})
        return worker_id

    def workers(self) -> list[dict]:
        con = self._con()
        try:
            rows = con.execute("SELECT id, label, joined_at FROM workers").fetchall()
            return [{"id": r[0], "label": r[1], "joined_at": r[2]} for r in rows]
        finally:
            con.close()

    # -- queries / audit -------------------------------------------------

    def allocate_qid(self, worker: str | None, sql_raw: str, label: str = "") -> str:
        """Atomically allocate the next query id (safe under parallel workers)."""
        con = self._con()
        try:
            for _ in range(50):
                con.execute("BEGIN IMMEDIATE")
                n = con.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
                qid = f"q_{n + 1:04d}"
                try:
                    con.execute(
                        "INSERT INTO queries(qid, worker, ts, status, sql_raw, label) "
                        "VALUES(?, ?, ?, 'pending', ?, ?)",
                        (qid, worker, utcnow(), sql_raw, label),
                    )
                    con.commit()
                    return qid
                except sqlite3.IntegrityError:
                    con.rollback()
            raise RuntimeError("could not allocate query id after 50 attempts")
        finally:
            con.close()

    def update_query(self, qid: str, **fields: object) -> None:
        allowed = {
            "status", "guard_rule", "reason", "warnings", "sql_executed",
            "duration_ms", "row_count", "truncated", "tables_json", "error",
        }  # fmt: skip
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown query fields: {bad}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        con = self._con()
        try:
            con.execute(
                f"UPDATE queries SET {sets} WHERE qid = ?",  # noqa: S608 — keys allowlisted
                (*fields.values(), qid),
            )
            con.commit()
        finally:
            con.close()

    def query_row(self, qid: str) -> dict | None:
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM queries WHERE qid = ?", (qid,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    def query_log(self, limit: int = 200) -> list[dict]:
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT qid, worker, ts, status, guard_rule, reason, duration_ms, "
                "row_count, truncated, label, tables_json FROM queries "
                "ORDER BY qid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def executed_count(self) -> int:
        con = self._con()
        try:
            return con.execute("SELECT COUNT(*) FROM queries WHERE status = 'executed'").fetchone()[
                0
            ]
        finally:
            con.close()

    def executed_qids(self) -> set[str]:
        con = self._con()
        try:
            rows = con.execute("SELECT qid FROM queries WHERE status = 'executed'").fetchall()
            return {r[0] for r in rows}
        finally:
            con.close()

    def query_tables(self, qid: str) -> list[str]:
        row = self.query_row(qid)
        if not row or not row.get("tables_json"):
            return []
        try:
            return json.loads(row["tables_json"])
        except (json.JSONDecodeError, TypeError):
            return []

    # -- checkpoints -----------------------------------------------------

    def seed_checkpoints(self, checks: list[tuple[str, str]]) -> None:
        con = self._con()
        try:
            con.executemany(
                "INSERT OR IGNORE INTO checkpoints(key, title, status) VALUES(?, ?, 'open')",
                checks,
            )
            con.commit()
        finally:
            con.close()

    def checkpoints(self) -> list[dict]:
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT key, title, status, evidence, note, completed_by, completed_at "
                "FROM checkpoints ORDER BY rowid"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else []
                out.append(d)
            return out
        finally:
            con.close()

    def checkpoint(self, key: str) -> dict | None:
        return next((c for c in self.checkpoints() if c["key"] == key), None)

    def complete_checkpoint(self, key: str, evidence: list[str], note: str, actor: str) -> None:
        con = self._con()
        try:
            cur = con.execute(
                "UPDATE checkpoints SET status='complete', evidence=?, note=?, "
                "completed_by=?, completed_at=? WHERE key=?",
                (json.dumps(evidence), note, actor, utcnow(), key),
            )
            con.commit()
            if cur.rowcount == 0:
                raise KeyError(f"no checkpoint '{key}'")
        finally:
            con.close()
        self.log_event(actor, "checkpoint_completed", {"key": key, "evidence": evidence})

    def reopen_checkpoint(self, key: str, actor: str = "user") -> None:
        con = self._con()
        try:
            con.execute("UPDATE checkpoints SET status='open' WHERE key=?", (key,))
            con.commit()
        finally:
            con.close()
        self.log_event(actor, "checkpoint_reopened", {"key": key})

    # -- findings --------------------------------------------------------

    def add_finding(
        self,
        schema_name: str,
        severity: str,
        confidence: str,
        title: str,
        payload: dict,
        worker: str | None = None,
    ) -> str:
        con = self._con()
        try:
            for _ in range(50):
                con.execute("BEGIN IMMEDIATE")
                n = con.execute("SELECT COUNT(*) FROM findings").fetchone()[0]
                fid = f"f_{n + 1:03d}"
                try:
                    con.execute(
                        "INSERT INTO findings(fid, ts, worker, schema_name, severity, "
                        "confidence, title, payload) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            fid,
                            utcnow(),
                            worker,
                            schema_name,
                            severity,
                            confidence,
                            title,
                            json.dumps(payload, default=str),
                        ),
                    )
                    con.commit()
                    self.log_event(worker or "agent", "finding_added", {"fid": fid})
                    return fid
                except sqlite3.IntegrityError:
                    con.rollback()
            raise RuntimeError("could not allocate finding id")
        finally:
            con.close()

    def findings(self) -> list[dict]:
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT fid, ts, worker, schema_name, severity, confidence, title, "
                "accepted, payload FROM findings ORDER BY fid"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d["payload"])
                d["accepted"] = bool(d["accepted"])
                out.append(d)
            return out
        finally:
            con.close()

    def finding(self, fid: str) -> dict | None:
        return next((f for f in self.findings() if f["fid"] == fid), None)

    def accept_finding(self, fid: str, actor: str = "user") -> None:
        con = self._con()
        try:
            cur = con.execute("UPDATE findings SET accepted=1 WHERE fid=?", (fid,))
            con.commit()
            if cur.rowcount == 0:
                raise KeyError(f"no finding '{fid}'")
        finally:
            con.close()
        self.log_event(actor, "finding_accepted", {"fid": fid})

    # -- summary / cleanup ----------------------------------------------

    def summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.get_meta("title", ""),
            "workflow": self.workflow,
            "stage": self.stage,
            "created_at": self.get_meta("created_at"),
            "targets": self.targets,
            "guard_profile": self.get_meta("guard_profile"),
            "guard": self.guard_settings.model_dump(),
            "strict_scope": self.strict_scope,
            "connection": self.connection,
            "workers": self.workers(),
            "queries_executed": self.executed_count(),
        }

    def scrub_data(self) -> int:
        """Delete cached warehouse data; keep the audit trail and sidecars."""
        count = self.cache.drop_all_data()
        self.log_event("user", "data_scrubbed", {"artifacts_deleted": count})
        return count

    def delete(self) -> None:
        shutil.rmtree(self.dir)


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con
