"""Workflow library lint: everything that would make a shared YAML file break
or degrade a session, reported per file.

Errors are the conditions under which the registry refuses to load a file
(so a session cannot start from it); warnings are quality issues the registry
tolerates but a teammate — or an agent choosing a workflow by description —
will feel.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from grayson.workflows.models import WorkflowTemplate
from grayson.workflows.registry import load_override_report


def lint_workflows(overrides_dir: Path | None) -> dict:
    """Lint every library workflow file. Returns {ok, checked, errors, warnings}."""
    loaded, problems = load_override_report(overrides_dir)
    errors = [{**p, "level": "error"} for p in problems]
    error_files = {e["file"] for e in errors}
    warnings: list[dict] = []

    def warn(file: str, name: str | None, message: str) -> None:
        warnings.append({"file": file, "name": name, "problem": message, "level": "warning"})

    checked: list[str] = []
    if overrides_dir is not None and overrides_dir.is_dir():
        checked = sorted(p.name for p in overrides_dir.glob("*.yaml"))
    by_name = {tpl.name: tpl for tpl in loaded.values()}
    for file in checked:
        if file in error_files:  # unloadable — already reported
            continue
        tpl = _template_for_file(overrides_dir, file, by_name)  # type: ignore[arg-type]
        if tpl is None:
            continue
        if tpl.name != Path(file).stem:
            warn(
                file,
                tpl.name,
                f"file name '{file}' does not match workflow name '{tpl.name}' — "
                "the registry keys on the name, but matching them keeps the "
                "library greppable",
            )
        if not tpl.description.strip():
            warn(
                file,
                tpl.name,
                "no description — agents pick workflows by description; say when to use this one",
            )
        if not tpl.required_checks:
            warn(
                file,
                tpl.name,
                "no required_checks — sessions will have no evidence-gated "
                "checkpoints, so nothing enforces the investigation's shape",
            )
        for check in tpl.required_checks:
            if not check.description.strip():
                warn(
                    file,
                    tpl.name,
                    f"checkpoint '{check.key}' has no description — agents close "
                    "checkpoints better when the intent is written down",
                )
    return {
        "ok": not errors,
        "checked": checked,
        "errors": errors,
        "warnings": warnings,
    }


def _template_for_file(
    overrides_dir: Path, file: str, by_name: dict[str, WorkflowTemplate]
) -> WorkflowTemplate | None:
    """Map a loadable file back to its loaded template."""
    try:
        data = yaml.safe_load((overrides_dir / file).read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return None
    name = data.get("name") if isinstance(data, dict) else None
    return by_name.get(name)
