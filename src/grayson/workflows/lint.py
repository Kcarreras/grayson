"""Workflow library lint: everything that would make a shared YAML file break
or degrade a session, reported per file.

Errors are the conditions under which the registry refuses to load a file
(so a session cannot start from it); warnings are quality issues the registry
tolerates but a teammate — or an agent choosing a workflow by description —
will feel.
"""

from __future__ import annotations

from collections.abc import Callable
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
        _lint_template(tpl, file, warn)
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


def lint_template(tpl: WorkflowTemplate) -> list[str]:
    """Semantic warnings for one template, core or library.

    Core templates are canonical and non-editable, so their quality is grayson's
    to keep: the same rules run over the built-ins in the test suite.
    """
    out: list[str] = []
    _lint_template(tpl, "", lambda _f, _n, message: out.append(message))
    return out


def _lint_template(tpl: WorkflowTemplate, file: str, warn: Callable[..., None]) -> None:
    all_checks = tpl.required_checks + tpl.suggested_checks
    keys = {c.key for c in all_checks}
    for check in all_checks:
        if not check.description.strip():
            warn(
                file,
                tpl.name,
                f"checkpoint '{check.key}' has no description — agents close "
                "checkpoints better when the intent is written down",
            )
        for n, req in enumerate(check.charts, 1):
            if not req.description.strip():
                warn(
                    file,
                    tpl.name,
                    f"checkpoint '{check.key}' requires chart #{n} without saying what it "
                    "should show — a required picture with no intent gets closed with "
                    "whatever chart is to hand",
                )
        for dep in check.depends_on:
            if dep not in keys:
                warn(
                    file,
                    tpl.name,
                    f"checkpoint '{check.key}' depends on '{dep}', which this workflow "
                    "does not define — the dependency can never be satisfied",
                )
            elif dep == check.key:
                warn(file, tpl.name, f"checkpoint '{check.key}' depends on itself")
    dupes = sorted(k for k in keys if [c.key for c in all_checks].count(k) > 1)
    if dupes:
        warn(
            file,
            tpl.name,
            f"checkpoint key(s) {', '.join(dupes)} appear in both required_checks and "
            "suggested_checks — a check is one or the other",
        )
    # A required input that no checkpoint works from is a question asked of the
    # user and then ignored. Checked against declared linkage, not prose: whether
    # a description "mentions" an input is not something a matcher can judge, and
    # a lint that cries wolf is a lint people learn to skip.
    declared = {k for c in all_checks for k in c.uses_inputs}
    input_keys = set(tpl.input_keys())
    for check in all_checks:
        for key in check.uses_inputs:
            if key not in input_keys:
                warn(
                    file,
                    tpl.name,
                    f"checkpoint '{check.key}' declares uses_inputs: '{key}', which is "
                    "not a setup input of this workflow",
                )
    for setup_input in tpl.setup_inputs:
        if setup_input.required and setup_input.key not in declared:
            warn(
                file,
                tpl.name,
                f"required setup input '{setup_input.key}' is not used by any checkpoint "
                "(uses_inputs) — either put it to work or stop asking the user for it",
            )
