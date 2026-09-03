"""Session state: SQLite-backed (WAL) so parallel workers never corrupt state."""

from __future__ import annotations

import contextlib
import json
import secrets
import shutil
import sqlite3
from pathlib import Path

from grayson.cache.store import CacheStore
from grayson.config import GuardSettings
from grayson.util import is_object_name, new_session_id, utcnow
from grayson.workspace import Workspace

STAGES = ["setup", "analysis", "synthesis", "review", "fixes", "verification", "closed"]

#: how a closed session ended. 'clean' means the required checks cleared and
#: nothing was found worth acting on — an outcome a human confirmed, not a
#: session that merely ran out of road. 'abandoned' is the honest label for a
#: session closed without any result (broken, mistaken, no longer relevant):
#: it skipped the gates on purpose, says why, and published nothing.
OUTCOMES = ["clean", "findings", "abandoned"]

#: aliases accepted anywhere a session id is expected; resolve to the newest session
LATEST_ALIASES = {"latest", "last", "."}


def resolve_session_id(workspace: Workspace, session_id: str) -> str:
    """Resolve 'latest'/'last'/'.' to the most recently created session id."""
    if session_id not in LATEST_ALIASES:
        return session_id
    ids = workspace.list_session_ids()  # sorted; ids start with a UTC timestamp
    if not ids:
        raise FileNotFoundError("no sessions yet — start one with `grayson session start`")
    return ids[-1]


def find_recent_duplicate(
    workspace: Workspace, workflow: str, targets: list[str], window_minutes: int = 10
) -> str | None:
    """A just-created, still-empty session with the same workflow and targets.

    Agents sometimes re-run `session start` (lost output, retry after a shell
    hiccup); treating the re-run as idempotent beats litter of abandoned twins.
    Only sessions with zero queries count — once work exists, a new session is
    assumed intentional.
    """
    from datetime import UTC, datetime, timedelta

    wanted = sorted(t.upper() for t in targets)
    for sid in reversed(workspace.list_session_ids()[-5:]):
        try:
            s = Session(workspace, sid)
            meta = s.meta_all()
        except (OSError, ValueError):
            continue
        if meta.get("workflow") != workflow or meta.get("stage") == "closed":
            continue
        if sorted(json.loads(meta.get("targets") or "[]")) != wanted:
            continue
        try:
            created = datetime.strptime(meta.get("created_at") or "", "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except ValueError:
            continue
        if datetime.now(UTC) - created > timedelta(minutes=window_minutes):
            continue
        if s.query_stats()["total"] == 0:
            return sid
    return None


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
    evidence TEXT, note TEXT, completed_by TEXT, completed_at TEXT,
    evidence_off_scope TEXT
);
CREATE TABLE IF NOT EXISTS findings(
    fid TEXT PRIMARY KEY, ts TEXT NOT NULL, worker TEXT,
    schema_name TEXT NOT NULL, severity TEXT, confidence TEXT,
    title TEXT NOT NULL, accepted INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    superseded_by TEXT, rejected_reason TEXT, rejected_at TEXT
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
        self._migrate()

    #: additive columns for session DBs created before the feature existed
    _MIGRATIONS = (
        "ALTER TABLE findings ADD COLUMN superseded_by TEXT",
        "ALTER TABLE findings ADD COLUMN rejected_reason TEXT",
        "ALTER TABLE findings ADD COLUMN rejected_at TEXT",
        "ALTER TABLE checkpoints ADD COLUMN evidence_off_scope TEXT",
    )

    def _migrate(self) -> None:
        con = self._con()
        try:
            for stmt in self._MIGRATIONS:
                with contextlib.suppress(sqlite3.OperationalError):  # column exists
                    con.execute(stmt)
            con.commit()
        finally:
            con.close()

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
        actor: str = "user",
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
        # attributed to whoever actually started it: an agent-started session used to
        # be recorded under the human's name, same class of misattribution the stage
        # and close paths carried
        session.log_event(actor, "session_created", {"workflow": workflow, "targets": targets})
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

    # -- setup inputs ----------------------------------------------------

    def setup_inputs(self) -> dict[str, str]:
        """The workflow setup answers recorded on this session (may be empty —
        answers historically lived only in the agent's chat transcript)."""
        raw = self.get_meta("setup_inputs")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def set_setup_inputs(self, inputs: dict[str, str], actor: str = "user") -> dict[str, str]:
        """Record (merge) setup-input answers, so the session itself says why it
        was started — not just the chat transcript. Returns the merged dict."""
        merged = {**self.setup_inputs(), **{k: str(v) for k, v in inputs.items()}}
        self.set_meta("setup_inputs", json.dumps(merged))
        self.log_event(actor, "setup_inputs_recorded", {"keys": sorted(inputs)})
        return merged

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

    def widen_scope(self, tables: list[str], actor: str = "user", via: str = "") -> dict:
        """Bring tables into the readable scope, as a logged user decision.

        Scope only ever widens by a human's say-so: the setup inputs flagged
        for it at session start, a registered view, the console or
        `grayson session scope`, or a granted scope_request intervention. This
        is the one write path for the last three, so every widening lands in
        the audit trail with who did it and through what (`via`: the console,
        the command, or the intervention id whose answer granted it)."""
        names: list[str] = []
        for raw in tables:
            name = str(raw).strip().upper()
            if not is_object_name(name):
                raise ValueError(
                    f"'{raw}' is not a table name — use DB.SCHEMA.TABLE, one per entry"
                )
            if name not in names:
                names.append(name)
        if not names:
            raise ValueError("no tables named")
        before = self.scope_tables
        added = [n for n in names if n not in before]
        if added:
            self.add_scope(added)
        self.log_event(actor, "scope_changed", {"tables": names, "added": added, "via": via})
        return {"added": added, "scope": sorted(self.scope_tables)}

    def add_scope(self, tables: list[str]) -> None:
        extra = set(json.loads(self.get_meta("scope_extra", "[]") or "[]"))
        extra.update(t.upper() for t in tables)
        self.set_meta("scope_extra", json.dumps(sorted(extra)))

    @property
    def outcome(self) -> str:
        """How a closed session ended: '' while open, then 'clean', 'findings',
        or 'abandoned' (closed on purpose without a result).

        A clean run — checks cleared, nothing wrong found — is a real result, not
        an unfinished investigation. Recording it as such is what lets "we looked
        and it was fine" compound in the library alongside defects.
        """
        return self.get_meta("outcome", "") or ""

    def set_outcome(self, outcome: str, actor: str = "user", note: str = "") -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome '{outcome}' (outcomes: {', '.join(OUTCOMES)})")
        self.set_meta("outcome", outcome)
        if note.strip():
            self.set_meta("outcome_note", note.strip())
        self.log_event(actor, "outcome_recorded", {"outcome": outcome, "note": note.strip()})

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

    def query_log(self, limit: int = 200, status: str | None = None) -> list[dict]:
        """Newest first; `status` narrows to one outcome (executed, rejected, …)."""
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            where = "WHERE status = ? " if status else ""
            params: tuple = (status, limit) if status else (limit,)
            rows = con.execute(
                "SELECT qid, worker, ts, status, guard_rule, reason, duration_ms, "
                "row_count, truncated, label, tables_json, "
                f"substr(sql_raw, 1, 300) AS sql_raw FROM queries {where}"
                "ORDER BY rowid DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def executed_statements(self) -> list[dict]:
        """Full executed SQL text, for audit reconciliation against warehouse history."""
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT qid, ts, COALESCE(sql_executed, sql_raw) AS sql FROM queries "
                "WHERE status = 'executed' ORDER BY rowid"
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
                "SELECT key, title, status, evidence, note, completed_by, completed_at, "
                "evidence_off_scope FROM checkpoints ORDER BY rowid"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else []
                d["evidence_off_scope"] = (
                    json.loads(d["evidence_off_scope"]) if d["evidence_off_scope"] else []
                )
                out.append(d)
            return out
        finally:
            con.close()

    def checkpoint(self, key: str) -> dict | None:
        con = self._con()
        try:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT key, title, status, evidence, note, completed_by, completed_at, "
                "evidence_off_scope FROM checkpoints WHERE key = ?",
                (key,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        d = dict(row)
        d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else []
        d["evidence_off_scope"] = (
            json.loads(d["evidence_off_scope"]) if d["evidence_off_scope"] else []
        )
        return d

    def complete_checkpoint(
        self,
        key: str,
        evidence: list[str],
        note: str,
        actor: str,
        off_scope: list[str] | None = None,
    ) -> None:
        con = self._con()
        try:
            cur = con.execute(
                "UPDATE checkpoints SET status='complete', evidence=?, note=?, "
                "completed_by=?, completed_at=?, evidence_off_scope=? WHERE key=?",
                (
                    json.dumps(evidence),
                    note,
                    actor,
                    utcnow(),
                    json.dumps(off_scope) if off_scope else None,
                    key,
                ),
            )
            con.commit()
            if cur.rowcount == 0:
                raise KeyError(f"no checkpoint '{key}'")
        finally:
            con.close()
        self.log_event(actor, "checkpoint_completed", {"key": key, "evidence": evidence})

    def waive_checkpoint(self, key: str, reason: str, actor: str = "user") -> None:
        """Mark a checkpoint not-applicable, with the reason on the record.

        A waive is the honest alternative to evidence-laundering: an agent facing
        an inapplicable check (freshness on a static dimension table) otherwise has
        to manufacture a nominally-relevant query to clear the gate. The reason is
        mandatory and the waiver is named, so a waived gate reads differently from
        a closed one everywhere it is shown.
        """
        if not reason.strip():
            raise ValueError("a waive requires a reason — it is what makes the gap auditable")
        con = self._con()
        try:
            row = con.execute("SELECT status FROM checkpoints WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(f"no checkpoint '{key}'")
            if row[0] == "complete":
                # waiving over a completed check would silently discard the evidence
                # on record — the reverse of what a waive is for
                raise ValueError(
                    f"checkpoint '{key}' is already complete, with evidence on the "
                    "record — waiving it now would discard that evidence. Reopen it "
                    "first (`grayson checkpoint reopen`) if it really should be waived."
                )
            con.execute(
                "UPDATE checkpoints SET status='waived', evidence=NULL, note=?, "
                "completed_by=?, completed_at=?, evidence_off_scope=NULL WHERE key=?",
                (reason.strip(), actor, utcnow(), key),
            )
            con.commit()
        finally:
            con.close()
        self.log_event(actor, "checkpoint_waived", {"key": key, "reason": reason.strip()})

    def reopen_checkpoint(self, key: str, actor: str = "user") -> None:
        con = self._con()
        try:
            con.execute(
                "UPDATE checkpoints SET status='open', completed_by=NULL, completed_at=NULL "
                "WHERE key=?",
                (key,),
            )
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
                "accepted, payload, superseded_by, rejected_reason, rejected_at "
                "FROM findings ORDER BY rowid"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d["payload"])
                d["accepted"] = bool(d["accepted"])
                d["rejected"] = bool(d["rejected_reason"])
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
                "accepted, payload, superseded_by, rejected_reason, rejected_at "
                "FROM findings WHERE fid = ?",
                (fid,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        d["accepted"] = bool(d["accepted"])
        d["rejected"] = bool(d["rejected_reason"])
        return d

    def accept_finding(self, fid: str, actor: str = "user") -> None:
        """Accept a finding (a user action). If the finding proposed to
        supersede an earlier one, the supersession executes here — inside the
        acceptance — so agents can only ever suggest it, never perform it."""
        con = self._con()
        try:
            cur = con.execute(
                "UPDATE findings SET accepted=1, rejected_reason=NULL, rejected_at=NULL "
                "WHERE fid=?",
                (fid,),
            )
            con.commit()
            if cur.rowcount == 0:
                raise KeyError(f"no finding '{fid}'")
        finally:
            con.close()
        self.log_event(actor, "finding_accepted", {"fid": fid})
        target = (self.finding(fid) or {}).get("payload", {}).get("supersedes")
        if target:
            con = self._con()
            try:
                # first-wins: never re-point a finding that is already superseded
                cur = con.execute(
                    "UPDATE findings SET superseded_by=? WHERE fid=? AND superseded_by IS NULL",
                    (fid, target),
                )
                con.commit()
            finally:
                con.close()
            if cur.rowcount:
                self.log_event(actor, "finding_superseded", {"fid": target, "by": fid})
        # acceptance is the provenance gate: the vetted finding compounds into
        # the team library (best-effort — publication never fails the accept)
        from grayson.records import publish_finding

        publish_finding(self, fid)

    def reject_finding(self, fid: str, reason: str, actor: str = "user") -> None:
        """Reject a finding with a required reason (a user action). The reason
        is the agent's signal to continue analysis in a corrected direction."""
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("a rejection requires a reason — it is what the agent works from")
        con = self._con()
        try:
            cur = con.execute(
                "UPDATE findings SET accepted=0, rejected_reason=?, rejected_at=? WHERE fid=?",
                (reason, utcnow(), fid),
            )
            con.commit()
            if cur.rowcount == 0:
                raise KeyError(f"no finding '{fid}'")
        finally:
            con.close()
        self.log_event(actor, "finding_rejected", {"fid": fid, "reason": reason})

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
            kind = con.execute("SELECT kind FROM interventions WHERE iid=?", (iid,)).fetchone()[0]
        finally:
            con.close()
        self.log_event(actor, "intervention_answered", {"iid": iid})
        # A granted scope request IS the authorization: the human's answer
        # widens the scope here, in the one place every response surface
        # (console, CLI) passes through, so the query that follows is in scope
        # and its citation counts — rather than an out-of-scope read whose link
        # to the answer that allowed it is left for a later reader to infer.
        if kind == "scope_request" and response.get("granted"):
            self.widen_scope(list(response["granted"]), actor=actor, via=iid)

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
            "outcome": meta.get("outcome", ""),
            "outcome_note": meta.get("outcome_note", ""),
            "created_at": meta.get("created_at"),
            "targets": json.loads(meta.get("targets") or "[]"),
            "guard_profile": meta.get("guard_profile"),
            "guard": guard.model_dump(),
            "strict_scope": bool(json.loads(meta.get("strict_scope") or "false")),
            "scope_extra": sorted(json.loads(meta.get("scope_extra") or "[]")),
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
