"""`knowledge define`: record where a table is defined, completely.

A definition someone points at by hand — "it's models/orders.sql" — is a bare
path: meaningful on the machine that typed it and to nobody else. A team
library is read by collaborators in other checkouts, by agents with no
checkout at all, and by whoever inherits the table next year. For the pointer
to be usable by all of them it has to answer three questions:

- **who** recorded it — the actor kind (agent|user) and the configured user id,
  the same provenance a fact carries;
- **what** it is — its kind (dbt model, view, job, ...), a fingerprint of its
  text so a later pass can say "changed since", and optionally a dated copy
  beside the doc for the reader with no repo;
- **where** it lives — the repo that owns the file, the commit it was read at,
  and the path *relative to that repo*, not to someone's home directory.

When the path names a file on this machine, everything under "what" and
"where" is observed rather than typed: the containing git repo's remote and
HEAD, the file's hash, and whether the working copy is dirty (the hash then
describes text that is not at `ref`). When it does not, the entry is still
stamped with who and when, and the caller is told the pointer is unresolved.
Nothing here is confirmed: a definition is a pointer plus a dated observation.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from grayson.knowledge.store import DEFINITION_KINDS, KnowledgeStore, text_hash
from grayson.util import utcnow

#: the largest local file worth copying beside a doc; a definition is a
#: model or a DDL, not a data extract
CAPTURE_MAX_BYTES = 512_000

_SCP_REMOTE = re.compile(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^/].*)$")
_URL_REMOTE = re.compile(r"^[a-z+]+://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)$")


def normalize_remote(url: str) -> str:
    """One spelling for a repo, whichever transport the clone used:
    `https://github.com/org/repo.git`, `git@github.com:org/repo.git` and
    `ssh://git@github.com/org/repo` all become `github.com/org/repo`. A path
    remote (a bare repo on a share) is returned as given."""
    url = url.strip()
    m = _URL_REMOTE.match(url) or _SCP_REMOTE.match(url)
    if not m:
        return url
    path = m.group("path").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{m.group('host')}/{path}"


def infer_kind(path: str) -> str | None:
    """A conservative guess at what a defining file is, from where it sits in
    a project: a dbt layout names its models, seeds, and snapshots by
    directory; a .py is a job. Anything else stays unknown rather than wrong."""
    parts = [p.lower() for p in Path(path).parts]
    suffix = Path(path).suffix.lower()
    if "models" in parts and suffix == ".sql":
        return "dbt_model"
    if "snapshots" in parts and suffix == ".sql":
        return "dbt_snapshot"
    if "seeds" in parts or ("data" in parts and suffix == ".csv"):
        return "dbt_seed"
    if suffix == ".py":
        return "job"
    if "views" in parts and suffix == ".sql":
        return "view"
    return None


def _git(cwd: Path, *args: str) -> str | None:
    """stdout of a git command run in `cwd`, or None when git is absent, the
    directory is not a repo, or the command fails — every caller treats an
    unanswered question as 'unknown', never as an error."""
    try:
        out = subprocess.run(  # noqa: S603
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def describe_local_file(path: str, root: Path) -> dict[str, Any]:
    """What can be observed about a defining file on this machine.

    Returns the pieces of a definition entry that describe the file itself
    (`path`, `repo`, `ref`, `branch`, `hash`, `dirty`) plus `resolved`
    (the file exists), `local_path` (where it was read from) and `text`
    (its content, for a capture). `path` comes back relative to the git repo
    that owns the file — the form that means the same thing in every checkout
    — or relative to the workspace root when the file is not under git."""
    given = Path(path).expanduser()
    candidate = given if given.is_absolute() else root / given
    out: dict[str, Any] = {"path": path, "resolved": candidate.is_file()}
    if not out["resolved"]:
        return out
    local = candidate.resolve()
    out["local_path"] = str(local)
    try:
        text = local.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    if text is not None:
        out["text"] = text
        out["hash"] = text_hash(text)
    top = _git(local.parent, "rev-parse", "--show-toplevel")
    if top:
        repo_root = Path(top).resolve()
        rel = local.relative_to(repo_root).as_posix()
        out["path"] = rel
        remote = _git(repo_root, "config", "--get", "remote.origin.url")
        if remote:
            out["repo"] = normalize_remote(remote)
        head = _git(repo_root, "rev-parse", "--short=12", "HEAD")
        if head:
            out["ref"] = head
        branch = _git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch and branch != "HEAD":
            out["branch"] = branch
        status = _git(repo_root, "status", "--porcelain", "--", rel)
        out["dirty"] = bool(status)
    else:
        root = root.resolve()
        if local.is_relative_to(root):
            out["path"] = local.relative_to(root).as_posix()
    return out


def record_definition(
    store: KnowledgeStore,
    table: str,
    path: str,
    root: Path,
    kind: str | None = None,
    repo: str | None = None,
    ref: str | None = None,
    description: str | None = None,
    capture: bool = False,
    by: str = "agent",
) -> dict[str, Any]:
    """Record one definition for `table`, as completely as this machine allows.

    Observed fields (repo, ref, branch, hash, dirty, and the repo-relative
    path) come from the local file when it exists; `kind`, `repo`, `ref` and
    `description` passed explicitly win over what was observed or inferred.
    With `capture`, the file's text is copied beside the doc as a dated
    snapshot so a reader with no checkout can still read the definition.
    Returns the entry as written, the doc's full definitions list, and
    `warnings` — the ways in which the pointer is still short of complete."""
    fqn = store.read(table)["table"]
    if kind is not None and kind not in DEFINITION_KINDS:
        raise ValueError(
            f"unknown definition kind {kind!r} (kinds: {', '.join(sorted(DEFINITION_KINDS))})"
        )
    if not path or not path.strip():
        raise ValueError("a definition needs a path")
    seen = describe_local_file(path.strip(), root)
    entry: dict[str, Any] = {"path": seen["path"]}
    for key in ("repo", "ref", "branch", "hash"):
        if seen.get(key):
            entry[key] = seen[key]
    if seen.get("dirty"):
        entry["dirty"] = True
    if repo:
        entry["repo"] = normalize_remote(repo)
    if ref:
        entry["ref"] = ref.strip()
    resolved_kind = kind or infer_kind(entry["path"])
    if resolved_kind:
        entry["kind"] = resolved_kind
    if description and description.strip():
        entry["description"] = description.strip()[:500]
    warnings: list[str] = []
    captured = False
    if capture:
        text = seen.get("text")
        if text is None:
            warnings.append(
                "not captured: the file is not readable here"
                if seen["resolved"]
                else "not captured: the file does not exist here"
            )
        elif len(text.encode("utf-8")) > CAPTURE_MAX_BYTES:
            warnings.append(f"not captured: the file is larger than {CAPTURE_MAX_BYTES // 1000} kB")
        else:
            where = entry.get("repo") or "an untracked checkout"
            at = f" at {entry['ref']}" if entry.get("ref") else ""
            snap = store.write_snapshot(
                fqn,
                "source",
                text,
                header=f"{fqn}\n{entry['path']} ({where}{at})\n"
                f"captured by grayson knowledge define at {utcnow()} — "
                f"a dated copy{' of a dirty working file' if entry.get('dirty') else ''}; "
                "the repo is the authority",
                name=store.source_snapshot_name(fqn, entry["path"]),
            )
            entry["snapshot"] = snap["snapshot"]
            entry["captured_at"] = snap["captured_at"]
            captured = True
    if not seen["resolved"]:
        warnings.append(
            f"'{path}' is not a file here: recorded as a pointer only, nothing observed"
            + ("" if entry.get("repo") else " — pass --repo and --ref so others can find it")
        )
    if not entry.get("repo"):
        warnings.append(
            "no repo recorded: the path is meaningful only on this machine — pass --repo "
            "(or commit the file to a repo with a remote)"
        )
    if entry.get("dirty"):
        warnings.append(
            f"the working copy differs from {entry.get('ref') or 'HEAD'}: the hash "
            "describes uncommitted text"
        )
    if not entry.get("kind"):
        warnings.append("kind unknown: pass --kind (dbt_model, view, ddl, job, ...)")
    doc = store.upsert_definition(fqn, entry, by=by)
    written = next(d for d in doc["definitions"] if d.get("path") == entry["path"])
    return {
        "table": fqn,
        "definition": written,
        "definitions": doc["definitions"],
        "resolved": bool(seen["resolved"]),
        "captured": captured,
        "warnings": warnings,
    }
