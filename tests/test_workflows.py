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


def test_core_workflows_are_canonical(workspace):
    """A library file cannot shadow a core template — core changes only with a
    grayson release; the collision is reported, not silently merged."""
    from grayson.workflows import override_problems

    (workspace.workflows_dir / "th.yaml").write_text(
        "name: table-health\ntitle: My Health\nrequired_checks:\n  - key: mine\n    title: mine\n",
        encoding="utf-8",
    )
    t = get_workflow("table-health", workspace.workflows_dir)
    assert t.title != "My Health"  # the core template wins
    problems = override_problems(workspace.workflows_dir)
    assert len(problems) == 1
    assert "shadows the core workflow" in problems[0]["problem"]
    assert "table-health" not in {
        w.name for w in list_workflows(workspace.workflows_dir) if w.title == "My Health"
    }


def test_invalid_yaml_reported_not_silent(workspace):
    from grayson.workflows import override_problems

    (workspace.workflows_dir / "broken.yaml").write_text(
        "name: broken\n  bad indent: [unclosed\n", encoding="utf-8"
    )
    problems = override_problems(workspace.workflows_dir)
    assert len(problems) == 1
    assert problems[0]["file"] == "broken.yaml"
    assert "parse" in problems[0]["problem"]


def test_unloadable_workflow_error_names_the_reason(workspace):
    (workspace.workflows_dir / "custom.yaml").write_text(
        "name: custom\ntitle: C\nfindings_schema: nope_v9\n"
        "required_checks:\n  - key: a\n    title: A\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowNotFound, match="nope_v9"):
        get_workflow("custom", workspace.workflows_dir)


def test_unknown_findings_schema_blocks_load(workspace):
    from grayson.workflows import override_problems

    (workspace.workflows_dir / "custom.yaml").write_text(
        "name: custom\ntitle: C\nfindings_schema: contract_v2\n",
        encoding="utf-8",
    )
    assert "custom" not in {w.name for w in list_workflows(workspace.workflows_dir)}
    [problem] = override_problems(workspace.workflows_dir)
    assert "unknown findings_schema" in problem["problem"]


def test_duplicate_checkpoint_keys_block_load(workspace):
    from grayson.workflows import override_problems

    (workspace.workflows_dir / "dup.yaml").write_text(
        "name: dup\ntitle: D\nrequired_checks:\n"
        "  - key: a\n    title: A\n  - key: a\n    title: A again\n",
        encoding="utf-8",
    )
    [problem] = override_problems(workspace.workflows_dir)
    assert "duplicate checkpoint keys" in problem["problem"]


def test_lint_reports_errors_and_warnings(workspace):
    from grayson.workflows import lint_workflows

    (workspace.workflows_dir / "shadow.yaml").write_text(
        "name: bug-hunter\ntitle: Shadow\n", encoding="utf-8"
    )
    (workspace.workflows_dir / "sparse.yaml").write_text(
        "name: sparse\ntitle: Sparse\nrequired_checks:\n  - key: only\n    title: Only\n",
        encoding="utf-8",
    )
    report = lint_workflows(workspace.workflows_dir)
    assert report["ok"] is False
    assert {e["file"] for e in report["errors"]} == {"shadow.yaml"}
    warned = {w["problem"] for w in report["warnings"] if w["name"] == "sparse"}
    assert any("no description" in w for w in warned)
    assert any("checkpoint 'only' has no description" in w for w in warned)


def test_lint_clean_library_is_ok(workspace):
    from grayson.workflows import lint_workflows

    (workspace.workflows_dir / "good.yaml").write_text(
        "name: good\ntitle: Good\ndescription: A well-described workflow.\n"
        "required_checks:\n  - key: one\n    title: One\n    description: Do the one thing.\n",
        encoding="utf-8",
    )
    report = lint_workflows(workspace.workflows_dir)
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["warnings"] == []
    assert report["checked"] == ["good.yaml"]


# -- core templates are grayson's to keep clean ---------------------------


def test_core_templates_pass_the_same_lint_as_library_ones():
    """Core templates are canonical and non-editable, so a semantic regression in
    one ships with the release. Library files get linted; these never were."""
    from grayson.workflows.lint import lint_template

    problems = {t.name: lint_template(t) for t in list_workflows(None) if lint_template(t)}
    assert not problems, problems


def test_core_templates_declare_their_shape():
    for tpl in list_workflows(None):
        assert tpl.description.strip(), f"{tpl.name} has no description"
        assert tpl.required_checks, f"{tpl.name} has no required checks"
        assert tpl.suggested_checks, f"{tpl.name} offers no suggested breadth"
        for check in tpl.required_checks + tpl.suggested_checks:
            assert check.description.strip(), f"{tpl.name}:{check.key} has no description"


def test_bug_hunter_orders_cause_hunting_after_replication():
    tpl = get_workflow("bug-hunter", None)
    for key in ("scope_blast_radius", "upstream_trace", "rule_out_alternatives"):
        assert tpl.check(key).depends_on == ["replicate_anomaly"], key


def test_table_onboarding_covers_everything_base_complete_requires():
    """The workflow's stated goal state is base_complete; its checkpoints have to
    actually reach it, or agents are asked to guess the rest."""
    tpl = get_workflow("table-onboarding", None)
    prose = " ".join(c.description.lower() for c in tpl.required_checks)
    for field in ("grain", "column", "relationship", "freshness", "definition_files", "owners"):
        assert field.replace("_", " ") in prose or field in prose, field


def test_feature_readiness_gates_on_leakage():
    """The check that most often invalidates a model — it has to be required,
    and its schema field must not be satisfiable by silence."""
    tpl = get_workflow("feature-readiness", None)
    assert "leakage_assessed" in tpl.required_check_keys()
    assert tpl.check("leakage_assessed").depends_on == ["label_profiled", "feature_profiled"]

    from grayson.findings.schemas import FINDINGS_SCHEMAS

    required = dict(FINDINGS_SCHEMAS["feature_readiness_v1"]["required_extra"])
    assert "leakage_assessment" in required
    assert "readiness_verdict" in required


def test_feature_readiness_is_distinct_from_table_health():
    """Split by decision, not technique: if these two overlap on checkpoints,
    one of them is a junk drawer."""
    fr = set(get_workflow("feature-readiness", None).required_check_keys())
    th = set(get_workflow("table-health", None).required_check_keys())
    assert not (fr & th)
