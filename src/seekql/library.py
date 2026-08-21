"""Team library repo: scaffolding, freshness checks, and extraction.

Collaboration rides on git (docs/SPEC.md s11a). The tool is installed per user;
the compounding assets (knowledge/, views/, workflows/) live in a separate team
library repo that each workspace links via [library] in seekql.toml.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from seekql.workspace import Workspace

LIBRARY_ASSETS = ("knowledge", "views", "workflows")


def init_library(path: Path) -> Path:
    """Scaffold a fresh team library repo (empty asset dirs + README)."""
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    (path / "knowledge").mkdir(exist_ok=True)
    (path / "views" / "ddl").mkdir(parents=True, exist_ok=True)
    (path / "workflows").mkdir(exist_ok=True)
    registry = path / "views" / "registry.yaml"
    if not registry.exists():
        registry.write_text("views: []\n", encoding="utf-8")
    glossary = path / "knowledge" / "glossary.md"
    if not glossary.exists():
        glossary.write_text("# Glossary\n\nShared team definitions.\n", encoding="utf-8")
    readme = path / "README.md"
    if not readme.exists():
        readme.write_text(
            "# seekql team library\n\n"
            "Shared knowledge, QA views, and workflow templates for seekql.\n"
            "Link a workspace to a local clone of this repo via `[library] path` "
            "in its `seekql.toml`.\n",
            encoding="utf-8",
        )
    return path


def _git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def library_status(workspace: Workspace) -> dict:
    """Report whether the linked library clone is behind its remote / dirty."""
    lib = workspace.config.library_path
    if lib is None:
        return {"linked": False, "detail": "no [library] path configured (solo mode)"}
    if not lib.is_dir():
        return {
            "linked": True,
            "exists": False,
            "path": str(lib),
            "detail": "configured library path does not exist",
        }
    if not (lib / ".git").exists():
        return {
            "linked": True,
            "exists": True,
            "is_git": False,
            "path": str(lib),
            "detail": "library path is not a git repo (local-only library)",
        }
    import contextlib

    dirty = bool(_git(lib, "status", "--porcelain").stdout.strip())
    fetch = _git(lib, "fetch", "--quiet", timeout=60)
    behind = ahead = 0
    counts = _git(lib, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    if counts.returncode == 0 and counts.stdout.strip():
        with contextlib.suppress(ValueError):
            ahead, behind = (int(x) for x in counts.stdout.split())
    return {
        "linked": True,
        "exists": True,
        "is_git": True,
        "path": str(lib),
        "dirty": dirty,
        "behind": behind,
        "ahead": ahead,
        "fetch_ok": fetch.returncode == 0,
        "warning": (
            f"library is {behind} commit(s) behind origin — pull before starting"
            if behind
            else None
        ),
    }


def library_pull(workspace: Workspace) -> dict:
    lib = workspace.config.library_path
    if lib is None or not (lib / ".git").exists():
        return {"ok": False, "detail": "no linked git library to pull"}
    result = _git(lib, "pull", "--ff-only", timeout=120)
    return {"ok": result.returncode == 0, "output": (result.stdout + result.stderr).strip()[:2000]}


def extract_library(workspace: Workspace, dest: Path) -> dict:
    """Split a solo workspace's assets out into a new library repo."""
    dest = dest.resolve()
    init_library(dest)
    copied, skipped = [], []
    for asset in LIBRARY_ASSETS:
        src = workspace.root / asset
        if not src.is_dir():
            continue
        for item in src.rglob("*"):
            # Never dereference symlinks: a link planted under an asset dir could
            # otherwise copy out-of-tree secrets (e.g. ~/.ssh/id_rsa) into a repo
            # the user then pushes to a shared remote.
            if item.is_symlink():
                skipped.append(str(item.relative_to(workspace.root)))
                continue
            if item.is_file():
                rel = item.relative_to(workspace.root)
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target, follow_symlinks=False)
                copied.append(str(rel))
    return {
        "dest": str(dest),
        "copied": copied,
        "skipped_symlinks": skipped,
        "next": "commit/push this repo, then set [library] path in seekql.toml",
    }
