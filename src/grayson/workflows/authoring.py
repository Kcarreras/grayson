"""Creating and editing library workflows, with the ownership rules enforced.

The rules (server-side, not advisory):
- Core templates are canonical — no library file may take a core name.
- A library workflow edits in place only for its author (matching `grayson
  user` id). Anyone else forks: a new file, a new name, their id as
  `created_by`, lineage recorded in `forked_from`.
- A legacy library file with no `created_by` is editable by anyone (there is
  no author to protect) — the first save stamps the editor's id.

The YAML file stays the source of truth; every write here round-trips through
the same WorkflowTemplate validation the registry loads with.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from grayson.workflows.models import WorkflowTemplate
from grayson.workflows.registry import core_names, load_override_report

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

SCAFFOLD = """\
name: {name}
title: {title}
description: >
  Say when to use this workflow — agents pick workflows by this description.
suggested_guard_profile: moderate
setup_inputs:
  - key: target_description
    prompt: What should this session investigate, and why?
    required: true
required_checks:
  - key: first_checkpoint
    title: The first evidence-gated checkpoint
    description: >
      What must be demonstrated, with executed queries, before the session
      can advance. Write the intent down — agents close checkpoints better
      when it is explicit.
    uses_inputs: [target_description]
suggested_checks:
  - key: a_fundamental
    title: Something worth checking where it applies
    description: >
      Suggested checks carry breadth without gating. Put things here that are
      worth doing on most targets but not all — a required check that does not
      apply to the table in front of the agent gets closed hollow, which is
      exactly what the evidence rail exists to prevent. Keep required_checks to
      the handful without which the investigation is meaningless.
findings_schema: standard_v1
"""


class WorkflowAuthoringError(ValueError):
    pass


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise WorkflowAuthoringError(
            "workflow name must be 1-64 lowercase letters, digits or '-' "
            "(starting with a letter or digit), e.g. orders-slim-health"
        )
    return name


def _check_name_free(workflows_dir: Path, name: str) -> None:
    if name in core_names():
        raise WorkflowAuthoringError(
            f"'{name}' is a core workflow — core templates are canonical; pick a new name"
        )
    loaded, problems = load_override_report(workflows_dir)
    if name in loaded or any(p.get("name") == name for p in problems):
        raise WorkflowAuthoringError(f"a library workflow named '{name}' already exists")
    if (workflows_dir / f"{name}.yaml").exists():
        raise WorkflowAuthoringError(f"{name}.yaml already exists in the library")


def _dump(tpl: WorkflowTemplate) -> str:
    """Stable, human-editable YAML: field order matches how people read templates."""
    data = tpl.model_dump()
    ordered = {
        key: data[key]
        for key in (
            "name",
            "title",
            "description",
            "created_by",
            "forked_from",
            "suggested_guard_profile",
            "setup_inputs",
            "required_checks",
            "suggested_checks",
            "findings_schema",
        )
        if data.get(key) not in ("", [], None)
    }
    return yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=88)


def create_workflow(
    workflows_dir: Path,
    name: str,
    fork_of: str | None = None,
    title: str = "",
    user_id: str | None = None,
) -> Path:
    """Scaffold a new library workflow (blank, or forked from an existing one)."""
    _validate_name(name)
    workflows_dir.mkdir(parents=True, exist_ok=True)
    _check_name_free(workflows_dir, name)
    path = workflows_dir / f"{name}.yaml"
    if fork_of:
        from grayson.workflows.registry import get_workflow

        base = get_workflow(fork_of, workflows_dir)  # WorkflowNotFound propagates
        fork_title = title or (f"{base.title} (fork)" if base.title else "")
        tpl = base.model_copy(
            update={
                "name": name,
                "title": fork_title,
                "created_by": user_id or "",
                "forked_from": base.name,
            }
        )
        text = _dump(tpl)
    else:
        text = SCAFFOLD.format(name=name, title=title or name.replace("-", " ").title())
        if user_id:
            text = text.replace(
                "suggested_guard_profile:", f"created_by: {user_id}\nsuggested_guard_profile:", 1
            )
    path.write_text(text, encoding="utf-8")
    return path


def can_edit(tpl: WorkflowTemplate, user_id: str | None) -> bool:
    """In-place edit rights: never for core, author-only when authored."""
    if tpl.name in core_names():
        return False
    if not tpl.created_by:
        return True  # legacy file with no author to protect
    return bool(user_id) and tpl.created_by == user_id


def save_workflow_yaml(
    workflows_dir: Path, name: str, text: str, user_id: str | None
) -> WorkflowTemplate:
    """Validate and write an edited library workflow file, enforcing ownership.

    `name` is the workflow being edited; the YAML's own `name` must match —
    renames go through fork/create so nothing silently claims another slot.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise WorkflowAuthoringError(f"YAML does not parse: {e}") from e
    try:
        tpl = WorkflowTemplate.model_validate(data)
    except ValueError as e:
        raise WorkflowAuthoringError(f"does not validate as a workflow template: {e}") from e
    if tpl.name != name:
        raise WorkflowAuthoringError(
            f"the YAML names '{tpl.name}' but you are editing '{name}' — renames are a "
            "fork (new file), not an edit"
        )
    from grayson.findings.schemas import FINDINGS_SCHEMAS

    if tpl.findings_schema not in FINDINGS_SCHEMAS:
        known = ", ".join(sorted(FINDINGS_SCHEMAS))
        raise WorkflowAuthoringError(
            f"unknown findings_schema '{tpl.findings_schema}' (known: {known})"
        )
    keys = tpl.required_check_keys()
    if len(keys) != len(set(keys)):
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        raise WorkflowAuthoringError(f"duplicate checkpoint keys: {', '.join(dupes)}")
    if name in core_names():
        raise WorkflowAuthoringError(
            f"'{name}' is a core workflow — core templates are canonical; fork it instead"
        )
    path = workflows_dir / f"{name}.yaml"
    if path.exists():
        try:
            existing = WorkflowTemplate.model_validate(
                yaml.safe_load(path.read_text(encoding="utf-8"))
            )
        except (yaml.YAMLError, ValueError):
            existing = None  # a broken file has no enforceable author
        if existing is not None and not can_edit(existing, user_id):
            raise WorkflowAuthoringError(
                f"'{name}' was created by '{existing.created_by}' — fork it under a "
                "new name instead of editing their copy"
            )
    if not tpl.created_by and user_id:
        tpl = tpl.model_copy(update={"created_by": user_id})
    workflows_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(tpl), encoding="utf-8")
    return tpl
