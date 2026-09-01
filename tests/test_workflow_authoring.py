"""Workflow authoring: scaffold, fork, and the ownership rules on save."""

from __future__ import annotations

import pytest
import yaml

from grayson.workflows import get_workflow
from grayson.workflows.authoring import (
    WorkflowAuthoringError,
    can_edit,
    create_workflow,
    save_workflow_yaml,
)


def test_scaffold_new_workflow(workspace):
    path = create_workflow(workspace.workflows_dir, "orders-health", user_id="kcg")
    assert path.name == "orders-health.yaml"
    tpl = get_workflow("orders-health", workspace.workflows_dir)
    assert tpl.created_by == "kcg"
    assert tpl.required_checks  # scaffold ships a commented example checkpoint


def test_fork_records_lineage(workspace):
    create_workflow(workspace.workflows_dir, "orders-health", fork_of="table-health", user_id="kcg")
    tpl = get_workflow("orders-health", workspace.workflows_dir)
    assert tpl.forked_from == "table-health"
    assert tpl.created_by == "kcg"
    base = get_workflow("table-health")
    assert tpl.required_check_keys() == base.required_check_keys()
    assert base.forked_from == ""  # the core template is untouched


def test_core_names_refused(workspace):
    with pytest.raises(WorkflowAuthoringError, match="core workflow"):
        create_workflow(workspace.workflows_dir, "bug-hunter")


def test_existing_names_refused(workspace):
    create_workflow(workspace.workflows_dir, "mine")
    with pytest.raises(WorkflowAuthoringError, match="already exists"):
        create_workflow(workspace.workflows_dir, "mine")


@pytest.mark.parametrize("bad", ["", "Has-Caps", "under_score", "-lead", "a b"])
def test_bad_names_refused(workspace, bad):
    with pytest.raises(WorkflowAuthoringError):
        create_workflow(workspace.workflows_dir, bad)


def _yaml_for(name, created_by=""):
    return yaml.safe_dump(
        {
            "name": name,
            "title": "T",
            "description": "d",
            "created_by": created_by,
            "required_checks": [{"key": "one", "title": "One", "description": "d"}],
        },
        sort_keys=False,
    )


def test_save_own_workflow_in_place(workspace):
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    tpl = save_workflow_yaml(workspace.workflows_dir, "mine", _yaml_for("mine", "kcg"), "kcg")
    assert tpl.title == "T"
    assert get_workflow("mine", workspace.workflows_dir).title == "T"


def test_save_someone_elses_workflow_refused(workspace):
    create_workflow(workspace.workflows_dir, "theirs", user_id="mkoval2")
    with pytest.raises(WorkflowAuthoringError, match="fork it under a new name"):
        save_workflow_yaml(workspace.workflows_dir, "theirs", _yaml_for("theirs"), "kcg")


def test_save_legacy_file_stamps_editor(workspace):
    (workspace.workflows_dir / "legacy.yaml").write_text(
        "name: legacy\ntitle: L\nrequired_checks:\n  - key: a\n    title: A\n",
        encoding="utf-8",
    )
    tpl = save_workflow_yaml(workspace.workflows_dir, "legacy", _yaml_for("legacy"), "kcg")
    assert tpl.created_by == "kcg"


def test_save_rename_refused(workspace):
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    with pytest.raises(WorkflowAuthoringError, match="renames are a"):
        save_workflow_yaml(workspace.workflows_dir, "mine", _yaml_for("other", "kcg"), "kcg")


def test_save_core_name_refused(workspace):
    with pytest.raises(WorkflowAuthoringError, match="canonical"):
        save_workflow_yaml(
            workspace.workflows_dir, "bug-hunter", _yaml_for("bug-hunter", "kcg"), "kcg"
        )


def test_save_validates_schema_and_keys(workspace):
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    bad_schema = _yaml_for("mine", "kcg") + "findings_schema: nope_v9\n"
    with pytest.raises(WorkflowAuthoringError, match="unknown findings_schema"):
        save_workflow_yaml(workspace.workflows_dir, "mine", bad_schema, "kcg")
    dup_keys = yaml.safe_dump(
        {
            "name": "mine",
            "created_by": "kcg",
            "required_checks": [{"key": "a", "title": "A"}, {"key": "a", "title": "B"}],
        }
    )
    with pytest.raises(WorkflowAuthoringError, match="duplicate checkpoint keys"):
        save_workflow_yaml(workspace.workflows_dir, "mine", dup_keys, "kcg")


def test_can_edit_matrix(workspace):
    core = get_workflow("bug-hunter")
    assert can_edit(core, "kcg") is False
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    mine = get_workflow("mine", workspace.workflows_dir)
    assert can_edit(mine, "kcg") is True
    assert can_edit(mine, "mkoval2") is False
    assert can_edit(mine, None) is False


def test_preview_renders_the_confirmation_form():
    from grayson.workflows.authoring import render_preview
    from grayson.workflows.registry import get_workflow

    text = render_preview(get_workflow("bug-hunter", None))
    # everything a human needs to sign off: inputs, gates with order and the
    # answers they work from, breadth, and the session shape
    assert "bug-hunter" in text
    assert "Setup inputs" in text and "anomaly_description (required)" in text
    assert "Required checks" in text and "after: replicate_anomaly" in text
    assert "uses: expectation" in text
    assert "Suggested checks" in text and "onset_dating" in text
    assert "Session shape" in text and "bug_hunter_v1" in text


def test_preview_of_a_gateless_workflow_says_so():
    from grayson.workflows.authoring import render_preview
    from grayson.workflows.models import WorkflowTemplate

    text = render_preview(WorkflowTemplate(name="empty", title="Empty"))
    assert "nothing will gate" in text


def test_unknown_fields_round_trip_through_a_save(workspace):
    # The docs/LIBRARY.md round-trip contract for workflow YAML: a field a
    # newer grayson added — top-level or nested in a check — survives this
    # version's edit-and-save instead of being stripped by the rewrite.
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    path = workspace.workflows_dir / "mine.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["escalation_policy"] = "page-the-oncall"
    data["required_checks"][0]["owner_team"] = "data-platform"
    save_workflow_yaml(workspace.workflows_dir, "mine", yaml.safe_dump(data), user_id="kcg")
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["escalation_policy"] == "page-the-oncall"
    assert saved["required_checks"][0]["owner_team"] == "data-platform"
