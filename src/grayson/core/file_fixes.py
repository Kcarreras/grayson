"""Draft local fixes without editing source; apply only the reviewed bytes.

The Cursor guard blocks direct writes while investigations are open. This is
the corresponding writer behind Grayson's MCP interface, not an OS sandbox.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import tempfile
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
    return path


def draft(
    session: Session,
    target_file: str,
    new_content: str,
    title: str,
    finding_fid: str | None = None,
    rationale: str = "",
    worker: str | None = None,
    success_criteria: dict | None = None,
) -> dict:
    """Snapshot the current source and generate the review diff; never write it."""
    if session.stage == "closed":
        raise ProposalError("cannot draft a fix on a closed session")
    if not isinstance(new_content, str):
        raise ProposalError("new_content must be the full UTF-8 replacement text")
    if finding_fid is not None and session.finding(finding_fid) is None:
        raise ProposalError(f"proposal references unknown finding '{finding_fid}'")
    path = _target(session, target_file)
    before = _read(path)
    after = new_content.encode("utf-8")
    if before == after:
        raise ProposalError("replacement matches the source; there is no fix to propose")
    try:
        old_text = (before or b"").decode("utf-8")
    except UnicodeDecodeError as e:
        raise ProposalError("managed file fixes support UTF-8 text files only") from e
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
        },
    }
    if success_criteria is not None:
        from grayson.core.criteria import prepare

        body["success_criteria"] = prepare(session, success_criteria, for_fix=True)
    pid = session.add_proposal("file_diff", title, body, finding_fid, worker)
    if success_criteria is not None:
        from grayson.core.criteria import request_scope

        request_scope(session, pid)
    return session.proposal(pid)


def approve(session: Session, pid: str, reviewed_digest: str, actor: str) -> dict:
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
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as f:
            tmp = Path(f.name)
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
