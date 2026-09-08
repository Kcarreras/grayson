"""Plain structured diff rows; templates escape all source text."""

import re

from grayson.core import file_fixes


def diff_rows(diff: str) -> list[dict]:
    rows = []
    old = new = None
    for line in diff.splitlines():
        hunk = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        left = right = None
        if hunk:
            old, new = map(int, hunk.groups())
            kind = "hunk"
        elif old is None or line.startswith("\\"):
            kind = "meta"
        elif line.startswith("-"):
            kind, left = "removed", old
            old += 1
        elif line.startswith("+"):
            kind, right = "added", new
            new += 1
        elif line.startswith(" "):
            kind, left, right = "context", old, new
            old += 1
            new += 1
        else:
            kind = "meta"
        rows.append({"kind": kind, "old": left, "new": right, "text": line})
    return rows


def review_proposal(session, proposal: dict) -> dict:
    p = dict(proposal)
    p["review_digest"] = file_fixes.review_digest(p)
    body = p["payload"]
    if p["kind"] != "file_diff":
        return p
    snapshot = body.get("file_change") or {}
    rows = diff_rows(body.get("diff", ""))
    stats = snapshot.get("stats")
    if not stats:
        added = sum(row["kind"] == "added" for row in rows)
        deleted = sum(row["kind"] == "removed" for row in rows)
        after = len(body.get("new_content", "").splitlines())
        stats = {"added": added, "deleted": deleted}
        if snapshot:
            stats.update(before_lines=after - added + deleted, after_lines=after)
    error = None
    if snapshot and p["status"] in {"proposed", "approved"}:
        try:
            file_fixes.check_source(session, p)
        except (ValueError, OSError) as e:
            error = str(e)
    p["file_review"] = {"rows": rows, "stats": stats, "error": error}
    return p
