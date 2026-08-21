"""Workflow template discovery: built-in templates + workspace overrides."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from seekql.workflows.models import WorkflowTemplate

BUILTIN_DIR = Path(__file__).parent / "templates"


class WorkflowNotFound(KeyError):
    pass


def _load_file(path: Path) -> WorkflowTemplate:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return WorkflowTemplate.model_validate(data)


@lru_cache(maxsize=1)
def _builtin() -> dict[str, WorkflowTemplate]:
    out: dict[str, WorkflowTemplate] = {}
    for path in sorted(BUILTIN_DIR.glob("*.yaml")):
        tpl = _load_file(path)
        out[tpl.name] = tpl
    return out


def list_workflows(overrides_dir: Path | None = None) -> list[WorkflowTemplate]:
    merged = dict(_builtin())
    for tpl in _load_overrides(overrides_dir).values():
        merged[tpl.name] = tpl
    return [merged[name] for name in sorted(merged)]


def get_workflow(name: str, overrides_dir: Path | None = None) -> WorkflowTemplate:
    overrides = _load_overrides(overrides_dir)
    if name in overrides:
        return overrides[name]
    builtin = _builtin()
    if name in builtin:
        return builtin[name]
    known = ", ".join(sorted({*builtin, *overrides}))
    raise WorkflowNotFound(f"unknown workflow '{name}' (known: {known})")


def _load_overrides(overrides_dir: Path | None) -> dict[str, WorkflowTemplate]:
    if overrides_dir is None or not overrides_dir.is_dir():
        return {}
    out: dict[str, WorkflowTemplate] = {}
    for path in sorted(overrides_dir.glob("*.yaml")):
        try:
            tpl = _load_file(path)
        except (yaml.YAMLError, ValueError):
            continue
        out[tpl.name] = tpl
    return out
