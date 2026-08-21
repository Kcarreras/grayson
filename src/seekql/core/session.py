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

#: aliases accepted anywhere a session id is expected; resolve to the newest session
LATEST_ALIASES = {"latest", "last", "."}


def resolve_session_id(workspace: Workspace, session_id: str) -> str:
    """Resolve 'latest'/'last'/'.' to the most recently created session id."""
    if session_id not in LATEST_ALIASES:
        return session_id
    ids = workspace.list_session_ids()  # sorted; ids start with a UTC timestamp
    if not ids:
        raise FileNotFoundError("no sessions yet — start one with `seekql session start`")
    return ids[-1]


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
CREATE TABLE IF NOT EXISTS interventions(
    iid TEXT PRIMARY KEY, ts TEXT NOT NULL, worker TEXT,
    kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    title TEXT NOT NULL, prompt TEXT, request TEXT NOT NULL,
    response TEXT, responded_at TEXT
);
CREATE TABLE IF NOT EXISTS proposals(
    pid TEXT PRIMARY KEY, ts TEXT NOT NULL, worker TEXT,
    finding_fid TEXT, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'proposed',
    title TEXT NOT NULL, payload TEXT NOT NULL,
    decided_by TEXT, decided_at TEXT, verification TEXT
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

    def meta_all(self) -> dict[str, str]:
        con = self._con()
        try:
            return dict(con.execute("SELECT key, value FROM meta").fetchall())
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
                # MAX(rowid) is O(1); rows are never deleted so it equals COUNT(*).
                n = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM queries").fetchone()[0]
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
                "ORDER BY rowid DESC LIMIT ?",
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

    def budget_consumed_count(self) -> int:
        """Queries counting against the budget: everything allocated except those
        the guard rejected. Includes 'pending' (in-flight) rows so concurrent
        workers see each other and cannot all slip under a hard cap (TOCTOU)."""
        con = self._con()
        try:
            return con.execute(
                "SELECT COUNT(*) FROM queries WHERE status != 'rejected'"
            ).fetchone()[0]
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
        return self.query_tables_many([qid]).get(qid, [])

    def query_tables_many(self, qids: list[str]) -> dict[str, list[str]]:
        """Source tables for several queries in one round-trip."""
        if not qids:
            return {}
        con = self._con()
        try:
            marks = ", ".join("?" for _ in qids)
            rows = con.execute(
                f"SELECT qid, tables_json FROM queries WHERE qid IN ({marks})",  # noqa: S608
                qids,
            ).fetchall()
        finally:
            con.close()
        out: dict[str, list[str]] = {}
        for qid, tables_json in rows:
            try:
                out[qid] = json.loads(tables_json) if tables_json else []
            except (json.JSONDecodeError, TypeError):
                out[qid] = []
        return out

    def query_stats(self) -> dict:
        """Aggregate query counts and totals, grouped by status."""
        con = self._con()
        try:
            rows = con.execute(
                "SELECT status, COUNT(*), COALESCE(SUM(duration_ms), 0), "
                "COALESCE(SUM(row_count), 0) FROM queries GROUP BY status"
            ).fetchall()
        finally:
            con.close()
        by_status = {r[0]: r[1] for r in rows}
        executed = next((r for r in rows if r[0] == "executed"), (None, 0, 0, 0))
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "executed_duration_ms": executed[2],
            "executed_rows": executed[3],
        }

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
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT key, title, status, evidence, note, completed_by, completed_at "
                "FROM checkpoints WHERE key = ?",
                (key,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        d = dict(row)
        d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else []
        return d

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
                n = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM findings").fetchone()[0]
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
                "accepted, payload FROM findings ORDER BY rowid"
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
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT fid, ts, worker, schema_name, severity, confidence, title, "
                "accepted, payload FROM findings WHERE fid = ?",
                (fid,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        d["accepted"] = bool(d["accepted"])
        return d

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

    # -- interventions ---------------------------------------------------

    def add_intervention(
        self, kind: str, title: str, prompt: str, request: dict, worker: str | None = None
    ) -> str:
        con = self._con()
        try:
            for _ in range(50):
                con.execute("BEGIN IMMEDIATE")
                n = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM interventions").fetchone()[0]
                iid = f"i_{n + 1:03d}"
                try:
                    con.execute(
                        "INSERT INTO interventions(iid, ts, worker, kind, status, title, "
                        "prompt, request) VALUES(?, ?, ?, ?, 'open', ?, ?, ?)",
                        (
                            iid,
                            utcnow(),
                            worker,
                            kind,
                            title,
                            prompt,
                            json.dumps(request, default=str),
                        ),
                    )
                    con.commit()
                    self.log_event(
                        worker or "agent", "intervention_opened", {"iid": iid, "kind": kind}
                    )
                    return iid
                except sqlite3.IntegrityError:
                    con.rollback()
            raise RuntimeError("could not allocate intervention id")
        finally:
            con.close()

    def _hydrate_intervention(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["request"] = json.loads(d["request"]) if d["request"] else {}
        d["response"] = json.loads(d["response"]) if d["response"] else None
        return d

    def interventions(self, status: str | None = None) -> list[dict]:
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            if status:
                rows = con.execute(
                    "SELECT * FROM interventions WHERE status = ? ORDER BY iid", (status,)
                ).fetchall()
            else:
                rows = con.execute("SELECT * FROM interventions ORDER BY iid").fetchall()
            return [self._hydrate_intervention(r) for r in rows]
        finally:
            con.close()

    def intervention(self, iid: str) -> dict | None:
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM interventions WHERE iid = ?", (iid,)).fetchone()
            return self._hydrate_intervention(row) if row else None
        finally:
            con.close()

    def respond_intervention(self, iid: str, response: dict, actor: str = "user") -> None:
        con = self._con()
        try:
            cur = con.execute(
                "UPDATE interventions SET status='answered', response=?, responded_at=? "
                "WHERE iid=? AND status='open'",
                (json.dumps(response, default=str), utcnow(), iid),
            )
            con.commit()
            if cur.rowcount == 0:
                existing = con.execute(
                    "SELECT status FROM interventions WHERE iid=?", (iid,)
                ).fetchone()
                if existing is None:
                    raise KeyError(f"no intervention '{iid}'")
                raise ValueError(f"intervention '{iid}' is not open (status={existing[0]})")
        finally:
            con.close()
        self.log_event(actor, "intervention_answered", {"iid": iid})

    def cancel_intervention(self, iid: str, actor: str = "agent") -> None:
        con = self._con()
        try:
            con.execute(
                "UPDATE interventions SET status='cancelled' WHERE iid=? AND status='open'",
                (iid,),
            )
            con.commit()
        finally:
            con.close()
        self.log_event(actor, "intervention_cancelled", {"iid": iid})

    # -- proposals -------------------------------------------------------

    def add_proposal(
        self,
        kind: str,
        title: str,
        payload: dict,
        finding_fid: str | None,
        worker: str | None = None,
    ) -> str:
        con = self._con()
        try:
            for _ in range(50):
                con.execute("BEGIN IMMEDIATE")
                n = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM proposals").fetchone()[0]
                pid = f"p_{n + 1:03d}"
                try:
                    con.execute(
                        "INSERT INTO proposals(pid, ts, worker, finding_fid, kind, status, "
                        "title, payload) VALUES(?, ?, ?, ?, ?, 'proposed', ?, ?)",
                        (
                            pid,
                            utcnow(),
                            worker,
                            finding_fid,
                            kind,
                            title,
                            json.dumps(payload, default=str),
                        ),
                    )
                    con.commit()
                    self.log_event(
                        worker or "agent",
                        "proposal_added",
                        {"pid": pid, "kind": kind, "finding": finding_fid},
                    )
                    return pid
                except sqlite3.IntegrityError:
                    con.rollback()
            raise RuntimeError("could not allocate proposal id")
        finally:
            con.close()

    def _hydrate_proposal(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        d["verification"] = json.loads(d["verification"]) if d["verification"] else None
        return d

    def proposals(self, status: str | None = None) -> list[dict]:
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            if status:
                rows = con.execute(
                    "SELECT * FROM proposals WHERE status = ? ORDER BY pid", (status,)
                ).fetchall()
            else:
                rows = con.execute("SELECT * FROM proposals ORDER BY pid").fetchall()
            return [self._hydrate_proposal(r) for r in rows]
        finally:
            con.close()

    def proposal(self, pid: str) -> dict | None:
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM proposals WHERE pid = ?", (pid,)).fetchone()
            return self._hydrate_proposal(row) if row else None
        finally:
            con.close()

    def decide_proposal(self, pid: str, status: str, actor: str = "user") -> None:
        if status not in {"approved", "rejected"}:
            raise ValueError("status must be approved or rejected")
        con = self._con()
        try:
            cur = con.execute(
                "UPDATE proposals SET status=?, decided_by=?, decided_at=? "
                "WHERE pid=? AND status='proposed'",
                (status, actor, utcnow(), pid),
            )
            con.commit()
            if cur.rowcount == 0:
                existing = con.execute(
                    "SELECT status FROM proposals WHERE pid=?", (pid,)
                ).fetchone()
                if existing is None:
                    raise KeyError(f"no proposal '{pid}'")
                raise ValueError(f"proposal '{pid}' is not pending (status={existing[0]})")
        finally:
            con.close()
        self.log_event(actor, f"proposal_{status}", {"pid": pid})

    def set_proposal_status(self, pid: str, status: str, actor: str = "agent") -> None:
        con = self._con()
        try:
            con.execute("UPDATE proposals SET status=? WHERE pid=?", (status, pid))
            con.commit()
        finally:
            con.close()
        self.log_event(actor, "proposal_status", {"pid": pid, "status": status})

    def attach_verification(self, pid: str, verification: dict, actor: str = "agent") -> None:
        con = self._con()
        try:
            status = "verified" if verification.get("verdict") == "pass" else "verification_failed"
            cur = con.execute(
                "UPDATE proposals SET verification=?, status=? WHERE pid=?",
                (json.dumps(verification, default=str), status, pid),
            )
            con.commit()
            if cur.rowcount == 0:
                raise KeyError(f"no proposal '{pid}'")
        finally:
            con.close()
        self.log_event(
            actor, "proposal_verified", {"pid": pid, "verdict": verification.get("verdict")}
        )

    # -- summary / cleanup ----------------------------------------------

    def summary(self) -> dict:
        # One connection for everything: meta, workers, and the executed count.
        con = self._con()
        try:
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
            workers = [
                {"id": r[0], "label": r[1], "joined_at": r[2]}
                for r in con.execute("SELECT id, label, joined_at FROM workers").fetchall()
            ]
            executed = con.execute(
                "SELECT COUNT(*) FROM queries WHERE status = 'executed'"
            ).fetchone()[0]
        finally:
            con.close()
        guard_raw = meta.get("guard")
        guard = GuardSettings.model_validate_json(guard_raw) if guard_raw else GuardSettings()
        return {
            "id": self.id,
            "title": meta.get("title", ""),
            "workflow": meta.get("workflow", ""),
            "stage": meta.get("stage", "setup"),
            "created_at": meta.get("created_at"),
            "targets": json.loads(meta.get("targets") or "[]"),
            "guard_profile": meta.get("guard_profile"),
            "guard": guard.model_dump(),
            "strict_scope": bool(json.loads(meta.get("strict_scope") or "false")),
            "connection": meta.get("connection", "default"),
            "workers": workers,
            "queries_executed": executed,
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
