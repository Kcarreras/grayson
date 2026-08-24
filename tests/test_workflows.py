from __future__ import annotations

import pytest

from grayson.workflows import WorkflowNotFound, get_workflow, list_workflows

EXPECTED = {
    "bug-hunter",
    "pipeline-qa",
    "table-health",
    "semantic-rule-qa",
    "migration-parity",
}


def test_all_builtins_load():
    names = {t.name for t in list_workflows()}
    assert names >= EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_workflow_wellformed(name):
    t = get_workflow(name)
    assert t.required_checks, f"{name} has no checks"
    assert t.setup_inputs
    assert t.findings_schema
    # keys unique
    keys = t.required_check_keys()
    assert len(keys) == len(set(keys))


def test_unknown_workflow_raises():
    with pytest.raises(WorkflowNotFound):
        get_workflow("does-not-exist")


def test_workspace_override(workspace):
    (workspace.workflows_dir / "custom.yaml").write_text(
        "name: custom-check\ntitle: Custom\n"
        "required_checks:\n  - key: only_check\n    title: The one check\n"
        "setup_inputs:\n  - key: x\n    prompt: what?\n",
        encoding="utf-8",
    )
    t = get_workflow("custom-check", workspace.workflows_dir)
    assert t.required_check_keys() == ["only_check"]
    assert "custom-check" in {w.name for w in list_workflows(workspace.workflows_dir)}


def test_override_shadows_builtin(workspace):
    (workspace.workflows_dir / "th.yaml").write_text(
        "name: table-health\ntitle: My Health\nrequired_checks:\n  - key: mine\n    title: mine\n",
        encoding="utf-8",
    )
    t = get_workflow("table-health", workspace.workflows_dir)
    assert t.title == "My Health"
