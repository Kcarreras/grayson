"""Cross-session records: findings, proposals, and their verifications as a
searchable archive.

Sessions are the unit of work; records are the unit of memory. This module
lets humans (console Records tab) and agents (CLI `records search`, MCP
`records_search`) find past problems and fixes without knowing which session
they happened in — "how did we fix the promo fan-out last quarter?".

Records also COMPOUND across the team: at the human-approved moments — a
finding accepted, a fix verification recorded — the distilled record is
published into the library's `records/` directory (small JSON files, git-
shared like knowledge facts). Raw session state (cache, query log,
interventions) stays local; only the vetted output travels. Teammates' records
merge into every search here, and the knowledge-only server exposes them
read-only.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from grayson.core.session import Session
from grayson.workspace import Workspace

RECORD_KINDS = ("finding", "proposal", "report")

#: published-record format version, stamped on every record written. The same
#: contract as knowledge docs (docs/LIBRARY.md "Format stability"): additive-only
#: within a version, unstamped means format 1, a newer stamp loads best-effort
#: (fields are additive) and `library doctor` flags it for upgrade.
RECORDS_FORMAT = 1


def _finding_row(base: dict, f: dict) -> dict:
    return {
        **base,
        "kind": "finding",
        "id": f["fid"],
        "ts": f["ts"],
        "title": f["title"],
        "severity": f["severity"],
        "accepted": f["accepted"],
        "superseded_by": f.get("superseded_by"),
        "supersedes": f["payload"].get("supersedes"),
        "rejected": f.get("rejected", False),
        "rejected_reason": f.get("rejected_reason"),
        "summary": f["payload"].get("summary", ""),
        "payload": f["payload"],
    }


def _proposal_row(base: dict, p: dict) -> dict:
    verification = p.get("verification") or {}
    return {
        **base,
        "kind": "proposal",
        "id": p["pid"],
        "ts": p["ts"],
        "title": p["title"],
        "status": p["status"],
        "finding_fid": p.get("finding_fid"),
        "verdict": verification.get("verdict"),
        "summary": p["payload"].get("rationale", "")[:400],
        "payload": p["payload"],
    }


def _session_base(sid: str, meta: dict) -> dict:
    return {
        "session_id": sid,
        "session_title": meta.get("title", ""),
        "workflow": meta.get("workflow", ""),
        "stage": meta.get("stage", ""),
    }


def collect_records(workspace: Workspace, kind: str | None = None) -> list[dict]:
    """All findings and proposals across local sessions, newest session first,
    followed by team records from the library that no local session covers."""
    if kind is not None and kind not in RECORD_KINDS:
        raise ValueError(f"kind must be one of {RECORD_KINDS}")
    out: list[dict] = []
    for sid in reversed(workspace.list_session_ids()):
        try:
            s = Session(workspace, sid)
            meta = s.meta_all()
        except (OSError, ValueError):
            continue
        base = {**_session_base(sid, meta), "source": "session"}
        if kind in (None, "finding"):
            out.extend(_finding_row(base, f) for f in s.findings())
        if kind in (None, "proposal"):
            out.extend(_proposal_row(base, p) for p in s.proposals())
    seen = {(r["session_id"], r["kind"], r["id"]) for r in out}
    library = [
        r
        for r in library_records(workspace.records_dir, kind)
        if (r["session_id"], r["kind"], r["id"]) not in seen
    ]
    library.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return out + library


# -- library publication (the compounding path) --------------------------


def _record_path(records_dir: Path, session_id: str, record_id: str) -> Path:
    return records_dir / session_id / f"{record_id}.json"


def evidence_snapshot(session: Session, qids: list[str]) -> list[dict]:
    """The cited queries themselves — statement, executed form, timestamp,
    outcome, tables — in citation order, deduplicated, unknown ids skipped.

    Query ids are per-session counters (`q_0002` means nothing outside its
    session), and the SQL lives only in local session state. Publishing this
    beside a record is what lets "evidence or it didn't happen" survive the
    trip to the library: a teammate reading a finding sees the queries that
    proved it, not just their numbers. Results stay local — the statement and
    its stats are what a reviewer needs; rows can be large and sensitive."""
    out: list[dict] = []
    for qid in dict.fromkeys(q for q in qids if q):
        row = session.query_row(qid)
        if row is None:
            continue
        executed = row.get("sql_executed")
        entry = {
            "qid": qid,
            "session_id": session.id,
            "ts": row.get("ts"),
            "status": row.get("status"),
            "sql": row.get("sql_raw") or "",
            "row_count": row.get("row_count"),
            "truncated": bool(row.get("truncated")),
            "tables": json.loads(row["tables_json"]) if row.get("tables_json") else [],
        }
        if executed and executed != entry["sql"]:
            entry["sql_executed"] = executed  # e.g. the guard's injected LIMIT
        if row.get("label"):
            entry["label"] = row["label"]
        out.append(entry)
    return out


def _publish(
    workspace: Workspace,
    row: dict,
    record: dict,
    message: str,
    evidence: list[dict] | None = None,
) -> None:
    """Write one distilled record into the library and auto-push if configured.

    Best-effort by design: publication must never fail the user action
    (acceptance/verification) it rides on.
    """
    from grayson.identity import get_user_id
    from grayson.library import maybe_auto_push
    from grayson.util import atomic_write_text, utcnow

    try:
        path = _record_path(workspace.records_dir, row["session_id"], row["id"])
        doc = {
            "format": RECORDS_FORMAT,
            **{k: v for k, v in row.items() if k != "source"},
            "author": get_user_id(),
            "published_at": utcnow(),
            "record": record,
        }
        if evidence:
            doc["evidence_queries"] = evidence
        atomic_write_text(path, json.dumps(doc, indent=2, default=str) + "\n")
        maybe_auto_push(workspace, message)
    except OSError:
        return


def publish_finding(session: Session, fid: str) -> None:
    """Publish an accepted finding to the library (called from acceptance).

    Also republishes a finding this one superseded, so its library copy shows
    the supersession instead of standing as current knowledge.
    """
    f = session.finding(fid)
    if f is None or not f.get("accepted"):
        return
    base = _session_base(session.id, session.meta_all())
    _publish(
        session.workspace,
        _finding_row(base, f),
        f,
        f"grayson records: finding {fid} ({session.id})",
        evidence=evidence_snapshot(session, f["payload"].get("evidence") or []),
    )
    target = f["payload"].get("supersedes")
    if target:
        old = session.finding(target)
        if old is not None and old.get("accepted"):
            _publish(
                session.workspace,
                _finding_row(base, old),
                old,
                f"grayson records: supersede {target} ({session.id})",
                evidence=evidence_snapshot(session, old["payload"].get("evidence") or []),
            )


def publish_proposal(session: Session, pid: str) -> None:
    """Publish a proposal once its verification is recorded (either verdict —
    'this fix did not work' is team knowledge too)."""
    p = session.proposal(pid)
    if p is None or not p.get("verification"):
        return
    base = _session_base(session.id, session.meta_all())
    verification = p.get("verification") or {}
    _publish(
        session.workspace,
        _proposal_row(base, p),
        p,
        f"grayson records: proposal {pid} ({session.id})",
        evidence=evidence_snapshot(
            session, [verification.get("before_qid"), verification.get("after_qid")]
        ),
    )


def publish_report(session: Session) -> None:
    """Publish the session's full report at close — the third compounding
    artifact beside knowledge and records: the whole story of an investigation,
    searchable from any linked workspace.

    Close is the human-approved moment (a user action, like acceptance for
    findings), so publication rides on it. Best-effort like every publication:
    it must never fail the close itself. Writes records/<sid>/report.json (the
    searchable row + full report) and report.md (the rendered document, using
    the library's default profile) — plus records/<sid>/charts/<id>.svg when
    the profile asks for pictures (`charts: svg|both`), since the session that
    could render them stays local and the report is what the team has.
    """
    try:
        from grayson.report import (
            ReportError,
            ReportProfile,
            build_report,
            export_chart_svgs,
            load_profile,
        )
        from grayson.report import render_markdown as _render

        ws = session.workspace
        report = build_report(session, ws.workflows_dir)
        try:
            profile = load_profile(ws.reports_dir)
        except ReportError:
            profile = ReportProfile()  # a broken profile must not block the close
        meta = session.meta_all()
        ready = report["readiness"]
        summary = _outcome_summary(meta, ready)
        from grayson.util import atomic_write_text

        folder = ws.records_dir / session.id
        chart_files = export_chart_svgs(session, folder) if profile.charts != "text" else {}
        atomic_write_text(folder / "report.md", _render(report, profile, chart_files))
        row = {
            **_session_base(session.id, meta),
            "kind": "report",
            "id": "report",
            "ts": report["generated_at"],
            "title": meta.get("title") or session.id,
            "outcome": meta.get("outcome", ""),
            "summary": summary,
            "payload": {
                "targets": report["session"].get("targets", []),
                "queries_executed": ready.get("queries_executed", 0),
                "findings_accepted": ready.get("findings_accepted", 0),
                "waived_checks": [w["key"] for w in ready.get("waived_checks", [])],
                "narrative": report.get("narrative", ""),
            },
        }
        cited: list[str] = []
        for cp in report.get("checkpoints") or []:
            cited += cp.get("evidence") or []
        for finding in report.get("findings") or []:
            cited += (finding.get("payload") or {}).get("evidence") or []
        _publish(
            session.workspace,
            row,
            report,
            f"grayson records: report ({session.id})",
            evidence=evidence_snapshot(session, cited),
        )
    except (OSError, ValueError, KeyError):  # pragma: no cover - best-effort
        return


def _outcome_summary(meta: dict, ready: dict) -> str:
    outcome = meta.get("outcome", "")
    note = (meta.get("outcome_note") or "").strip()
    head = {
        "clean": "clean — checks cleared, nothing found worth acting on",
        "findings": f"closed on {ready.get('findings_accepted', 0)} accepted finding(s)",
    }.get(outcome, f"stage {meta.get('stage', '')}")
    counts = f"{ready.get('queries_executed', 0)} queries executed"
    return f"{head}; {counts}" + (f" — {note}" if note else "")


# -- removal (the author's action, or an admin's) --------------------------


def session_records(records_dir: Path, session_id: str) -> list[dict]:
    """What the library holds for one session: each published record's kind,
    id, and author (report.md rides with report.json and is not listed)."""
    folder = records_dir / session_id
    if not folder.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        if not isinstance(data, dict) or data.get("kind") not in RECORD_KINDS:
            continue
        out.append(
            {
                "file": path.name,
                "kind": data["kind"],
                "id": data.get("id"),
                "title": data.get("title", ""),
                "author": data.get("author") or None,
            }
        )
    return out


def deletion_verdict(
    records_dir: Path,
    session_id: str,
    user_id: str | None,
    admins: list[str],
    solo: bool = False,
) -> dict:
    """Whether `user_id` may remove this session's published records, and why.

    The rule: the records' author, or a library admin. Records with no author
    (published before a user id was set) are an admin's to remove, or git's.
    In solo mode there is no team to protect — the records are the workspace
    owner's own. Declared identity makes this a guard rail against the
    accidental and the casual, not access control (docs/LIBRARY.md).
    """
    records = session_records(records_dir, session_id)
    count = len(records)
    authors = sorted({r["author"] for r in records if r["author"]})
    base = {"count": count, "authors": authors}
    if not records:
        return {**base, "allowed": False, "reason": "nothing is published for this session"}
    if solo:
        return {**base, "allowed": True, "as": "solo workspace — the records are yours"}
    if user_id and user_id in admins:
        return {**base, "allowed": True, "as": "library admin"}
    if not user_id:
        who = f"their author ({', '.join(authors)})" if authors else "a library admin"
        return {
            **base,
            "allowed": False,
            "reason": f"no user id is set (`grayson user set <id>`) — these records are "
            f"{who}'s to remove, or a library admin's",
        }
    if any(not r["author"] for r in records):
        return {
            **base,
            "allowed": False,
            "reason": "some of these records carry no author, so only a library admin "
            "removes them (or a git commit by hand)",
        }
    if authors != [user_id]:
        return {
            **base,
            "allowed": False,
            "reason": f"published by {', '.join(authors)}, not you ({user_id}) — only the "
            "author or a library admin removes a session's records",
        }
    return {**base, "allowed": True, "as": f"author ({user_id})"}


def delete_session_records(workspace: Workspace, session_id: str, reason: str = "") -> dict:
    """Remove a session's published records from the library as one commit.

    A user action (the CLI gates it on an interactive terminal; the console is
    the human's surface; MCP has no twin). The commit carries the reason and
    the actor's trailer, so `git log` answers who removed what and why, and
    `git revert` brings it back. Raises PermissionError with the verdict's
    reason when the caller is neither the author nor an admin.
    """
    from grayson.identity import get_user_id
    from grayson.library import commit_library_paths, library_admins, library_root
    from grayson.util import ensure_within

    records_dir = workspace.records_dir
    solo = workspace.config.library_path is None
    verdict = deletion_verdict(
        records_dir,
        session_id,
        get_user_id(),
        library_admins(library_root(workspace)),
        solo=solo,
    )
    if not verdict["allowed"]:
        raise PermissionError(verdict["reason"])
    folder = ensure_within(records_dir, records_dir / session_id)
    removed = sorted(p.name for p in folder.iterdir())
    shutil.rmtree(folder)
    message = f"grayson records: remove {session_id} ({verdict['count']} record(s))"
    if reason.strip():
        message += f"\n\n{reason.strip()}"
    sync = commit_library_paths(workspace, [f"records/{session_id}"], message)
    return {
        "session_id": session_id,
        "removed": removed,
        "count": verdict["count"],
        "as": verdict["as"],
        "library_sync": sync,
    }


def library_records(records_dir: Path, kind: str | None = None) -> list[dict]:
    """Summary rows for every published record in the library."""
    if kind is not None and kind not in RECORD_KINDS:
        raise ValueError(f"kind must be one of {RECORD_KINDS}")
    out: list[dict] = []
    if not records_dir.is_dir():
        return out
    for path in sorted(records_dir.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict) or data.get("kind") not in RECORD_KINDS:
            continue
        if kind is not None and data["kind"] != kind:
            continue
        out.append(
            {
                **{k: v for k, v in data.items() if k not in ("record", "evidence_queries")},
                "source": "library",
            }
        )
    return out


def search_library_records(
    records_dir: Path, term: str = "", kind: str | None = None, limit: int = 50
) -> list[dict]:
    """Library-only record search (what the knowledge-only server exposes)."""
    rows = library_records(records_dir, kind)
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    if term:
        needle = term.lower()
        rows = [
            r
            for r in rows
            if needle in str(r.get("title", "")).lower()
            or needle in str(r.get("summary", "")).lower()
            or needle in json.dumps(r.get("payload", {}), default=str).lower()
        ]
    return [{k: v for k, v in r.items() if k != "payload"} for r in rows[:limit]]


def get_library_record(records_dir: Path, session_id: str, record_id: str) -> dict | None:
    path = _record_path(records_dir, session_id, record_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict) or data.get("kind") not in RECORD_KINDS:
        return None
    return data


def search_records(
    workspace: Workspace, term: str = "", kind: str | None = None, limit: int = 50
) -> list[dict]:
    """Case-insensitive search over titles, summaries, and full payloads.

    Results carry summaries, not full payloads (use get_record for those) —
    they are list rows for humans and agents scanning for a past problem.
    """
    records = collect_records(workspace, kind)
    if term:
        needle = term.lower()
        records = [
            r
            for r in records
            if needle in r["title"].lower()
            or needle in r["summary"].lower()
            or needle in json.dumps(r["payload"], default=str).lower()
        ]
    return [{k: v for k, v in r.items() if k != "payload"} for r in records[:limit]]


def get_record(workspace: Workspace, session_id: str, kind: str, record_id: str) -> dict | None:
    """One record with its full payload (and verification, for proposals).

    Falls back to the library's published copy when the session is not local —
    a teammate's record is readable from any linked workspace."""
    if kind not in RECORD_KINDS:
        raise ValueError(f"kind must be one of {RECORD_KINDS}")
    item = None
    try:
        s = Session(workspace, session_id)
        item = s.finding(record_id) if kind == "finding" else s.proposal(record_id)
    except (OSError, ValueError, FileNotFoundError):
        pass
    if item is not None:
        # local: the evidence is a query away, so snapshot it live — the same
        # shape a library copy carries, whichever way the record is reached
        if kind == "finding":
            cited = (item.get("payload") or {}).get("evidence") or []
        else:
            verification = item.get("verification") or {}
            cited = [verification.get("before_qid"), verification.get("after_qid")]
        return {
            "session_id": session_id,
            "kind": kind,
            "record": item,
            "source": "session",
            "evidence_queries": evidence_snapshot(s, cited),
        }
    published = get_library_record(workspace.records_dir, session_id, record_id)
    if published is None or published.get("kind") != kind:
        return None
    return {
        "session_id": session_id,
        "kind": kind,
        "record": published["record"],
        "source": "library",
        "session_title": published.get("session_title", ""),
        "author": published.get("author"),
        "evidence_queries": published.get("evidence_queries") or [],
    }
