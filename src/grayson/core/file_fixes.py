"""Draft local fixes without editing source; apply only the reviewed bytes.

The Cursor guard blocks direct writes while investigations are open. This is
the corresponding writer behind Grayson's MCP interface, not an OS sandbox.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path

from grayson.core.proposals import ProposalError
from grayson.core.session import Session
from grayson.util import utcnow

_CONTROL_PATHS = {
    ".git",
    ".grayson",
    ".seekql",
    ".cursor",
    ".claude",
    ".vscode",
    ".snowflake",
    ".snowsql",
    "grayson.toml",
    "seekql.toml",
    "agents.md",
    "claude.md",
}


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _review_diff(before: str, after: str, relative: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relative}",
        tofile=f"b/{relative}",
    )
    # Keep removed/added lines distinct when either file has no final newline.
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
    )


def _target(session: Session, relative: str) -> Path:
    # Use one portable path syntax; reject Windows drives/ADS even on POSIX.
    parts = relative.replace("\\", "/").split("/")
    if not relative or ":" in relative or any(p in {"", ".", ".."} for p in parts):
        raise ProposalError("target_file must be a relative path inside the workspace")
    if any(p.lower() in _CONTROL_PATHS for p in parts):
        raise ProposalError("file fixes cannot change Grayson state or harness controls")
    root = session.workspace.root.resolve()
    path = root
    for part in parts:
        path /= part
        if path.is_symlink() or path.is_junction():
            raise ProposalError("file fixes cannot follow symlinks or junctions")
    if not path.resolve().is_relative_to(root) or not path.parent.is_dir():
        raise ProposalError("target_file must have an existing parent inside the workspace")
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ProposalError("target_file must be a regular file without hard links")
    return path


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def change_stats(before: str, after: str) -> dict:
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    added = deleted = 0
    for tag, i, j, k, end in difflib.SequenceMatcher(None, old, new).get_opcodes():
        if tag in {"replace", "delete"}:
            deleted += j - i
        if tag in {"replace", "insert"}:
            added += end - k
    old_bytes, new_bytes = len(before.encode("utf-8")), len(after.encode("utf-8"))
    large_deletion = bool(before) and (
        not after
        or (len(old) >= 20 and len(new) < len(old) / 2)
        or (old_bytes >= 4096 and new_bytes < old_bytes / 2)
    )
    return {
        "before_lines": len(old),
        "after_lines": len(new),
        "before_bytes": old_bytes,
        "after_bytes": new_bytes,
        "added": added,
        "deleted": deleted,
        "large_deletion": large_deletion,
    }


def _check_replacement(before: str, after: str, mode: str = "replacement") -> dict:
    stats = change_stats(before, after)
    if stats["large_deletion"] and mode != "edits":
        raise ProposalError(
            f"replacement appears truncated ({stats['before_lines']} → "
            f"{stats['after_lines']} lines; {stats['before_bytes']} → "
            f"{stats['after_bytes']} bytes). No proposal was created or applied. "
            "Use proposal_draft_edits with exact old_text/new_text edits; "
            "never submit file chunks as separate replacements"
        )
    return stats


def source_snapshot(session: Session, target_file: str) -> dict:
    source = _read(_target(session, target_file))
    if source is None:
        raise ProposalError("target file does not exist; use proposal_draft_file to create it")
    return {
        "target_file": target_file,
        "sha256": _sha(source),
        "bytes": len(source),
        "lines": len(source.decode("utf-8").splitlines()),
    }


def _request(
    session: Session, request: dict, request_id: str | None
) -> tuple[str, str, dict | None]:
    fingerprint = _sha(json.dumps(request, sort_keys=True).encode("utf-8"))
    if request_id is not None and (not request_id.strip() or len(request_id) > 200):
        raise ProposalError("request_id must contain 1–200 characters")
    key = "file_draft_request:" + ("id:" + request_id if request_id else "auto:" + fingerprint)
    saved = session.get_meta(key)
    if saved:
        record = json.loads(saved)
        if record["fingerprint"] != fingerprint:
            raise ProposalError(
                "request_id already used with different inputs; use a new request_id"
            )
        return key, fingerprint, session.proposal(record["pid"])
    return key, fingerprint, None


def _save_draft(session, body, title, finding_fid, worker, key, fingerprint, supersedes):
    # Share the apply lock: an approved revision cannot be superseded mid-write.
    lock = session.workspace.root / ".grayson" / "file-apply.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as e:
        raise ProposalError("another file operation is in progress; retry this draft") from e
    con = None
    try:
        os.close(fd)
        con = session._con()
        con.execute("BEGIN IMMEDIATE")
        saved = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if saved:
            record = json.loads(saved[0])
            if record["fingerprint"] != fingerprint:
                raise ProposalError("request_id already used with different inputs")
            return session.proposal(record["pid"])
        if session.stage == "closed":
            raise ProposalError("cannot draft a fix on a closed session")
        check_source(session, {"payload": body})
        if supersedes:
            previous = session.proposal(supersedes)
            if (
                not previous
                or previous["kind"] != "file_diff"
                or previous["payload"].get("target_file") != body["target_file"]
                or previous["status"] not in {"proposed", "approved"}
            ):
                raise ProposalError(
                    "supersedes must name a pending or approved fix for the same file"
                )
            con.execute(
                "UPDATE proposals SET status='superseded', decided_by=?, decided_at=? WHERE pid=?",
                (worker or "agent", utcnow(), supersedes),
            )
            for prefix in ("file_approval:", "criteria_approval:"):
                con.execute("DELETE FROM meta WHERE key=?", (prefix + supersedes,))
        n = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM proposals").fetchone()[0]
        pid = f"p_{n + 1:03d}"
        con.execute(
            "INSERT INTO proposals(pid, ts, worker, finding_fid, kind, status, title, payload) "
            "VALUES(?, ?, ?, ?, 'file_diff', 'proposed', ?, ?)",
            (pid, utcnow(), worker, finding_fid, title, json.dumps(body)),
        )
        con.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            (key, json.dumps({"pid": pid, "fingerprint": fingerprint})),
        )
        con.commit()
    finally:
        if con is not None:
            con.close()
        lock.unlink(missing_ok=True)
    session.log_event(
        worker or "agent",
        "proposal_added",
        {"pid": pid, "kind": "file_diff", "finding": finding_fid, "supersedes": supersedes},
    )
    return session.proposal(pid)


def review_digest(proposal: dict) -> str:
    from grayson.core.criteria import digest

    return digest({"kind": proposal["kind"], "payload": proposal["payload"]})


def check_source(session: Session, proposal: dict) -> Path:
    body = proposal["payload"]
    snapshot = body.get("file_change") or {}
    if snapshot.get("format") != 1 or snapshot.get("workspace") != str(session.workspace.root):
        raise ProposalError("file fix has no supported source snapshot; draft it again")
    path = _target(session, body["target_file"])
    source = _read(path)
    if (None if source is None else _sha(source)) != snapshot.get("before_sha256"):
        raise ProposalError(
            "source file changed since this fix was drafted; review the unexpected changes "
            "and draft a new proposal. The file has not been overwritten"
        )
    content = body.get("new_content")
    if not isinstance(content, str) or _sha(content.encode("utf-8")) != snapshot.get(
        "after_sha256"
    ):
        raise ProposalError("replacement content changed; draft a new proposal")
    _check_replacement((source or b"").decode("utf-8"), content, snapshot.get("mode"))
    return path


def draft(
    session: Session,
    target_file: str,
    new_content: str | None,
    title: str,
    finding_fid: str | None = None,
    rationale: str = "",
    worker: str | None = None,
    success_criteria: dict | None = None,
    *,
    edits: list[dict] | None = None,
    expected_source_sha256: str | None = None,
    request_id: str | None = None,
    supersedes: str | None = None,
) -> dict:
    """Snapshot the current source and generate the review diff; never write it."""
    if session.stage == "closed":
        raise ProposalError("cannot draft a fix on a closed session")
    if edits is None and not isinstance(new_content, str):
        raise ProposalError("new_content must be the full UTF-8 replacement text")
    if edits is not None and new_content is not None:
        raise ProposalError("supply edits or new_content, never both")
    if finding_fid is not None and session.finding(finding_fid) is None:
        raise ProposalError(f"proposal references unknown finding '{finding_fid}'")
    path = _target(session, target_file)
    key, fingerprint, existing = _request(
        session,
        {
            "target": path.relative_to(session.workspace.root).as_posix(),
            "content": new_content,
            "edits": edits,
            "source": expected_source_sha256,
            "title": title,
            "finding": finding_fid,
            "rationale": rationale,
            "criteria": success_criteria,
            "supersedes": supersedes,
        },
        request_id,
    )
    if existing:
        return existing
    before = _read(path)
    try:
        old_text = (before or b"").decode("utf-8")
    except UnicodeDecodeError as e:
        raise ProposalError("managed file fixes support UTF-8 text files only") from e
    if edits is not None:
        if before is None or not expected_source_sha256 or _sha(before) != expected_source_sha256:
            raise ProposalError("source snapshot mismatch; read the current source and draft again")
        if not isinstance(edits, list) or not edits:
            raise ProposalError("edits must be a nonempty list of old_text/new_text replacements")
        spans = []
        for index, edit in enumerate(edits, 1):
            if (
                not isinstance(edit, dict)
                or set(edit) != {"old_text", "new_text"}
                or not isinstance(edit["old_text"], str)
                or not edit["old_text"]
                or not isinstance(edit["new_text"], str)
            ):
                raise ProposalError(f"edit {index} requires nonempty old_text and string new_text")
            old = edit["old_text"]
            start = old_text.find(old)
            if start < 0 or old_text.find(old, start + 1) >= 0:
                raise ProposalError(f"edit {index}: old_text must match exactly once in the source")
            spans.append((start, start + len(old), edit["new_text"]))
        spans.sort()
        if any(left[1] > right[0] for left, right in zip(spans, spans[1:], strict=False)):
            raise ProposalError("edits overlap; combine them into one exact replacement")
        new_content = old_text
        for start, end, replacement in reversed(spans):
            new_content = new_content[:start] + replacement + new_content[end:]
    after = new_content.encode("utf-8")
    if before == after:
        raise ProposalError("replacement matches the source; there is no fix to propose")
    mode = "edits" if edits is not None else "replacement"
    stats = _check_replacement(old_text, new_content, mode)
    relative = path.relative_to(session.workspace.root).as_posix()
    body = {
        "target_file": relative,
        "new_content": new_content,
        "diff": _review_diff(old_text, new_content, relative),
        "rationale": rationale,
        "file_change": {
            "format": 1,
            "workspace": str(session.workspace.root),
            "before_sha256": _sha(before) if before is not None else None,
            "after_sha256": _sha(after),
            "mode": mode,
            "stats": stats,
        },
    }
    if supersedes:
        body["supersedes"] = supersedes
    if success_criteria is not None:
        from grayson.core.criteria import prepare

        body["success_criteria"] = prepare(session, success_criteria, for_fix=True)
    proposal = _save_draft(session, body, title, finding_fid, worker, key, fingerprint, supersedes)
    pid = proposal["pid"]
    if success_criteria is not None:
        from grayson.core.criteria import request_scope

        request_scope(session, pid)
    return session.proposal(pid)


def confirm_deletions(proposal: dict, acknowledged: bool) -> None:
    stats = (proposal["payload"].get("file_change") or {}).get("stats") or {}
    if stats.get("large_deletion") and not acknowledged:
        raise ProposalError("review and explicitly acknowledge the large deletion before approval")


def approve(
    session: Session,
    pid: str,
    reviewed_digest: str,
    actor: str,
    acknowledge_deletions: bool = False,
) -> dict:
    """Bind the human's review to the entire file proposal, even without criteria."""
    if actor != "user":
        raise ProposalError("approving a file fix is a user action")
    con = session._con()
    try:
        con.execute("BEGIN IMMEDIATE")
        proposal = session.proposal(pid)
        if not proposal or review_digest(proposal) != reviewed_digest:
            raise ProposalError("the file fix changed since review; reload and review again")
        check_source(session, proposal)
        confirm_deletions(proposal, acknowledge_deletions)
        stamp = utcnow()
        changed = con.execute(
            "UPDATE proposals SET status='approved', decided_by=?, decided_at=? "
            "WHERE pid=? AND status='proposed'",
            (actor, stamp, pid),
        )
        if not changed.rowcount:
            raise ProposalError("proposal is no longer pending")
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (f"file_approval:{pid}", reviewed_digest),
        )
        con.commit()
    finally:
        con.close()
    session.log_event(actor, "proposal_approved", {"pid": pid, "file_digest": reviewed_digest})
    return session.proposal(pid)


def apply(session: Session, pid: str, actor: str = "agent") -> dict:
    """Write exactly one approved file. Source drift, replays and races fail closed."""
    from grayson.core import criteria

    lock = session.workspace.root / ".grayson" / "file-apply.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as e:
        raise ProposalError(
            "another file apply holds the workspace lock; ask the user if it persists"
        ) from e
    tmp: Path | None = None
    try:
        os.close(fd)
        proposal = session.proposal(pid)
        if (
            not proposal
            or proposal["kind"] != "file_diff"
            or not proposal["payload"].get("file_change")
        ):
            raise ProposalError(
                "use proposal draft-file (MCP: proposal_draft_file) before applying"
            )
        if session.stage == "closed" or proposal["status"] != "approved":
            raise ProposalError(
                "the file proposal must be approved in an open session before applying"
            )
        contract = criteria.contract(session, pid)
        if contract:
            approved = contract["review_current"]
        else:
            approved = session.get_meta(f"file_approval:{pid}") == review_digest(proposal)
        if not approved:
            raise ProposalError("file fix or success criteria changed since approval")
        path = check_source(session, proposal)
        body = proposal["payload"]
        candidate = path.with_name(f".{path.name}.{secrets.token_hex(16)}")
        # New source files get ordinary creation permissions, filtered by the
        # process umask. Existing files stay private until their mode is copied.
        mode = 0o666 if body["file_change"]["before_sha256"] is None else 0o600
        fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        tmp = candidate  # Only clean up a temporary file we successfully created.
        with os.fdopen(fd, "wb") as f:
            f.write(body["new_content"].encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            tmp.chmod(stat.S_IMODE(path.stat().st_mode))
        # Recheck immediately before replacing: another editor may have saved
        # while the proposal was approved or the temporary file was written.
        check_source(session, proposal)
        session.log_event(
            actor, "file_apply_started", {"pid": pid, "target_file": body["target_file"]}
        )
        if body["file_change"]["before_sha256"] is None:
            # A concurrently created file must not be replaced by a creation.
            os.link(tmp, path)
            tmp.unlink()
            tmp = None
        else:
            os.replace(tmp, path)
            tmp = None
        stamp = utcnow()
        con = session._con()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE proposals SET status='applied' WHERE pid=? AND status='approved'", (pid,)
            )
            con.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (
                    f"file_applied:{pid}",
                    json.dumps({"digest": review_digest(proposal), "at": stamp}),
                ),
            )
            if contract:
                con.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                    (f"criteria_applied:{pid}", stamp),
                )
            con.commit()
        finally:
            con.close()
        session.log_event(
            actor,
            "file_applied",
            {
                "pid": pid,
                "target_file": body["target_file"],
                "sha256": body["file_change"]["after_sha256"],
            },
        )
        return session.proposal(pid)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
