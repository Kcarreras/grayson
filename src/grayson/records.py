"""Cross-session records: findings, proposals, and their verifications as a
searchable archive.

Sessions are the unit of work; records are the unit of memory. This module
lets humans (console Records tab) and agents (CLI `records search`, MCP
`records_search`) find past problems and fixes without knowing which session
they happened in — "how did we fix the promo fan-out last quarter?".
"""

from __future__ import annotations

import json

from grayson.core.session import Session
from grayson.workspace import Workspace

RECORD_KINDS = ("finding", "proposal")


def collect_records(workspace: Workspace, kind: str | None = None) -> list[dict]:
    """All findings and proposals across sessions, newest session first."""
    if kind is not None and kind not in RECORD_KINDS:
        raise ValueError(f"kind must be one of {RECORD_KINDS}")
    out: list[dict] = []
    for sid in reversed(workspace.list_session_ids()):
        try:
            s = Session(workspace, sid)
            meta = s.meta_all()
        except (OSError, ValueError):
            continue
        base = {
            "session_id": sid,
            "session_title": meta.get("title", ""),
            "workflow": meta.get("workflow", ""),
            "stage": meta.get("stage", ""),
        }
        if kind in (None, "finding"):
            for f in s.findings():
                out.append(
                    {
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
                )
        if kind in (None, "proposal"):
            for p in s.proposals():
                verification = p.get("verification") or {}
                out.append(
                    {
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
                )
    return out


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
    """One record with its full payload (and verification, for proposals)."""
    if kind not in RECORD_KINDS:
        raise ValueError(f"kind must be one of {RECORD_KINDS}")
    try:
        s = Session(workspace, session_id)
    except (OSError, ValueError, FileNotFoundError):
        return None
    item = s.finding(record_id) if kind == "finding" else s.proposal(record_id)
    if item is None:
        return None
    return {"session_id": session_id, "kind": kind, "record": item}
