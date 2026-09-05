"""Inspect and refresh installed instructions without changing harness permissions."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from grayson.harness.generate import HARNESSES, INSTRUCTION_PATHS, plan_harness
from grayson.util import atomic_write_text, ensure_within, unified_diff_text


def _changes(root: Path, files: dict[str, str]) -> dict[str, str]:
    changed = {}
    for rel, after in files.items():
        path = root / rel
        ensure_within(root, path)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"{path}: expected a regular file, not a link or directory")
        before = path.read_text(encoding="utf-8") if path.is_file() else None
        if before != after:
            changed[rel] = after
    return changed


def apply_plan(root: Path, files: dict[str, str]) -> dict:
    """Preflight the whole plan, back up originals, then replace changed files.

    Backups are exact bytes. On an IO failure, restore files already replaced;
    keep the backup and manifest available even if restoration also fails.
    """
    changed = _changes(root, files)
    if not changed:
        return {"changed": [], "backup": None}
    originals = {
        rel: (root / rel).read_bytes() if (root / rel).is_file() else None for rel in changed
    }
    backup_root = ensure_within(root, root / ".grayson" / "harness-backups")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="update-", dir=backup_root))
    for rel, content in originals.items():
        if content is not None:
            target = backup / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    atomic_write_text(
        backup / "manifest.json",
        json.dumps(
            {"changed": list(changed), "created": [r for r, b in originals.items() if b is None]},
            indent=2,
        ),
    )
    replaced = []
    try:
        for rel, after in changed.items():
            # Detect edits made since planning instead of trampling a user's save.
            path = root / rel
            now = path.read_bytes() if path.is_file() else None
            if now != originals[rel]:
                raise OSError(f"{path} changed during update; retry after reviewing it")
            if now and b"\r\n" in now and b"\n" not in now.replace(b"\r\n", b""):
                after = after.replace("\r\n", "\n").replace("\n", "\r\n")
            atomic_write_text(path, after)
            replaced.append(rel)
    except OSError as exc:
        errors = []
        for rel in reversed(replaced):
            try:
                content = originals[rel]
                if content is None:
                    (root / rel).unlink(missing_ok=True)
                else:
                    atomic_write_text(root / rel, content.decode("utf-8"))
            except OSError as rollback_error:
                errors.append(str(rollback_error))
        raise OSError(
            f"harness update failed: {exc}; originals: {backup}; rollback errors: {errors}"
        ) from exc
    return {"changed": list(changed), "backup": str(backup)}


def harness_status(root: Path, harness: str) -> dict:
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness '{harness}' (known: {', '.join(sorted(HARNESSES))})")
    path = root / INSTRUCTION_PATHS[harness]
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    installed = (
        path.is_file()
        if harness == "cursor"
        else any(marker in existing for marker in ("<!-- grayson:", "<!-- seekql:"))
    )
    out = {"harness": harness, "installed": installed}
    if not installed:
        return {**out, "current": False, "next": f"grayson harness init {harness}"}
    # Inspect only our section: a house rule may have its own MCP heading.
    protocol = existing
    if harness != "cursor":
        for mark in ("grayson", "seekql"):
            start, end = f"<!-- {mark}:start -->", f"<!-- {mark}:end -->"
            if start in existing:
                protocol = existing.split(start, 1)[1].split(end, 1)[0]
                break
    with_mcp = "## MCP\n" in protocol
    try:
        files = plan_harness(root, harness, with_mcp)
        # Reference copies are shared across harnesses and are not their input.
        changed = _changes(root, {r: t for r, t in files.items() if not r.startswith(".grayson/")})
    except (ValueError, OSError) as exc:
        return {**out, "current": False, "error": str(exc)}
    return {**out, "current": not changed, "with_mcp": with_mcp, "changed": list(changed)}


def update_harness(root: Path, harness: str, *, apply: bool = False) -> dict:
    status = harness_status(root, harness)
    if not status["installed"]:
        raise ValueError(f"{harness} has no installed grayson instructions — {status['next']}")
    if status.get("error"):
        raise ValueError(status["error"])
    files = plan_harness(root, harness, status["with_mcp"])
    changed = _changes(root, files)
    diffs = {
        rel: unified_diff_text(
            (root / rel).read_text(encoding="utf-8") if (root / rel).is_file() else "",
            text,
            f"a/{rel}",
            f"b/{rel}",
        )
        for rel, text in changed.items()
    }
    out = {"harness": harness, "applied": apply, "changed": list(changed), "diffs": diffs}
    if apply:
        out.update(apply_plan(root, files))
    else:
        out["next"] = f"grayson harness update {harness} --apply"
    return out
