"""Workflow template discovery: built-in templates + library extensions.

Core (built-in) templates are canonical: a library file whose `name` collides
with a core template is rejected, never merged — core behavior changes only
with a grayson release. Library files that fail to parse or validate are
rejected loudly (surfaced as problems) instead of silently skipped, so a
broken workflow shows up as broken rather than vanishing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from grayson.findings.library import known_schema, known_schema_names, schemas_dir_beside
from grayson.workflows.models import WorkflowTemplate

BUILTIN_DIR = Path(__file__).parent / "templates"


class WorkflowNotFound(KeyError):
    pass


# Parsed templates cached by (path, mtime) so repeated lookups (each readiness
# call, each dashboard render) do not re-read and re-parse unchanged YAML.
_file_cache: dict[Path, tuple[float, WorkflowTemplate]] = {}


def _load_file(path: Path) -> WorkflowTemplate:
    mtime = path.stat().st_mtime
    cached = _file_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    tpl = WorkflowTemplate.model_validate(data)
    _file_cache[path] = (mtime, tpl)
    return tpl


@lru_cache(maxsize=1)
def _builtin() -> dict[str, WorkflowTemplate]:
    out: dict[str, WorkflowTemplate] = {}
    for path in sorted(BUILTIN_DIR.glob("*.yaml")):
        tpl = _load_file(path)
        out[tpl.name] = tpl
    return out


def load_override_report(
    overrides_dir: Path | None,
) -> tuple[dict[str, WorkflowTemplate], list[dict]]:
    """Load library workflow files, returning (loadable templates, problems).

    A file with a problem is excluded from the loadable set — loadable means
    runnable. Every exclusion is reported, never silent.
    """
    out: dict[str, WorkflowTemplate] = {}
    problems: list[dict] = []

    def problem(path: Path, message: str, name: str | None = None) -> None:
        problems.append({"file": path.name, "name": name, "problem": message})

    if overrides_dir is None or not overrides_dir.is_dir():
        return out, problems
    builtin = _builtin()
    for path in sorted(overrides_dir.glob("*.yaml")):
        try:
            tpl = _load_file(path)
        except yaml.YAMLError as e:
            problem(path, f"YAML does not parse: {e}")
            continue
        except ValueError as e:
            problem(path, f"does not validate as a workflow template: {e}")
            continue
        if tpl.name in builtin:
            problem(
                path,
                f"shadows the core workflow '{tpl.name}' — core templates are "
                "canonical and cannot be overridden from the library; fork it "
                "under a new name instead",
                tpl.name,
            )
            continue
        if tpl.name in out:
            problem(
                path,
                f"duplicate workflow name '{tpl.name}' (already defined by another library file)",
                tpl.name,
            )
            continue
        if not known_schema(tpl.findings_schema, schemas_dir_beside(overrides_dir)):
            known = ", ".join(known_schema_names(schemas_dir_beside(overrides_dir)))
            problem(
                path,
                f"unknown findings_schema '{tpl.findings_schema}' (known: {known})",
                tpl.name,
            )
            continue
        keys = tpl.required_check_keys()
        if len(keys) != len(set(keys)):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            problem(path, f"duplicate checkpoint keys: {', '.join(dupes)}", tpl.name)
            continue
        out[tpl.name] = tpl
    return out, problems


def core_names() -> set[str]:
    """Names of the canonical built-in templates (unshadowable)."""
    return set(_builtin())


def override_problems(overrides_dir: Path | None) -> list[dict]:
    """The library workflow files that could not be loaded, and why."""
    return load_override_report(overrides_dir)[1]


def list_workflows(overrides_dir: Path | None = None) -> list[WorkflowTemplate]:
    merged = dict(_builtin())
    overrides, _ = load_override_report(overrides_dir)
    merged.update(overrides)
    return [merged[name] for name in sorted(merged)]


def get_workflow(name: str, overrides_dir: Path | None = None) -> WorkflowTemplate:
    builtin = _builtin()
    if name in builtin:
        return builtin[name]
    overrides, problems = load_override_report(overrides_dir)
    if name in overrides:
        return overrides[name]
    # A library file for this name exists but was rejected: say why, rather
    # than a bare "unknown workflow".
    for p in problems:
        if p.get("name") == name:
            raise WorkflowNotFound(
                f"workflow '{name}' exists in the library but is not loadable: "
                f"{p['problem']} (file: {p['file']}; run `grayson workflow lint`)"
            )
    known = ", ".join(sorted({*builtin, *overrides}))
    raise WorkflowNotFound(f"unknown workflow '{name}' (known: {known})")
