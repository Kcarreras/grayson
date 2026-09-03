"""Library findings schemas: named, owned, shareable extensions of the
built-ins — model rules, registry, layered resolution, authoring, lint,
promotion, and the CLI."""

from __future__ import annotations

import json

import pytest
import yaml

from grayson.findings.authoring import (
    SchemaAuthoringError,
    apply_schema_edit,
    can_edit_schema,
    create_schema,
    delete_schema,
    lint_schema,
    lint_schemas,
    render_schema_preview,
    save_schema,
    save_schema_yaml,
    workflows_using,
)
from grayson.findings.library import (
    LibrarySchema,
    SchemaNotFound,
    get_library_schema,
    known_schema,
    list_library_schemas,
    schema_problems,
    schemas_dir_beside,
)
from grayson.findings.schemas import describe_schema, effective_extra, validate_finding
from grayson.workflows import get_workflow
from grayson.workflows.authoring import (
    WorkflowAuthoringError,
    apply_element_edit,
    create_workflow,
    plan_promotion,
    promote_fields,
    save_workflow_yaml,
)
from grayson.workflows.authoring import _dump as dump_workflow


def _finding(**over):
    payload = {
        "title": "Duplicate keys in output",
        "severity": "low",
        "confidence": "low",
        "summary": "The output table has duplicate primary keys.",
        "evidence": ["q_0001"],
    }
    payload.update(over)
    return payload


def _triage(schemas_dir, user="kcg"):
    """A schema with a field, a choice list, a discriminator and two branches."""
    create_schema(schemas_dir, "orders_triage_v1", user_id=user, title="Orders triage")
    sc = get_library_schema("orders_triage_v1", schemas_dir)
    sc = apply_schema_edit(sc, {"kind": "meta", "description": "Findings on orders."})
    sc = apply_schema_edit(
        sc,
        {
            "kind": "field",
            "key": "outcome",
            "description": "What happened.",
            "choices": "fixed | deferred",
        },
    )
    sc = apply_schema_edit(sc, {"kind": "discriminator", "key": "outcome"})
    sc = apply_schema_edit(
        sc, {"kind": "field", "branch": "fixed", "key": "fix_ref", "description": "The change."}
    )
    return save_schema(schemas_dir, sc, user)


# -- model ---------------------------------------------------------------------


def test_schema_extends_a_builtin_only():
    with pytest.raises(ValueError, match="base must be a built-in"):
        LibrarySchema(name="x_v1", base="orders_triage_v1")
    with pytest.raises(ValueError, match="schema name"):
        LibrarySchema(name="Has-Caps")


def test_discriminator_rules():
    fields = [{"key": "outcome", "choices": ["a", "b"]}]
    assert LibrarySchema(name="x_v1", fields=fields, discriminator="outcome").branch_values() == [
        "a",
        "b",
    ]
    with pytest.raises(ValueError, match="not one of this schema's fields"):
        LibrarySchema(name="x_v1", fields=fields, discriminator="nope")
    with pytest.raises(ValueError, match="needs `choices`"):
        LibrarySchema(name="x_v1", fields=[{"key": "outcome"}], discriminator="outcome")
    with pytest.raises(ValueError, match="must be required"):
        LibrarySchema(
            name="x_v1",
            fields=[{"key": "outcome", "choices": ["a"], "required": False}],
            discriminator="outcome",
        )
    with pytest.raises(ValueError, match="already branches on 'resolution'"):
        LibrarySchema(name="x_v1", base="bug_hunter_v1", fields=fields, discriminator="outcome")
    with pytest.raises(ValueError, match="need a discriminator"):
        LibrarySchema(name="x_v1", fields=fields, branches={"a": [{"key": "z"}]})
    with pytest.raises(ValueError, match="are not choices"):
        LibrarySchema(name="x_v1", fields=fields, discriminator="outcome", branches={"c": []})
    with pytest.raises(ValueError, match="repeats top-level field"):
        LibrarySchema(
            name="x_v1",
            fields=fields + [{"key": "z"}],
            discriminator="outcome",
            branches={"a": [{"key": "z"}]},
        )


# -- registry ------------------------------------------------------------------


def test_registry_reports_problems_not_silence(workspace):
    d = workspace.findings_schemas_dir
    (d / "broken.yaml").write_text("name: [", encoding="utf-8")
    (d / "shadow.yaml").write_text("name: bug_hunter_v1\n", encoding="utf-8")
    (d / "good.yaml").write_text("name: good_v1\nfields:\n  - key: a\n", encoding="utf-8")
    problems = {p["file"]: p["problem"] for p in schema_problems(d)}
    assert "parse" in problems["broken.yaml"]
    assert "shadows the built-in" in problems["shadow.yaml"]
    assert [s.name for s in list_library_schemas(d)] == ["good_v1"]
    assert known_schema("good_v1", d) and known_schema("standard_v1", d)
    assert not known_schema("bug_hunter_v1_", d)
    with pytest.raises(SchemaNotFound, match="unknown findings schema"):
        get_library_schema("nope", d)
    assert schemas_dir_beside(workspace.workflows_dir) == d
    assert schemas_dir_beside(None) is None


# -- layered resolution ----------------------------------------------------------


def test_effective_schema_layers_builtin_library_workflow(workspace):
    d = workspace.findings_schemas_dir
    _triage(d)
    entries = effective_extra("orders_triage_v1", [{"key": "ticket", "required": False}], d)
    assert [(e["key"], e["source"]) for e in entries] == [
        ("owner_team", "library"),
        ("outcome", "library"),
        ("ticket", "workflow"),
    ]
    # a workflow tightening a library field
    entries = effective_extra("orders_triage_v1", [{"key": "owner_team", "choices": ["x"]}], d)
    assert entries[0]["source"] == "library+workflow" and entries[0]["choices"] == ["x"]
    assert entries[0]["required"] is True
    # a library schema tightening a built-in field
    create_schema(d, "bh_v1", base="bug_hunter_v1", user_id="kcg")
    sc = get_library_schema("bh_v1", d)
    sc = apply_schema_edit(
        sc,
        {
            "kind": "field",
            "orig_key": "owner_team",
            "key": "blast_radius",
            "choices": "rows | keys",
            "description": "",
        },
    )
    save_schema(d, sc, "kcg")
    entries = {e["key"]: e for e in effective_extra("bh_v1", None, d)}
    blast = entries["blast_radius"]
    assert (blast["source"], blast["choices"]) == ("schema+library", ["rows", "keys"])
    assert blast["description"]  # the built-in's stays when the library gives none
    assert len(entries) == 3  # tightened, not duplicated


def test_validation_enforces_library_fields_and_branches(workspace):
    d = workspace.findings_schemas_dir
    _triage(d)
    with pytest.raises(ValueError, match="requires extra.outcome"):
        validate_finding(_finding(), "orders_triage_v1", None, d)
    with pytest.raises(ValueError, match="outcome must be one of"):
        validate_finding(
            _finding(extra={"owner_team": "a", "outcome": "maybe"}), "orders_triage_v1", None, d
        )
    with pytest.raises(ValueError, match="fix_ref"):
        validate_finding(
            _finding(extra={"owner_team": "a", "outcome": "fixed"}), "orders_triage_v1", None, d
        )
    ok = validate_finding(
        _finding(extra={"owner_team": "a", "outcome": "fixed", "fix_ref": "PR 1"}),
        "orders_triage_v1",
        None,
        d,
    )
    assert ok.extra["fix_ref"] == "PR 1"
    assert validate_finding(
        _finding(extra={"owner_team": "a", "outcome": "deferred"}), "orders_triage_v1", None, d
    )
    with pytest.raises(ValueError, match="unknown findings schema 'nope'"):
        validate_finding(_finding(), "nope", None, d)


def test_describe_schema_covers_library_and_branches(workspace):
    d = workspace.findings_schemas_dir
    _triage(d)
    spec = describe_schema("orders_triage_v1", None, d)
    assert spec["library"] is True and spec["base"] == "standard_v1"
    assert spec["title"] == "Orders triage" and spec["created_by"] == "kcg"
    assert spec["discriminator"] == "outcome"
    assert {k: [f["key"] for f in v] for k, v in spec["conditional_extra"].items()} == {
        "fixed": ["fix_ref"],
        "deferred": [],
    }
    assert spec["example"]["extra"] == {"owner_team": "...", "outcome": "fixed", "fix_ref": "..."}
    validate_finding(spec["example"], "orders_triage_v1", None, d)  # shaped to pass
    assert describe_schema("nope", None, d)["known"] is False


def test_workflow_may_name_a_library_schema(workspace):
    d = workspace.findings_schemas_dir
    _triage(d)
    (workspace.workflows_dir / "w.yaml").write_text(
        "name: w\ntitle: W\nfindings_schema: orders_triage_v1\n", encoding="utf-8"
    )
    assert get_workflow("w", workspace.workflows_dir).findings_schema == "orders_triage_v1"
    (workspace.workflows_dir / "w2.yaml").write_text(
        "name: w2\ntitle: W\nfindings_schema: nope_v1\n", encoding="utf-8"
    )
    from grayson.workflows import override_problems

    [problem] = override_problems(workspace.workflows_dir)
    assert "unknown findings_schema 'nope_v1'" in problem["problem"]
    assert "orders_triage_v1" in problem["problem"]  # library names are in the known list
    assert workflows_using("orders_triage_v1", workspace.workflows_dir) == ["w"]


def test_engine_validates_against_library_schema(workspace):
    from conftest import FakeExecutor
    from grayson.core import engine
    from grayson.core.engine import EnforcementError
    from grayson.core.run import run_statement
    from grayson.core.session import Session

    d = workspace.findings_schemas_dir
    _triage(d)
    (workspace.workflows_dir / "w.yaml").write_text(
        "name: w\ntitle: W\nfindings_schema: orders_triage_v1\n"
        "required_checks:\n  - key: a\n    title: A\n",
        encoding="utf-8",
    )
    s = Session.create(
        workspace,
        workflow="w",
        targets=["DB.S.T1"],
        guard=workspace.config.guard_profiles["moderate"].model_copy(),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s, workspace.workflows_dir)
    qid = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    with pytest.raises(EnforcementError, match="outcome"):
        engine.record_finding(s, _finding(evidence=[qid]), overrides_dir=workspace.workflows_dir)
    engine.record_finding(
        s,
        _finding(evidence=[qid], extra={"owner_team": "a", "outcome": "deferred"}),
        overrides_dir=workspace.workflows_dir,
    )


# -- authoring -----------------------------------------------------------------


def test_create_fork_and_ownership(workspace):
    d = workspace.findings_schemas_dir
    path = create_schema(d, "orders_triage_v1", base="bug_hunter_v1", user_id="kcg")
    sc = get_library_schema("orders_triage_v1", d)
    assert (sc.base, sc.created_by, sc.title) == ("bug_hunter_v1", "kcg", "Orders Triage")
    assert "owner_team" in path.read_text(encoding="utf-8")  # the scaffold's example field
    with pytest.raises(SchemaAuthoringError, match="already exists"):
        create_schema(d, "orders_triage_v1")
    with pytest.raises(SchemaAuthoringError, match="built-in"):
        create_schema(d, "standard_v1")
    with pytest.raises(SchemaAuthoringError, match="rather than forking"):
        create_schema(d, "x_v1", fork_of="standard_v1")
    create_schema(d, "fork_v1", fork_of="orders_triage_v1", user_id="mkoval2")
    fork = get_library_schema("fork_v1", d)
    assert (fork.forked_from, fork.created_by, fork.base) == (
        "orders_triage_v1",
        "mkoval2",
        "bug_hunter_v1",
    )
    assert can_edit_schema(sc, "kcg") and not can_edit_schema(sc, "mkoval2")
    assert not can_edit_schema(fork, "kcg")
    with pytest.raises(SchemaAuthoringError, match="fork it under a new name"):
        save_schema_yaml(d, "fork_v1", "name: fork_v1\ncreated_by: mkoval2\n", "kcg")
    with pytest.raises(SchemaAuthoringError, match="renames are a"):
        save_schema_yaml(d, "orders_triage_v1", "name: other_v1\n", "kcg")
    # a legacy file with no author: the first save stamps the editor
    (d / "legacy_v1.yaml").write_text("name: legacy_v1\n", encoding="utf-8")
    assert (
        save_schema_yaml(d, "legacy_v1", "name: legacy_v1\ntitle: L\n", "kcg").created_by == "kcg"
    )


def test_element_edits(workspace):
    d = workspace.findings_schemas_dir
    sc = _triage(d)
    assert sc.discriminator == "outcome" and [f.key for f in sc.branches["fixed"]] == ["fix_ref"]
    # move, delete, rename
    sc2 = apply_schema_edit(
        sc, {"kind": "field", "action": "move", "key": "outcome", "direction": "up"}
    )
    assert [f.key for f in sc2.fields] == ["outcome", "owner_team"]
    sc2 = apply_schema_edit(sc2, {"kind": "field", "action": "delete", "key": "owner_team"})
    assert [f.key for f in sc2.fields] == ["outcome"]
    sc2 = apply_schema_edit(
        sc2, {"kind": "field", "branch": "fixed", "action": "delete", "key": "fix_ref"}
    )
    assert "fixed" not in sc2.branches  # an emptied branch is dropped
    # clearing the discriminator drops the branches
    sc3 = apply_schema_edit(sc, {"kind": "discriminator", "key": ""})
    assert sc3.discriminator == "" and sc3.branches == {}
    # rules surface as authoring errors
    with pytest.raises(SchemaAuthoringError, match="already exists"):
        apply_schema_edit(sc, {"kind": "field", "key": "outcome", "description": "dup"})
    with pytest.raises(SchemaAuthoringError, match="base field"):
        apply_schema_edit(sc, {"kind": "field", "key": "evidence"})
    with pytest.raises(SchemaAuthoringError, match="does not validate"):
        apply_schema_edit(sc, {"kind": "discriminator", "key": "owner_team"})  # no choices
    with pytest.raises(SchemaAuthoringError, match="built-in"):
        apply_schema_edit(sc, {"kind": "meta", "base": "orders_triage_v1"})
    with pytest.raises(SchemaAuthoringError, match="unknown element kind"):
        apply_schema_edit(sc, {"kind": "banana"})
    # unknown fields ride along
    raw = yaml.safe_load((d / "orders_triage_v1.yaml").read_text(encoding="utf-8"))
    raw["owner_channel"] = "#data"
    save_schema_yaml(d, "orders_triage_v1", yaml.safe_dump(raw), "kcg")
    sc4 = apply_schema_edit(
        get_library_schema("orders_triage_v1", d), {"kind": "meta", "title": "T"}
    )
    assert sc4.model_dump()["owner_channel"] == "#data"


def test_delete_rules(workspace):
    d = workspace.findings_schemas_dir
    _triage(d)
    with pytest.raises(SchemaAuthoringError, match="built in"):
        delete_schema(d, "standard_v1", "kcg")
    with pytest.raises(SchemaAuthoringError, match="no library file"):
        delete_schema(d, "ghost_v1", "kcg")
    with pytest.raises(SchemaAuthoringError, match="only its author"):
        delete_schema(d, "orders_triage_v1", "mkoval2")
    with pytest.raises(SchemaAuthoringError, match="findings schema of 1 workflow"):
        delete_schema(d, "orders_triage_v1", "kcg", used_by=["w"])
    assert not delete_schema(d, "orders_triage_v1", "kcg").exists()
    (d / "broken.yaml").write_text("name: [", encoding="utf-8")
    delete_schema(d, "broken", "anyone")  # no author to protect


def test_preview_and_lint(workspace):
    d = workspace.findings_schemas_dir
    sc = _triage(d)
    text = render_schema_preview(sc, d, ["w"])
    assert "orders_triage_v1 — Orders triage" in text and "extends standard_v1" in text
    assert "owner_team (this schema's): Which team owns the fix." in text
    assert "Branches on `outcome`" in text and "fix_ref: The change." in text
    assert "(no further fields)" in text and "Used by: w" in text
    assert lint_schema(sc) == []
    sparse = LibrarySchema(name="sparse", fields=[{"key": "a"}])
    notes = lint_schema(sparse)
    assert any("no description" in n for n in notes)
    assert any("version suffix" in n for n in notes)
    assert any("field 'a' has no description" in n for n in notes)
    report = lint_schemas(d, workspace.workflows_dir)
    assert report["ok"] and report["checked"] == ["orders_triage_v1.yaml"]
    assert any("no workflow names this schema" in w["problem"] for w in report["warnings"])
    (d / "shadow.yaml").write_text("name: standard_v1\n", encoding="utf-8")
    assert lint_schemas(d)["ok"] is False


# -- promotion -------------------------------------------------------------------


def _workflow_with_fields(workspace):
    create_workflow(workspace.workflows_dir, "orders-x", fork_of="table-health", user_id="kcg")
    tpl = apply_element_edit(
        get_workflow("orders-x", workspace.workflows_dir),
        {"kind": "field", "key": "owner_team", "description": "who", "choices": "a | b"},
    )
    save_workflow_yaml(workspace.workflows_dir, "orders-x", dump_workflow(tpl), "kcg")


def test_promote_fields_to_a_shared_schema(workspace):
    _workflow_with_fields(workspace)
    schema, repointed = plan_promotion(workspace.workflows_dir, "orders-x", "orders_x_v1", "kcg")
    assert (schema.base, schema.created_by, [f.key for f in schema.fields]) == (
        "standard_v1",
        "kcg",
        ["owner_team"],
    )
    assert repointed.findings_schema == "orders_x_v1" and repointed.findings_fields == []
    assert not (workspace.findings_schemas_dir / "orders_x_v1.yaml").exists()  # a plan only
    path, tpl = promote_fields(workspace.workflows_dir, "orders-x", "orders_x_v1", "kcg")
    assert path.is_file() and tpl.findings_schema == "orders_x_v1"
    assert get_workflow("orders-x", workspace.workflows_dir).findings_fields == []
    # the effective contract is unchanged by the move
    entries = effective_extra("orders_x_v1", None, workspace.findings_schemas_dir)
    assert [(e["key"], e["choices"]) for e in entries] == [("owner_team", ["a", "b"])]
    with pytest.raises(WorkflowAuthoringError, match="already uses the library schema"):
        plan_promotion(workspace.workflows_dir, "orders-x", "again_v1", "kcg")


def test_promote_refusals(workspace):
    _workflow_with_fields(workspace)
    with pytest.raises(WorkflowAuthoringError, match="not yours"):
        plan_promotion(workspace.workflows_dir, "orders-x", "x_v1", "mkoval2")
    with pytest.raises(WorkflowAuthoringError, match="schema name must be"):
        plan_promotion(workspace.workflows_dir, "orders-x", "Bad-Name", "kcg")
    create_schema(workspace.findings_schemas_dir, "taken_v1")
    with pytest.raises(WorkflowAuthoringError, match="already exists"):
        plan_promotion(workspace.workflows_dir, "orders-x", "taken_v1", "kcg")
    create_workflow(workspace.workflows_dir, "plain", user_id="kcg")
    with pytest.raises(WorkflowAuthoringError, match="no findings_fields"):
        plan_promotion(workspace.workflows_dir, "plain", "plain_v1", "kcg")


# -- CLI -----------------------------------------------------------------------


def test_cli_schema_commands(workspace):
    from typer.testing import CliRunner

    from grayson.cli import app

    runner = CliRunner()

    def invoke(*args):
        result = runner.invoke(app, list(args))
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    invoke("user", "set", "kcg")
    out = invoke("schema", "new", "orders_triage_v1", "--base", "bug_hunter_v1")
    assert out["base"] == "bug_hunter_v1" and out["lint"]["ok"]
    names = {s["name"]: s for s in invoke("schema", "list")}
    assert names["orders_triage_v1"]["created_by"] == "kcg"
    assert names["bug_hunter_v1"]["used_by"] == ["bug-hunter"]
    show = invoke("schema", "show", "orders_triage_v1")
    assert show["library"] and show["base"] == "bug_hunter_v1"
    assert [e["key"] for e in show["required_extra"]][-1] == "owner_team"
    preview = invoke("schema", "preview", "orders_triage_v1")
    assert "Branches on `resolution` (built in to bug_hunter_v1)" in preview["text"]
    assert invoke("schema", "lint")["ok"] is True
    # workflow show and session start carry the library schema unpacked
    _workflow_with_fields(workspace)
    invoke("workflow", "promote", "orders-x", "--schema", "orders_x_v1")
    wf = invoke("workflow", "show", "orders-x")
    assert wf["findings_schema"] == "orders_x_v1"
    assert wf["findings_schema_spec"]["library"] is True
    assert "orders_x_v1" in invoke("workflow", "schemas")
    assert invoke("workflow", "lint")["schemas"]["ok"] is True
    result = runner.invoke(app, ["schema", "delete", "orders_x_v1", "--yes"])
    assert result.exit_code == 1 and "findings schema of 1 workflow" in result.output
    assert invoke("schema", "delete", "orders_triage_v1", "--yes")["deleted"].endswith(
        "orders_triage_v1.yaml"
    )
    result = runner.invoke(app, ["schema", "show", "nope"])
    assert result.exit_code == 1
