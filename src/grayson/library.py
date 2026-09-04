"""Team library repo: scaffolding, freshness checks, and extraction.

Collaboration rides on git (docs/SPEC.md s11a). The tool is installed per user;
the compounding assets (knowledge/, views/, workflows/) live in a separate team
library repo that each workspace links via [library] in grayson.toml.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from grayson.config import CONFIG_FILENAME
from grayson.knowledge.policy import EffectivePolicy, KnowledgePolicy
from grayson.workspace import Workspace

LIBRARY_ASSETS = (
    "knowledge",
    "views",
    "workflows",
    "findings_schemas",
    "checks",
    "records",
    "reports",
)

#: shared library settings, versioned with the library itself (docs/LIBRARY.md
#: "Admins"). Today it holds one thing: who may remove any published record.
LIBRARY_SETTINGS_FILENAME = "library.toml"

_ADMIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")

#: how recently a `git fetch` must have run for repo_status to reuse it
#: instead of paying another network round-trip
_FETCH_TTL_SECONDS = 60.0

_REMOTE_RE = re.compile(r"^[\w.-]+@[\w.-]+:")  # scp-style git remote (git@host:org/repo)


#: one README per asset directory, so a browser of the repo (GitHub, a file
#: manager, a new teammate) sees what each folder is for without grayson.
#: checks/ gets its own, format-heavy README from grayson.checks.
FOLDER_READMES: dict[str, str] = {
    "knowledge": """\
# knowledge/

What the team knows about tables, as one markdown document per table at
`<db>/<schema>/<table>.md`, plus `glossary.md` for shared definitions.

Each document carries structured base descriptors (grain, columns,
relationships, freshness, owners), dated definition observations, and
free-form facts. Every fact has a status — `proposed`, `data_inferred`, or
`user_confirmed` — and an author. Agents write proposed and data-inferred
facts; only a human confirms one (console table page, or
`grayson knowledge confirm`).

Sessions read this at start, so what one investigation learned briefs the
next. Edit by hand freely; `grayson library doctor` reports anything that
would not parse.
""",
    "views": """\
# views/

The QA view library: reusable, analysis-ready SQL views that agents can query
without ever holding DDL rights.

- `registry.yaml` — one entry per view: name, purpose, source tables, base
  files (where the underlying definition logic lives in your work repos), the
  DDL file, created_at, and the source tables' `last_altered` at creation.
- `ddl/*.sql` — the `CREATE VIEW` statements themselves.

At session start grayson matches registry entries to the session's target
tables: matching views enter the session's query scope automatically (even
under strict scope), and the coverage check reports which targets have no
view yet. Agents *propose* views (`grayson views propose`); a human creates
them in the warehouse and registers them (`grayson views register`), which
is what lands here.
""",
    "workflows": """\
# workflows/

Workflow templates: the investigation types agents can run (`table-health`,
`bug-hunter`, `table-onboarding`, …), each a YAML file naming the
checkpoints a session must clear, the setup inputs to ask the user for, and
a suggested guard profile and scope.

A file here with the same name as a built-in template overrides it; a new
name adds a custom workflow. Forked workflows (`grayson workflow fork`) land
here too, so a team's refinements travel with the library.
""",
    "findings_schemas": """\
# findings_schemas/

The team's own findings schemas: each a YAML file that names a built-in
schema as its `base` and extends it — fields every finding must carry
(`fields`, each with a description, `required`, and an optional closed
`choices` list) and, optionally, a `discriminator` whose value selects a
branch of further fields. A workflow names one with `findings_schema`.
Author-only edits; `grayson schema lint` checks every file.
""",
    "records": """\
# records/

Published session output — the durable, searchable record of what was found
and fixed. Sessions themselves stay local to each workspace (query cache,
live progress, interventions never leave it); what publishes here, at the
human-approved moments, is the distilled result:

- `<session-id>/<record-id>.json` — an accepted finding, or a fix with its
  verification, stamped with author and the queries it cites as evidence.
- `<session-id>/report.md` and `report.json` — the session's full report,
  written when the session closes; `<session-id>/charts/<chart-id>.svg`
  beside it when the report profile says `charts: svg` or `both` (the
  session that drew them stays local, so the pictures travel with the
  report or not at all).

`grayson records search` and the console's Records page read this folder
across everyone's sessions. Records are removed as a unit by their author or
a library admin (`grayson records delete <sid>`), never edited in place.

The `reports/` folder next door does not hold reports; it holds the
*profiles* that decide how reports render.
""",
    "reports": """\
# reports/

Report **profiles**, not reports. Rendered reports live in
`records/<session-id>/report.md`.

Each `*.yaml` here is a presentation preference for session reports: section
order and inclusion, `engineering` or `stakeholder` audience, how charts are
carried (`text` renderings inline, `svg` files embedded as images, or `both`),
a header and footer. `default.yaml` is used when a session closes; pick
another with `grayson session report --profile <name>`.

Report *facts* — checkpoints, findings, evidence, query statistics — are
built deterministically from the session record and are not configurable
from here; a profile only changes how they are laid out.
""",
}


def init_library(path: Path, admins: list[str] | None = None) -> Path:
    """Scaffold a fresh team library repo (empty asset dirs + README).

    `admins` seeds library.toml on a brand-new library only; an existing
    settings file is never rewritten here (linking a teammate's clone must
    not reset who the team's admins are)."""
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    settings = path / LIBRARY_SETTINGS_FILENAME
    if not settings.exists():
        write_library_settings(path, {"admins": list(admins or [])})
    (path / "knowledge").mkdir(exist_ok=True)
    (path / "views" / "ddl").mkdir(parents=True, exist_ok=True)
    (path / "workflows").mkdir(exist_ok=True)
    (path / "findings_schemas").mkdir(exist_ok=True)
    (path / "records").mkdir(exist_ok=True)
    (path / "reports").mkdir(exist_ok=True)
    profile = path / "reports" / "default.yaml"
    if not profile.exists():
        from grayson.report import DEFAULT_PROFILE_YAML

        profile.write_text(DEFAULT_PROFILE_YAML, encoding="utf-8")
    from grayson.checks import scaffold_checks_dir

    scaffold_checks_dir(path / "checks")
    registry = path / "views" / "registry.yaml"
    if not registry.exists():
        registry.write_text("views: []\n", encoding="utf-8")
    glossary = path / "knowledge" / "glossary.md"
    if not glossary.exists():
        glossary.write_text("# Glossary\n\nShared team definitions.\n", encoding="utf-8")
    for folder, text in FOLDER_READMES.items():
        folder_readme = path / folder / "README.md"
        if not folder_readme.exists():
            folder_readme.write_text(text, encoding="utf-8")
    readme = path / "README.md"
    if not readme.exists():
        readme.write_text(LIBRARY_README, encoding="utf-8")
    return path


LIBRARY_README = """\
# grayson team library

Shared knowledge, QA views, workflow templates, external check results,
report profiles, and published session records for grayson. Link a workspace
to a local clone of this repo with `grayson library link <url>` (or
`[library] path` in its `grayson.toml`).

- `knowledge/` — one document per table (descriptors, definitions, facts with
  provenance) plus `glossary.md`. Agents propose; humans confirm.
- `views/` — the QA view library: `registry.yaml` + `ddl/*.sql`. Humans
  register the views agents proposed.
- `workflows/` — workflow templates: overrides of the built-ins and custom
  investigation types.
- `checks/` — external check results as JSON (Airflow, dbt, …), written by
  automation.
- `records/` — published session output: accepted findings, verified fixes,
  and each closed session's `report.md`.
- `reports/` — report *profiles* (`*.yaml`): how reports render. The reports
  themselves are in `records/`.

Each folder has its own README with the format. `grayson library doctor`
checks that everything here still parses.
"""


def library_root(workspace: Workspace) -> Path:
    """Where library assets live: the linked clone, or the workspace in solo mode."""
    return workspace.config.library_path or workspace.root


def read_library_settings(root: Path) -> dict:
    """The library's shared settings; {} when the file is absent. A file that
    does not parse raises ValueError — callers decide whether that is a
    finding (doctor) or fail-closed (no admins)."""
    path = root / LIBRARY_SETTINGS_FILENAME
    if not path.is_file():
        return {}
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise ValueError(f"{path} is not valid TOML: {e}") from e
    section = data.get("library", {})
    return section if isinstance(section, dict) else {}


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def write_library_settings(root: Path, settings: dict) -> Path:
    """Rewrite library.toml's [library] table. Keys this grayson does not know
    survive (a newer one may have written them); only simple values are
    representable, which is all the file is for."""
    from grayson.util import atomic_write_text

    try:
        current = read_library_settings(root)
    except ValueError:
        current = {}
    merged = {**current, **settings}
    lines = [
        "# grayson team library settings — shared through this repo, so a change",
        "# here is a commit the team can see (and, with a CODEOWNERS entry, review).",
        "[library]",
        "# user ids (as set with `grayson user set`) who may remove any session's",
        "# published records; everyone else removes only what they published.",
    ]
    for key, value in merged.items():
        lines.append(f"{key} = {_toml_value(value)}")
    path = root / LIBRARY_SETTINGS_FILENAME
    atomic_write_text(path, "\n".join(lines) + "\n")
    return path


def library_admins(root: Path) -> list[str]:
    """User ids allowed to remove any published record. Fail-closed: a missing
    or unreadable settings file means no admins, never everyone."""
    try:
        raw = read_library_settings(root).get("admins", [])
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    return [str(a) for a in raw if isinstance(a, str) and _ADMIN_ID_RE.match(a)]


def set_library_admins(workspace: Workspace, add: str = "", remove: str = "") -> dict:
    """Change the admins list: one id in or out, committed to the library with
    the actor's trailer when the library is a git repo.

    Guard rail, not access control: the caller must already be an admin —
    unless the list is empty, which is the bootstrap case (a library made
    before admins existed, or linked without naming one). Identity here is
    declared (`grayson user set`), so the real lock is the git host's review
    rules on library.toml; this refuses the accidental and the casual path and
    keeps history honest about who acted."""
    from grayson.identity import get_user_id

    root = library_root(workspace)
    admins = library_admins(root)
    me = get_user_id()
    if not me:
        raise PermissionError("set your user id first (`grayson user set <id>`)")
    if admins and me not in admins:
        raise PermissionError(
            f"only a library admin changes the admins list (current: {', '.join(admins)}); "
            "you are not one — ask one of them, or change library.toml through a "
            "reviewed commit"
        )
    change = ""
    if add:
        if not _ADMIN_ID_RE.match(add):
            raise ValueError("admin id must be 1-32 characters: letters, digits, '-' or '_'")
        if add not in admins:
            admins.append(add)
            change = f"add {add}"
    if remove:
        if remove not in admins:
            raise ValueError(f"{remove!r} is not an admin (current: {', '.join(admins) or 'none'})")
        admins.remove(remove)
        change = f"remove {remove}"
    if not change:
        return {"admins": admins, "changed": False}
    write_library_settings(root, {"admins": admins})
    sync = commit_library_paths(
        workspace, [LIBRARY_SETTINGS_FILENAME], f"grayson library admins: {change}"
    )
    return {"admins": admins, "changed": True, "library_sync": sync}


def library_policy(root: Path) -> tuple[KnowledgePolicy | None, dict]:
    """The team's knowledge policy from library.toml, and a report of it.
    None when the library has not chosen (the team default then applies).
    A settings file that does not parse reads as no policy — the meet falls
    back to the team default, which is the strict side."""
    from grayson.knowledge.policy import PolicyError

    try:
        settings = read_library_settings(root)
    except ValueError as e:
        return None, {"error": str(e)}
    try:
        policy = KnowledgePolicy.from_library_settings(settings)
    except PolicyError as e:
        return None, {"error": f"library.toml knowledge policy: {e}"}
    return policy, {"set": policy is not None}


def effective_policy(workspace: Workspace) -> EffectivePolicy:
    """What governs this workspace: its own [knowledge] policy in solo mode;
    in team mode, the meet of that and the library's (the team default,
    `propose`, when the library has not chosen)."""
    from grayson.knowledge.policy import DEFAULT_TEAM_PRESET, meet

    own = workspace.config.knowledge
    lib = workspace.config.library_path
    if lib is None:
        return meet(own, None)
    policy, _report = library_policy(lib)
    if policy is None:
        return meet(own, KnowledgePolicy.from_preset(DEFAULT_TEAM_PRESET), library_default=True)
    return meet(own, policy)


def set_library_policy(
    workspace: Workspace,
    preset: str | None = None,
    deny: list[str] | None = None,
    allow: list[str] | None = None,
    trust: str | None = None,
    proposed_horizon_days: int | None = None,
) -> dict:
    """Change the team's knowledge policy in library.toml: the preset, the
    actions withheld from agents regardless of preset (`deny`), or actions to
    stop withholding (`allow`). An admin's action, landing as its own commit
    with the actor's trailer — the same guard rail as the admins list, over
    the same declared identity. In solo mode the workspace's own grayson.toml
    is the policy and this refuses, naming it."""
    from grayson.identity import get_user_id
    from grayson.knowledge.policy import ACTIONS, PRESETS, TRUST_LEVELS, PolicyError

    if workspace.config.library_path is None:
        raise PermissionError(
            "no team library is linked — the workspace's own policy is in grayson.toml "
            "([knowledge] policy; `grayson config set knowledge.policy <preset>`)"
        )
    root = library_root(workspace)
    admins = library_admins(root)
    me = get_user_id()
    if not me:
        raise PermissionError("set your user id first (`grayson user set <id>`)")
    if admins and me not in admins:
        raise PermissionError(
            f"only a library admin changes the knowledge policy (current: {', '.join(admins)}); "
            "you are not one — ask one of them, or change library.toml through a "
            "reviewed commit"
        )
    settings = read_library_settings(root)
    changes: dict[str, object] = {}
    if preset is not None:
        if preset not in PRESETS:
            raise ValueError(f"unknown preset {preset!r} (presets: {', '.join(PRESETS)})")
        changes["knowledge_policy"] = preset
    denied = [str(a) for a in settings.get("knowledge_agent_denied") or []]
    for action in deny or []:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r} (actions: {', '.join(ACTIONS)})")
        if action not in denied:
            denied.append(action)
    for action in allow or []:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r} (actions: {', '.join(ACTIONS)})")
        if action in denied:
            denied.remove(action)
    if (deny or allow) and denied != list(settings.get("knowledge_agent_denied") or []):
        changes["knowledge_agent_denied"] = denied
    if trust is not None:
        if trust not in TRUST_LEVELS:
            raise ValueError(f"trust must be one of {', '.join(TRUST_LEVELS)}")
        changes["knowledge_trust"] = trust
    if proposed_horizon_days is not None:
        if proposed_horizon_days < 0:
            raise ValueError("proposed_horizon_days must be 0 or more")
        changes["knowledge_proposed_horizon_days"] = int(proposed_horizon_days)
    if not changes:
        policy, _ = library_policy(root)
        return {"policy": policy.summary() if policy else None, "changed": False}
    try:
        KnowledgePolicy.from_library_settings({**settings, **changes})
    except PolicyError as e:
        raise ValueError(str(e)) from e
    write_library_settings(root, changes)
    policy, _ = library_policy(root)
    label = ", ".join(f"{k.removeprefix('knowledge_')}={v}" for k, v in changes.items())
    sync = commit_library_paths(
        workspace, [LIBRARY_SETTINGS_FILENAME], f"grayson library policy: {label}"
    )
    return {"policy": policy.summary() if policy else None, "changed": True, "library_sync": sync}


def settings_last_change(root: Path) -> dict | None:
    """The last commit that touched library.toml — who changed the admins,
    and when — so an unexpected change shows up instead of sitting quietly."""
    if not (root / ".git").exists():
        return None
    log = _git(root, "log", "-1", "--format=%H%n%an%n%aI%n%B", "--", LIBRARY_SETTINGS_FILENAME)
    if log.returncode != 0 or not log.stdout.strip():
        return None
    commit, author, date, *body = log.stdout.rstrip("\n").split("\n")
    trailer = next(
        (ln.split(":", 1)[1].strip() for ln in body if ln.startswith("Grayson-User:")), None
    )
    return {"commit": commit[:12], "author": author, "date": date, "user_id": trailer}


def _git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def set_library_config(workspace_root: Path, lib_path: Path, auto_push: bool) -> None:
    """Rewrite the [library] section of grayson.toml (other sections untouched)."""
    cfg = workspace_root / CONFIG_FILENAME
    lines = cfg.read_text(encoding="utf-8").splitlines()
    kept, skipping = [], False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("["):
            skipping = stripped == "[library]"
        if not skipping:
            kept.append(ln)
    # forward slashes: backslashes are escape characters in TOML basic strings
    block = [
        "",
        "[library]",
        f'path = "{lib_path.as_posix()}"',
        f"auto_push = {str(auto_push).lower()}",
    ]
    cfg.write_text("\n".join(kept).rstrip() + "\n" + "\n".join(block) + "\n", encoding="utf-8")


def resolve_library_source(source: str, dest: Path | None = None) -> tuple[Path, str, bool]:
    """Resolve a library source to a local directory: clone a git URL (or pull an
    existing clone), or use a local path as-is. Returns (path, action, is_remote)."""
    is_remote = bool("://" in source or source.endswith(".git") or _REMOTE_RE.match(source))
    if is_remote:
        name = source.rstrip("/").split("/")[-1].removesuffix(".git") or "qa-library"
        target = (dest or Path.home() / ".grayson" / "libraries" / name).resolve()
        if (target / ".git").exists():
            _git(target, "pull", "--ff-only", timeout=120)
            action = "updated existing clone"
        else:
            if target.exists() and any(target.iterdir()):
                raise FileExistsError(f"{target} exists and is not a clone of the library")
            target.parent.mkdir(parents=True, exist_ok=True)
            clone = subprocess.run(  # noqa: S603
                ["git", "clone", source, str(target)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if clone.returncode != 0:
                raise RuntimeError(
                    f"git clone failed: {(clone.stderr or clone.stdout).strip()[:500]}"
                )
            action = "cloned"
    else:
        target = Path(source).expanduser().resolve()
        if not target.is_dir():
            raise FileNotFoundError(f"library path does not exist: {target}")
        action = "linked existing directory"
    return target, action, is_remote


def link_library(
    workspace: Workspace,
    source: str,
    dest: Path | None = None,
    auto_push: bool = False,
) -> dict:
    """Connect the workspace to a team library: clone a git URL, or link a local path."""
    target, action, is_remote = resolve_library_source(source, dest)
    init_library(target)  # idempotent: scaffold only what is missing
    set_library_config(workspace.root, target, auto_push)
    result = {
        "library": str(target),
        "action": action,
        "auto_push": auto_push,
        "next": "agents in this workspace now read and write the shared library",
    }
    # A freshly created (empty) team repo: the scaffold we just wrote is the
    # library's first commit — push it so teammates who link next get structure,
    # not an empty clone. Only for clones grayson made (is_remote): a linked local
    # path may carry unrelated uncommitted work that is not ours to commit.
    if (
        is_remote
        and (target / ".git").exists()
        and _git(target, "status", "--porcelain").stdout.strip()
    ):
        _git(target, "add", "-A")
        commit = _git(target, "commit", "-m", "grayson: scaffold library structure")
        push = _git(target, "push", "-u", "origin", "HEAD", timeout=120)
        result["bootstrapped"] = {
            "committed": commit.returncode == 0,
            "pushed": push.returncode == 0,
            "detail": (push.stdout + push.stderr).strip()[-300:],
        }
    return result


def push_library(
    workspace: Workspace, message: str = "grayson: library update", via: str | None = None
) -> dict:
    """Commit and push the linked library repo. Soft-fails with detail on error.

    When a user id is configured (`grayson user set`), every commit message
    carries a Grayson-User trailer — and Grayson-Via when the write came
    through an agent surface — so shared-library history stays attributable
    even from shared machines or generic git identities."""
    lib = workspace.config.library_path
    if lib is None or not (lib / ".git").exists():
        return {"ok": False, "detail": "no linked git library to push"}
    _git(lib, "add", "-A")
    commit = _git(lib, "commit", "-m", _with_trailers(message, via))
    return _push(lib, committed=commit.returncode == 0)


def _with_trailers(message: str, via: str | None = None) -> str:
    from grayson.identity import get_user_id

    trailers = []
    user_id = get_user_id()
    if user_id:
        trailers.append(f"Grayson-User: {user_id}")
    if via:
        trailers.append(f"Grayson-Via: {via}")
    if trailers:
        message = message + "\n\n" + "\n".join(trailers)
    return message


def commit_library_paths(
    workspace: Workspace, paths: list[str], message: str, via: str | None = None
) -> dict:
    """Commit exactly these paths as one library commit, then push if the
    workspace auto-pushes (otherwise `grayson library push` carries it).

    For a human action that deserves its own line in history — removing a
    session's records, changing the admins — rather than riding along with
    whatever else is uncommitted, as `push_library`'s sweep would have it.
    A library that is not a git repo just keeps the files as changed."""
    lib = workspace.config.library_path
    if lib is None or not (lib / ".git").exists():
        return {"ok": True, "committed": False, "detail": "library is not a git repo"}
    _git(lib, "add", "-A", "--", *paths)
    commit = _git(lib, "commit", "-m", _with_trailers(message, via), "--", *paths)
    committed = commit.returncode == 0
    if not committed:
        return {
            "ok": False,
            "committed": False,
            "detail": (commit.stdout + commit.stderr).strip()[-300:],
        }
    if not workspace.config.library_auto_push:
        return {
            "ok": True,
            "committed": True,
            "detail": "committed; `grayson library push` sends it",
        }
    return _push(lib, committed=True)


def _push(lib: Path, committed: bool) -> dict:
    # -u origin HEAD: works on the first push of a fresh clone and thereafter
    push = _git(lib, "push", "-u", "origin", "HEAD", timeout=120)
    result = {
        "ok": push.returncode == 0,
        "committed": committed,
        "detail": (push.stdout + push.stderr).strip()[-500:],
    }
    if push.returncode != 0 and _push_was_rejected(push):
        # The normal concurrent-team case: a teammate pushed first. Rebase our
        # small library commits onto theirs and try once more; on a conflict,
        # abort cleanly — the write is committed locally, nothing is lost.
        rebase = _git(lib, "pull", "--rebase", timeout=120)
        if rebase.returncode == 0:
            push = _git(lib, "push", "-u", "origin", "HEAD", timeout=120)
            result = {
                "ok": push.returncode == 0,
                "committed": committed,
                "rebased": True,
                "detail": (push.stdout + push.stderr).strip()[-500:],
            }
        else:
            _git(lib, "rebase", "--abort")
            result = {
                "ok": False,
                "committed": committed,
                "detail": "push rejected (a teammate pushed first) and the automatic "
                "rebase hit a conflict — the write is committed locally; run "
                "`grayson library pull`, resolve, then `grayson library push`. "
                + (rebase.stdout + rebase.stderr).strip()[-300:],
            }
    return result


def _push_was_rejected(push: subprocess.CompletedProcess) -> bool:
    """A non-fast-forward rejection (someone pushed first), as opposed to a
    network or auth failure a rebase cannot help with."""
    err = (push.stdout + push.stderr).lower()
    return "[rejected]" in err or "non-fast-forward" in err or "fetch first" in err


def maybe_auto_push(workspace: Workspace, message: str, via: str | None = None) -> dict | None:
    """Auto commit+push after a library write, when [library] auto_push is on."""
    try:
        if not workspace.config.library_auto_push:
            return None
        return push_library(workspace, message, via=via)
    except (OSError, subprocess.SubprocessError) as e:  # never fail the write itself
        return {"ok": False, "detail": f"auto-push failed: {e}"}


def repo_status(lib: Path) -> dict:
    """Git freshness of a library directory: dirty/ahead/behind its remote."""
    if not lib.is_dir():
        return {
            "exists": False,
            "path": str(lib),
            "detail": "library path does not exist",
        }
    if not (lib / ".git").exists():
        return {
            "exists": True,
            "is_git": False,
            "path": str(lib),
            "detail": "library path is not a git repo (local-only library)",
        }
    import contextlib
    import time

    dirty = bool(_git(lib, "status", "--porcelain").stdout.strip())
    # Throttle the network round-trip: `library_info` and doctor call this per
    # request, and on a slow enterprise git host every call would pay a fetch.
    # git updates .git/FETCH_HEAD's mtime on each fetch — a recent one stands in.
    fetch_head = lib / ".git" / "FETCH_HEAD"
    fetch_cached = False
    with contextlib.suppress(OSError):
        fetch_cached = time.time() - fetch_head.stat().st_mtime < _FETCH_TTL_SECONDS
    if fetch_cached:
        fetch_ok = True
    else:
        fetch = _git(lib, "fetch", "--quiet", timeout=60)
        fetch_ok = fetch.returncode == 0
    behind = ahead = 0
    counts = _git(lib, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    if counts.returncode == 0 and counts.stdout.strip():
        with contextlib.suppress(ValueError):
            ahead, behind = (int(x) for x in counts.stdout.split())
    return {
        "exists": True,
        "is_git": True,
        "path": str(lib),
        "dirty": dirty,
        "behind": behind,
        "ahead": ahead,
        "fetch_ok": fetch_ok,
        "fetch_cached": fetch_cached,
        "warning": (
            f"library is {behind} commit(s) behind origin — pull before starting"
            if behind
            else None
        ),
    }


def _admins_report(root: Path) -> dict:
    return {"admins": library_admins(root), "admins_changed": settings_last_change(root)}


def _policy_report(root: Path) -> dict:
    policy, report = library_policy(root)
    return {"knowledge_policy": policy.summary() if policy else None, **report}


def library_status(workspace: Workspace) -> dict:
    """Report whether the linked library clone is behind its remote / dirty."""
    lib = workspace.config.library_path
    if lib is None:
        return {"linked": False, "detail": "no [library] path configured (solo mode)"}
    return {"linked": True, **repo_status(lib), **_admins_report(lib), **_policy_report(lib)}


def library_pull_path(lib: Path) -> dict:
    """Fast-forward a library directory from its remote (no-op when not a clone)."""
    if not (lib / ".git").exists():
        return {"ok": False, "detail": "not a git clone — nothing to pull"}
    try:
        result = _git(lib, "pull", "--ff-only", timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "detail": f"pull failed: {e}"}
    return {"ok": result.returncode == 0, "output": (result.stdout + result.stderr).strip()[:2000]}


def library_pull(workspace: Workspace) -> dict:
    lib = workspace.config.library_path
    if lib is None or not (lib / ".git").exists():
        return {"ok": False, "detail": "no linked git library to pull"}
    return library_pull_path(lib)


def _lint_records(records_dir: Path) -> dict:
    """Published records the readers would silently skip: broken JSON, or a
    shape record search does not recognize (library_records drops both without
    a word — fine for serving, wrong for finding out). Also the record a newer
    grayson stamped with a format this one doesn't write — served best-effort
    (fields are additive), but worth knowing an upgrade is due."""
    from grayson.records import RECORD_KINDS, RECORDS_FORMAT

    errors: list[dict] = []
    checked = 0
    if records_dir.is_dir():
        for path in sorted(records_dir.rglob("*.json")):
            checked += 1
            rel = str(path.relative_to(records_dir))
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                errors.append({"file": rel, "problem": f"unreadable JSON: {e}"})
                continue
            if not isinstance(data, dict) or data.get("kind") not in RECORD_KINDS:
                errors.append(
                    {
                        "file": rel,
                        "problem": "not a recognized record shape — record search "
                        "skips this file silently",
                    }
                )
                continue
            fmt = data.get("format", 1)
            if not isinstance(fmt, int) or isinstance(fmt, bool):
                errors.append({"file": rel, "problem": f"format is not an integer: {fmt!r}"})
            elif fmt > RECORDS_FORMAT:
                errors.append(
                    {
                        "file": rel,
                        "problem": f"record format {fmt} is newer than this grayson "
                        f"writes (format {RECORDS_FORMAT}) — served best-effort; "
                        "upgrade grayson to be current",
                    }
                )
    return {"ok": not errors, "checked": checked, "errors": errors}


def library_doctor(workspace: Workspace) -> dict:
    """One read-only health pass over the whole library.

    Knowledge docs against the format contract (parse, stamp, duplicate fact
    ids, moved files), workflow templates through their linter, published
    records through a parse check, and the repo's git freshness. Surfaces on
    demand the drift that hand edits and merges accumulate — it changes
    nothing; fixing is the human's (or `library migrate`'s) job.
    """
    from grayson.knowledge import KnowledgeStore
    from grayson.workflows import lint_workflows

    lib = workspace.config.library_path or workspace.root
    knowledge = KnowledgeStore(workspace.knowledge_dir).lint()
    from grayson.findings.authoring import lint_schemas

    workflows = lint_workflows(workspace.workflows_dir)
    schemas = lint_schemas(workspace.findings_schemas_dir, workspace.workflows_dir)
    records = _lint_records(workspace.records_dir)
    settings = _lint_settings(lib)
    return {
        "library": str(lib),
        "ok": knowledge["ok"]
        and workflows["ok"]
        and schemas["ok"]
        and records["ok"]
        and settings["ok"],
        "knowledge": knowledge,
        "workflows": workflows,
        "schemas": schemas,
        "records": records,
        "settings": settings,
        # informational: standing never fails the doctor — it is the queue, not a fault
        "standing": standing_report(workspace),
        "policy": effective_policy(workspace).summary(),
        "repo": repo_status(lib),
    }


def _lint_settings(root: Path) -> dict:
    """library.toml: parses, admins is a list of well-formed ids, and who last
    changed it — a silent edit to the admins is exactly what this pass is for."""
    errors: list[str] = []
    try:
        raw = read_library_settings(root).get("admins", [])
    except ValueError as e:
        return {"ok": False, "errors": [str(e)], "admins": [], "admins_changed": None}
    if not isinstance(raw, list):
        errors.append("admins must be a list of user ids")
    else:
        bad = [a for a in raw if not (isinstance(a, str) and _ADMIN_ID_RE.match(a))]
        if bad:
            errors.append(f"admins entries are not user ids: {bad}")
    _policy, policy_report = library_policy(root)
    if policy_report.get("error"):
        errors.append(policy_report["error"])
    return {"ok": not errors, "errors": errors, **_admins_report(root), **_policy_report(root)}


def reconcile_root(
    root: Path,
    policy: KnowledgePolicy | EffectivePolicy | None = None,
    dry_run: bool = False,
    push: bool = False,
) -> dict:
    """The reconcile pass over a library directory — a workspace's linked
    clone or a bare checkout in CI. Rules only (knowledge/reconcile.py); the
    result lands as one commit with a `Grayson-Via: reconcile` trailer, on a
    clean tree, and is pushed when asked. With `dry_run` nothing is written."""
    from grayson.knowledge import KnowledgeStore
    from grayson.knowledge.policy import DEFAULT_TEAM_PRESET
    from grayson.knowledge.reconcile import reconcile_docs

    if policy is None:
        policy, _ = library_policy(root)
        if policy is None:
            policy = KnowledgePolicy.from_preset(DEFAULT_TEAM_PRESET)
    is_git = (root / ".git").exists()
    if is_git and not dry_run and _git(root, "status", "--porcelain").stdout.strip():
        raise RuntimeError(
            "library working tree is dirty — commit or stash first, so the reconcile "
            "lands as one revertible commit and nothing else rides along with it"
        )
    report = reconcile_docs(KnowledgeStore(root / "knowledge"), root / "records", policy, dry_run)
    out = {"library": str(root), "is_git": is_git, "policy": policy.summary(), **report}
    if dry_run or not report["touched"]:
        out["committed"] = False
        return out
    if not is_git:
        out["committed"] = False
        out["warning"] = (
            "library is not a git repo, so this pass has no rollback point — "
            "`git init` the library (or `grayson library link`) before the next one"
        )
        return out
    label = (
        f"{len(report['materialized'])} standing change(s), "
        f"{len(report['questions_folded'])} question(s) folded, "
        f"{len(report['questions_retired'])} question(s) retired"
    )
    _git(root, "add", "-A", "--", *report["touched"])
    commit = _git(
        root,
        "commit",
        "-m",
        _with_trailers(f"grayson library reconcile: {label}", via="reconcile"),
        "--",
        *report["touched"],
    )
    out["committed"] = commit.returncode == 0
    if out["committed"] and push:
        out["push"] = _push(root, committed=True)
    return out


def reconcile_library(workspace: Workspace, dry_run: bool = False) -> dict:
    """Reconcile the workspace's library (or, in solo mode, its own knowledge
    directory) under the effective policy; pushes when the workspace auto-pushes."""
    root = workspace.config.library_path or workspace.root
    return reconcile_root(
        root,
        policy=effective_policy(workspace),
        dry_run=dry_run,
        push=bool(workspace.config.library_path) and workspace.config.library_auto_push,
    )


def standing_report(workspace: Workspace) -> dict:
    """What the reconcile pass would do and what it cannot decide — the
    doctor's read-only view of standing across the library."""
    from grayson.knowledge import KnowledgeStore
    from grayson.knowledge.reconcile import reconcile_docs

    report = reconcile_docs(
        KnowledgeStore(workspace.knowledge_dir),
        workspace.records_dir,
        effective_policy(workspace),
        dry_run=True,
    )
    return {
        "counts": report["counts"],
        "would_materialize": len(report["materialized"]),
        "would_fold_questions": len(report["questions_folded"]),
        "would_retire_questions": len(report["questions_retired"]),
        "needs_human": report["needs_human"],
        "agent_actions": report["agent_actions"],
        "hint": (
            "`grayson library reconcile` materializes standing onto the docs as one commit; "
            "needs_human lists what no rule decides — contested pairs, unverified and "
            "stale facts — for the console's Knowledge tab or an agent the policy permits"
        ),
    }


def migrate_library(workspace: Workspace) -> dict:
    """Rewrite the library's knowledge docs to the current format, deliberately.

    The compatibility contract (docs/LIBRARY.md, "Format stability") is that a
    breaking format change never happens implicitly on read — it happens here,
    on a clean git tree, landing as one labeled commit a human can review and
    revert. Today the only rewrite is stamping `format:` on docs written before
    stamping existed; future FORMAT_STEPS run through the same door.
    """
    from grayson.identity import get_user_id
    from grayson.knowledge import KNOWLEDGE_FORMAT, KnowledgeStore

    lib = workspace.config.library_path or workspace.root
    is_git = (lib / ".git").exists()
    if is_git and _git(lib, "status", "--porcelain").stdout.strip():
        raise RuntimeError(
            "library working tree is dirty — commit or stash first, so the migration "
            "lands as one revertible commit and nothing else rides along with it"
        )
    report = KnowledgeStore(workspace.knowledge_dir).migrate()
    out = {"library": str(lib), "is_git": is_git, **report}
    if not is_git:
        out["warning"] = (
            "library is not a git repo, so this rewrite has no rollback point — "
            "`git init` the library (or `grayson library link`) before the next one"
        )
        return out
    if report["migrated"]:
        message = f"grayson library migrate: knowledge format {KNOWLEDGE_FORMAT}"
        user_id = get_user_id()
        if user_id:
            message += f"\n\nGrayson-User: {user_id}"
        _git(lib, "add", "-A")
        commit = _git(lib, "commit", "-m", message)
        out["committed"] = commit.returncode == 0
        if workspace.config.library_auto_push:
            push = _git(lib, "push", "-u", "origin", "HEAD", timeout=120)
            out["pushed"] = push.returncode == 0
    return out


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
        "next": "commit/push this repo, then set [library] path in grayson.toml",
    }
