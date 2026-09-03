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


# -- delete ------------------------------------------------------------------


def test_delete_own_workflow(workspace):
    from grayson.workflows.authoring import delete_workflow

    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    path = delete_workflow(workspace.workflows_dir, "mine", "kcg")
    assert not path.exists()


def test_delete_rules(workspace):
    from grayson.workflows.authoring import delete_workflow

    with pytest.raises(WorkflowAuthoringError, match="cannot be deleted"):
        delete_workflow(workspace.workflows_dir, "bug-hunter", "kcg")
    with pytest.raises(WorkflowAuthoringError, match="no library file"):
        delete_workflow(workspace.workflows_dir, "ghost", "kcg")
    create_workflow(workspace.workflows_dir, "theirs", user_id="mkoval2")
    with pytest.raises(WorkflowAuthoringError, match="only its author"):
        delete_workflow(workspace.workflows_dir, "theirs", "kcg")
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    with pytest.raises(WorkflowAuthoringError, match="open session"):
        delete_workflow(workspace.workflows_dir, "mine", "kcg", open_sessions=["s_0001"])
    assert (workspace.workflows_dir / "mine.yaml").exists()
    # a file that no longer parses has no author to protect
    (workspace.workflows_dir / "broken.yaml").write_text("name: [", encoding="utf-8")
    delete_workflow(workspace.workflows_dir, "broken", "kcg")
    assert not (workspace.workflows_dir / "broken.yaml").exists()


def test_open_sessions_on(workspace):
    from grayson.core.session import Session
    from grayson.workflows.authoring import open_sessions_on

    create_workflow(workspace.workflows_dir, "mine", fork_of="table-health", user_id="kcg")
    assert open_sessions_on(workspace, "mine") == []
    s = Session.create(
        workspace,
        workflow="mine",
        targets=["DB.S.T"],
        guard=workspace.config.guard_profiles["moderate"].model_copy(),
        guard_profile="moderate",
    )
    assert open_sessions_on(workspace, "mine") == [s.id]
    assert open_sessions_on(workspace, "table-health") == []


# -- element edits -------------------------------------------------------------


def _bh():
    return get_workflow("bug-hunter")


def test_element_check_upsert_and_rename():
    from grayson.workflows.authoring import apply_element_edit

    tpl = apply_element_edit(
        _bh(),
        {
            "kind": "check",
            "list": "required",
            "action": "upsert",
            "orig_key": "upstream_trace",
            "key": "trace_upstream",
            "title": "Trace",
            "description": "d",
            "depends_on": "replicate_anomaly, validate_expectation",
            "uses_inputs": "",
            "charts": "bar|line: where it starts",
        },
    )
    keys = tpl.required_check_keys()
    assert "trace_upstream" in keys and "upstream_trace" not in keys
    assert keys.index("trace_upstream") == _bh().required_check_keys().index("upstream_trace")
    check = tpl.check("trace_upstream")
    assert check.depends_on == ["replicate_anomaly", "validate_expectation"]
    assert check.uses_inputs == []
    assert [c.kinds for c in check.charts] == [["bar", "line"]]


def test_element_check_add_move_delete_and_cross_list():
    from grayson.workflows.authoring import apply_element_edit

    tpl = apply_element_edit(
        _bh(), {"kind": "check", "list": "suggested", "key": "extra_one", "title": "Extra"}
    )
    assert tpl.suggested_check_keys()[-1] == "extra_one"
    tpl = apply_element_edit(
        tpl,
        {
            "kind": "check",
            "list": "suggested",
            "action": "move",
            "key": "extra_one",
            "direction": "up",
        },
    )
    assert tpl.suggested_check_keys()[-2] == "extra_one"
    tpl = apply_element_edit(
        tpl,
        {
            "kind": "check",
            "list": "suggested",
            "action": "move",
            "key": "extra_one",
            "to_list": "required",
        },
    )
    assert tpl.required_check_keys()[-1] == "extra_one"
    tpl = apply_element_edit(
        tpl, {"kind": "check", "list": "required", "action": "delete", "key": "extra_one"}
    )
    assert "extra_one" not in tpl.required_check_keys() + tpl.suggested_check_keys()


def test_element_check_refuses_collisions_and_bad_keys():
    from grayson.workflows.authoring import apply_element_edit

    with pytest.raises(WorkflowAuthoringError, match="already exists"):
        apply_element_edit(
            _bh(), {"kind": "check", "list": "required", "key": "replicate_anomaly", "title": "x"}
        )
    with pytest.raises(WorkflowAuthoringError, match="one or the other"):
        apply_element_edit(
            _bh(), {"kind": "check", "list": "suggested", "key": "replicate_anomaly", "title": "x"}
        )
    with pytest.raises(WorkflowAuthoringError, match="key must be"):
        apply_element_edit(
            _bh(), {"kind": "check", "list": "required", "key": "Bad Key", "title": "x"}
        )
    with pytest.raises(WorkflowAuthoringError, match="needs a title"):
        apply_element_edit(_bh(), {"kind": "check", "list": "required", "key": "fine", "title": ""})
    with pytest.raises(WorkflowAuthoringError, match="no checkpoint 'nope'"):
        apply_element_edit(
            _bh(), {"kind": "check", "list": "required", "action": "delete", "key": "nope"}
        )


def test_element_input_and_field_and_meta():
    from grayson.workflows.authoring import apply_element_edit

    tpl = apply_element_edit(
        _bh(),
        {
            "kind": "input",
            "key": "threshold",
            "prompt": "How much is too much?",
            "required": False,
            "adds_scope": True,
        },
    )
    new = tpl.setup_inputs[-1]
    assert (new.key, new.required, new.adds_scope) == ("threshold", False, True)
    with pytest.raises(WorkflowAuthoringError, match="needs a prompt"):
        apply_element_edit(tpl, {"kind": "input", "key": "x", "prompt": " "})

    tpl = apply_element_edit(
        tpl,
        {
            "kind": "field",
            "key": "owner_team",
            "description": "who fixes",
            "choices": "data | finance",
        },
    )
    assert tpl.findings_fields[0].choices == ["data", "finance"]
    with pytest.raises(WorkflowAuthoringError, match="base field"):
        apply_element_edit(tpl, {"kind": "field", "key": "severity", "description": "x"})

    tpl = apply_element_edit(
        tpl,
        {
            "kind": "meta",
            "title": "Mine",
            "tags": "orders, orders, Finance",
            "suggested_strict_scope": "false",
            "findings_schema": "standard_v1",
        },
    )
    assert (tpl.title, tpl.tags, tpl.suggested_strict_scope) == (
        "Mine",
        ["orders", "finance"],
        False,
    )
    assert tpl.findings_schema == "standard_v1"
    with pytest.raises(WorkflowAuthoringError, match="unknown findings_schema"):
        apply_element_edit(tpl, {"kind": "meta", "findings_schema": "nope_v9"})
    with pytest.raises(WorkflowAuthoringError, match="unknown element kind"):
        apply_element_edit(tpl, {"kind": "banana"})


def test_element_edit_keeps_unknown_fields():
    from grayson.workflows.authoring import apply_element_edit
    from grayson.workflows.models import WorkflowTemplate

    tpl = WorkflowTemplate.model_validate(
        {
            "name": "x",
            "escalation_policy": "page",
            "required_checks": [{"key": "a", "title": "A", "owner_team": "platform"}],
        }
    )
    tpl = apply_element_edit(
        tpl, {"kind": "check", "list": "required", "orig_key": "a", "key": "a", "title": "A2"}
    )
    data = tpl.model_dump()
    assert data["escalation_policy"] == "page"
    assert data["required_checks"][0]["owner_team"] == "platform"
    assert data["required_checks"][0]["title"] == "A2"


def test_chart_lines_round_trip():
    from grayson.workflows.authoring import format_chart_lines, parse_chart_lines

    parsed = parse_chart_lines(
        "bar|line: onset over time\nany: whatever fits\nno prefix: still prose\n\n"
    )
    assert parsed == [
        {"kinds": ["bar", "line"], "description": "onset over time"},
        {"kinds": [], "description": "whatever fits"},
        {"kinds": [], "description": "no prefix: still prose"},
    ]
    assert (
        format_chart_lines(parsed)
        == "bar|line: onset over time\nany: whatever fits\nany: no prefix: still prose"
    )
    with pytest.raises(WorkflowAuthoringError, match="unknown chart kind"):
        parse_chart_lines("pie: nope")


def test_diff_and_dump_read_like_the_core_templates():
    from grayson.workflows.authoring import _dump, diff_yaml

    text = _dump(_bh())
    assert "description: >-" in text  # prose as folded blocks, not quoted scalars
    assert "\\n" not in text
    assert yaml.safe_load(text)["description"] == _bh().description.strip()
    changed = text.replace("title: Bug Hunter", "title: Mine")
    diff = diff_yaml(text, changed, "bug-hunter")
    assert "-title: Bug Hunter" in diff and "+title: Mine" in diff
    assert diff_yaml(text, text, "bug-hunter") == ""


def test_preview_shows_tags_and_effective_schema():
    from grayson.workflows.authoring import apply_element_edit, render_preview

    tpl = apply_element_edit(_bh(), {"kind": "meta", "tags": "orders"})
    tpl = apply_element_edit(
        tpl,
        {
            "kind": "field",
            "key": "owner_team",
            "description": "who fixes",
            "choices": "a | b",
            "required": False,
        },
    )
    text = render_preview(tpl)
    assert "tags: orders" in text
    assert "plus this workflow's own fields" in text
    assert "blast_radius: Quantified scope" in text  # the schema's own
    assert "owner_team (one of: a | b; optional; this workflow's): who fixes" in text
