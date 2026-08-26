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
from pathlib import Path

from grayson.core.session import Session
from grayson.workspace import Workspace

RECORD_KINDS = ("finding", "proposal")


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


def _publish(workspace: Workspace, row: dict, record: dict, message: str) -> None:
    """Write one distilled record into the library and auto-push if configured.

    Best-effort by design: publication must never fail the user action
    (acceptance/verification) it rides on.
    """
    from grayson.identity import get_user_id
    from grayson.library import maybe_auto_push
    from grayson.util import utcnow

    try:
        path = _record_path(workspace.records_dir, row["session_id"], row["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            **{k: v for k, v in row.items() if k != "source"},
            "author": get_user_id(),
            "published_at": utcnow(),
            "record": record,
        }
        path.write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
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
            )


def publish_proposal(session: Session, pid: str) -> None:
    """Publish a proposal once its verification is recorded (either verdict —
    'this fix did not work' is team knowledge too)."""
    p = session.proposal(pid)
    if p is None or not p.get("verification"):
        return
    base = _session_base(session.id, session.meta_all())
    _publish(
        session.workspace,
        _proposal_row(base, p),
        p,
        f"grayson records: proposal {pid} ({session.id})",
    )


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
        out.append({**{k: v for k, v in data.items() if k != "record"}, "source": "library"})
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
        return {"session_id": session_id, "kind": kind, "record": item, "source": "session"}
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
    }
