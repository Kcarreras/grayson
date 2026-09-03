"""Workflows tab: catalog, detail, review-then-save, element editing, delete,
and ownership enforcement."""

from __future__ import annotations

import html
import re

import pytest
from fastapi.testclient import TestClient

from grayson.identity import set_user_id
from grayson.ui.server import build_app
from grayson.workflows import get_workflow
from grayson.workflows.authoring import create_workflow

TOKEN = "test-token"


@pytest.fixture
def client(workspace):
    return TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")


def _get(client, path, params=None, **kw):
    return client.get(path, params={"t": TOKEN, **(params or {})}, **kw)


def _post(client, path, data, **kw):
    return client.post(path, params={"t": TOKEN}, data=data, follow_redirects=False, **kw)


def _reviewed_yaml(page_text: str) -> str:
    """The YAML a review page would save, as its confirm form carries it."""
    m = re.search(r'name="yaml" value="(.*?)">', page_text, re.S)
    assert m, "no review form on the page"
    return html.unescape(m.group(1))


def _element(client, name, **fields):
    return _post(client, f"/workflows/{name}/element", fields)


def _confirm(client, name, page_text):
    return _post(
        client, f"/workflows/{name}/edit", {"yaml": _reviewed_yaml(page_text), "action": "confirm"}
    )


# -- catalog ---------------------------------------------------------------


def test_gallery_lists_core_and_team(client, workspace):
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "orders-health", fork_of="table-health", user_id="kcg")
    (workspace.workflows_dir / "broken.yaml").write_text("name: [", encoding="utf-8")
    page = _get(client, "/workflows")
    assert page.status_code == 200
    assert "bug-hunter" in page.text
    assert "orders-health" in page.text
    assert "lint failed" in page.text
    assert "mine" in page.text


def test_gallery_is_a_filterable_list(client, workspace):
    """The catalog is one [data-list] with the shared toolbar: text filter,
    sort, and chips for core/team/mine/forks plus every user tag in use."""
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "orders-health", fork_of="table-health", user_id="kcg")
    path = workspace.workflows_dir / "orders-health.yaml"
    path.write_text(
        path.read_text(encoding="utf-8") + "tags: [orders, finance]\n", encoding="utf-8"
    )
    page = _get(client, "/workflows").text
    assert 'data-list-tools="workflows"' in page and 'data-list="workflows"' in page
    for chip in ("core", "team", "mine", "fork", "charts", "broken"):
        assert f'data-tag="{chip}"' in page
    assert 'data-tag="tag-orders"' in page and 'data-tag="tag-finance"' in page
    assert 'data-tags="team mine fork charts tag-orders tag-finance"' in page
    assert "#orders" in page


def test_gallery_unpacks_findings_schemas(client):
    page = _get(client, "/workflows").text
    assert 'id="schemas"' in page
    assert "bug_hunter_v1" in page and "blast_radius" in page
    assert "branches on resolution" in page
    assert "feature_readiness_v1" in page and "leakage_assessment" in page


# -- detail ----------------------------------------------------------------


def test_detail_core_shows_canonical(client):
    page = _get(client, "/workflows/bug-hunter")
    assert page.status_code == 200
    assert "Canonical" in page.text
    assert "evidence" in page.text  # the flow rail's gate
    assert "replicate_anomaly" in page.text
    assert "/workflows/bug-hunter/edit" not in page.text  # core: no edit affordance
    assert "/workflows/bug-hunter/element" not in page.text  # nor element forms
    assert "Danger zone" not in page.text


def test_detail_shows_charts_inputs_and_schema(client):
    page = _get(client, "/workflows/bug-hunter").text
    # scope_blast_radius requires one: stated as the requirement, kinds named
    assert "chart: line|bar" in page
    assert "needs chart" not in page  # a requirement, not a missing chart
    assert "read by" in page  # setup inputs show which checkpoints use them
    assert 'data-list="checks"' in page  # checkpoints are a filterable list
    assert 'data-tag="chart"' in page
    # the schema unpacked: required extras, branches, base fields, example
    assert "blast_radius" in page and "alternatives_tested" in page
    assert "branches on resolution" in page
    assert "remaining_hypotheses" in page
    assert "Base fields every finding carries" in page
    assert "Example payload" in page and "&#34;evidence&#34;" in page or '"evidence"' in page


def test_detail_lists_lint_notes_for_library_workflows(client, workspace):
    (workspace.workflows_dir / "sparse.yaml").write_text(
        "name: sparse\ntitle: Sparse\ndescription: d\n"
        "required_checks:\n  - key: only\n    title: Only\n",
        encoding="utf-8",
    )
    page = _get(client, "/workflows/sparse").text
    assert "Lint has 1 note" in page
    assert "checkpoint &#39;only&#39; has no description" in page


def test_detail_unknown_404(client):
    assert _get(client, "/workflows/nope").status_code == 404


# -- yaml ------------------------------------------------------------------


def test_yaml_page_and_raw_export(client):
    page = _get(client, "/workflows/bug-hunter/yaml")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "name: bug-hunter" in page.text
    assert "raw=1" in page.text  # the download link
    raw = client.get("/workflows/bug-hunter/yaml", params={"t": TOKEN, "raw": "1"})
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("text/plain")
    assert 'filename="bug-hunter.yaml"' in raw.headers["content-disposition"]
    assert raw.text.startswith("name: bug-hunter")


# -- edit: review, then confirm --------------------------------------------


def test_fork_then_edit_flow(client, workspace):
    set_user_id("kcg")
    r = _post(client, "/workflows/bug-hunter/fork", {"new_name": "bug-hunter-kcg"})
    assert r.status_code == 303
    assert "/workflows/bug-hunter-kcg?" in r.headers["location"]  # the fork's own page
    tpl = get_workflow("bug-hunter-kcg", workspace.workflows_dir)
    assert tpl.forked_from == "bug-hunter"
    assert tpl.created_by == "kcg"
    edit = _get(client, "/workflows/bug-hunter-kcg/edit")
    assert edit.status_code == 200
    new_yaml = client.get(
        "/workflows/bug-hunter-kcg/yaml", params={"t": TOKEN, "raw": "1"}
    ).text.replace("Bug Hunter (fork)", "My Hunter")
    # submitting the editor reviews; nothing is written yet
    review = _post(client, "/workflows/bug-hunter-kcg/edit", {"yaml": new_yaml})
    assert review.status_code == 200
    assert "not saved yet" in review.text
    assert "-title: Bug Hunter (fork)" in review.text and "+title: My Hunter" in review.text
    assert "bug-hunter-kcg — My Hunter" in review.text  # the preview
    assert get_workflow("bug-hunter-kcg", workspace.workflows_dir).title == "Bug Hunter (fork)"
    # confirming writes exactly what was reviewed
    save = _confirm(client, "bug-hunter-kcg", review.text)
    assert save.status_code == 303
    assert get_workflow("bug-hunter-kcg", workspace.workflows_dir).title == "My Hunter"


def test_review_back_keeps_the_draft(client, workspace):
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    draft = _get(client, "/workflows/mine/yaml", params={"raw": "1"}).text + "tags: [draft]\n"
    r = _post(client, "/workflows/mine/edit", {"yaml": draft, "action": "back"})
    assert r.status_code == 200
    assert "tags: [draft]" in r.text
    assert not get_workflow("mine", workspace.workflows_dir).tags


def test_review_of_an_unchanged_file_offers_no_save(client, workspace):
    set_user_id("kcg")
    # a fork is written in canonical layout; a scaffold's comments would differ
    create_workflow(workspace.workflows_dir, "mine", fork_of="table-health", user_id="kcg")
    text = _get(client, "/workflows/mine/yaml", params={"raw": "1"}).text
    r = _post(client, "/workflows/mine/edit", {"yaml": text})
    assert r.status_code == 200
    assert "Nothing changes" in r.text
    assert 'value="confirm"' not in r.text


def test_review_carries_lint_warnings(client, workspace):
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    text = (
        "name: mine\ntitle: M\ndescription: d\ncreated_by: kcg\n"
        "required_checks:\n  - key: a\n    title: A\n"
    )
    r = _post(client, "/workflows/mine/edit", {"yaml": text})
    assert r.status_code == 200
    assert "Lint will note" in r.text
    assert "has no description" in r.text


def test_edit_core_forbidden(client):
    assert _get(client, "/workflows/bug-hunter/edit").status_code == 403
    r = _post(client, "/workflows/bug-hunter/edit", {"yaml": "name: bug-hunter\n"})
    assert r.status_code == 400  # review path refuses core names
    r = _post(
        client, "/workflows/bug-hunter/edit", {"yaml": "name: bug-hunter\n", "action": "confirm"}
    )
    assert r.status_code == 400  # and so does the save path


def test_edit_someone_elses_forbidden(client, workspace):
    create_workflow(workspace.workflows_dir, "theirs", user_id="mkoval2")
    set_user_id("kcg")
    assert _get(client, "/workflows/theirs/edit").status_code == 403
    r = _post(client, "/workflows/theirs/edit", {"yaml": "name: theirs\ncreated_by: mkoval2\n"})
    assert r.status_code == 400
    r = _post(
        client,
        "/workflows/theirs/edit",
        {"yaml": "name: theirs\ncreated_by: mkoval2\n", "action": "confirm"},
    )
    assert r.status_code == 400
    assert _element(client, "theirs", kind="meta", title="x").status_code == 403


def test_fork_name_collision_rerenders_detail(client, workspace):
    create_workflow(workspace.workflows_dir, "taken")
    r = _post(client, "/workflows/bug-hunter/fork", {"new_name": "taken"})
    assert r.status_code == 400
    assert "already exists" in r.text
    assert "replicate_anomaly" in r.text  # the workflow page, with the error


def test_create_from_scratch(client, workspace):
    set_user_id("kcg")
    r = _post(client, "/workflows/new", {"new_name": "fresh-check", "fork_of": ""})
    assert r.status_code == 303
    assert "/workflows/fresh-check?" in r.headers["location"]
    tpl = get_workflow("fresh-check", workspace.workflows_dir)
    assert tpl.created_by == "kcg"
    assert tpl.forked_from == ""


def test_invalid_save_rerenders_with_error(client, workspace):
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    r = _post(client, "/workflows/mine/edit", {"yaml": "name: ["})
    assert r.status_code == 400
    assert "YAML does not parse" in r.text
    # the broken text is what re-renders, so nothing typed is lost
    assert "name: [" in r.text


# -- element editing -------------------------------------------------------


def test_detail_offers_element_forms_to_the_author(client, workspace):
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "mine", fork_of="bug-hunter", user_id="kcg")
    page = _get(client, "/workflows/mine").text
    assert "/workflows/mine/element" in page
    assert "Add a checkpoint" in page and "Add a setup input" in page
    assert "Add a field this workflow requires" in page
    assert "Edit title, description, tags, defaults" in page
    assert "Delete this workflow" in page
    # a teammate sees none of it
    set_user_id("someone-else")
    page = _get(client, "/workflows/mine").text
    assert "/workflows/mine/element" not in page
    assert "fork to edit" in page


def test_element_edit_reviews_then_saves(client, workspace):
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "mine", fork_of="bug-hunter", user_id="kcg")
    r = _element(
        client,
        "mine",
        kind="check",
        list="required",
        action="upsert",
        orig_key="scope_blast_radius",
        key="scope_blast_radius",
        title="Bound the blast radius",
        description="How far it spreads.",
        depends_on="replicate_anomaly",
        uses_inputs="anomaly_description",
        charts="histogram: the spread of affected keys",
    )
    assert r.status_code == 200
    assert "Confirm: editing checkpoint" in r.text
    assert "- histogram" in r.text  # the diff shows the new kind arriving
    assert get_workflow("mine", workspace.workflows_dir).check("scope_blast_radius").charts[
        0
    ].kinds == ["line", "bar"]
    assert _confirm(client, "mine", r.text).status_code == 303
    check = get_workflow("mine", workspace.workflows_dir).check("scope_blast_radius")
    assert check.charts[0].kinds == ["histogram"]
    assert check.description == "How far it spreads."
    assert check.uses_inputs == ["anomaly_description"]


def test_element_add_findings_field_with_choices(client, workspace):
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    r = _element(
        client,
        "mine",
        kind="field",
        action="upsert",
        key="verdict",
        description="Ship it or not.",
        choices="ship | hold",
        required="on",
    )
    assert r.status_code == 200
    assert "Confirm: editing findings field &#39;verdict&#39;" in r.text
    assert _confirm(client, "mine", r.text).status_code == 303
    [field] = get_workflow("mine", workspace.workflows_dir).findings_fields
    assert (field.key, field.choices, field.required) == ("verdict", ["ship", "hold"], True)
    # an unchecked box means optional
    r = _element(
        client,
        "mine",
        kind="field",
        action="upsert",
        orig_key="verdict",
        key="verdict",
        choices="ship | hold",
    )
    assert _confirm(client, "mine", r.text).status_code == 303
    assert get_workflow("mine", workspace.workflows_dir).findings_fields[0].required is False
    page = _get(client, "/workflows/mine").text
    assert "this workflow" in page and "optional" in page


def test_element_move_delete_and_meta(client, workspace):
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "mine", fork_of="bug-hunter", user_id="kcg")
    r = _element(
        client,
        "mine",
        kind="check",
        list="required",
        action="move",
        key="upstream_trace",
        to_list="suggested",
    )
    assert "Confirm: moving checkpoint" in r.text
    _confirm(client, "mine", r.text)
    tpl = get_workflow("mine", workspace.workflows_dir)
    assert "upstream_trace" not in tpl.required_check_keys()
    assert tpl.suggested_check_keys()[-1] == "upstream_trace"

    r = _element(client, "mine", kind="input", action="delete", key="expectation")
    assert "Confirm: removing setup input" in r.text
    _confirm(client, "mine", r.text)
    assert "expectation" not in get_workflow("mine", workspace.workflows_dir).input_keys()

    r = _element(
        client,
        "mine",
        kind="meta",
        title="Mine",
        description="Ours.",
        tags="orders, Finance",
        suggested_guard_profile="moderate",
        suggested_strict_scope="true",
        findings_schema="standard_v1",
    )
    assert "Confirm: editing the header" in r.text
    _confirm(client, "mine", r.text)
    tpl = get_workflow("mine", workspace.workflows_dir)
    assert (tpl.title, tpl.tags, tpl.suggested_strict_scope, tpl.findings_schema) == (
        "Mine",
        ["orders", "finance"],
        True,
        "standard_v1",
    )


def test_element_edit_errors_rerender_the_page(client, workspace):
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "mine", fork_of="bug-hunter", user_id="kcg")
    r = _element(
        client,
        "mine",
        kind="check",
        list="required",
        action="upsert",
        key="validate_expectation",
        title="dup",
    )
    assert r.status_code == 400
    assert "already exists" in r.text
    r = _element(
        client,
        "mine",
        kind="check",
        list="required",
        action="upsert",
        key="x",
        title="X",
        charts="pie: nope",
    )
    assert r.status_code == 400
    assert "unknown chart kind" in r.text
    assert _element(client, "bug-hunter", kind="meta", title="x").status_code == 403


# -- delete ------------------------------------------------------------------


def test_delete_own_workflow(client, workspace):
    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "mine", user_id="kcg")
    r = _post(client, "/workflows/mine/delete", {"confirm_name": "wrong"})
    assert r.status_code == 400 and "type the workflow" in r.text
    assert (workspace.workflows_dir / "mine.yaml").exists()
    r = _post(client, "/workflows/mine/delete", {"confirm_name": "mine"})
    assert r.status_code == 303
    assert not (workspace.workflows_dir / "mine.yaml").exists()
    assert _get(client, "/workflows/mine").status_code == 404


def test_delete_refused_for_core_and_teammates(client, workspace):
    create_workflow(workspace.workflows_dir, "theirs", user_id="mkoval2")
    set_user_id("kcg")
    r = _post(client, "/workflows/theirs/delete", {"confirm_name": "theirs"})
    assert r.status_code == 400 and "only its author" in r.text
    assert (workspace.workflows_dir / "theirs.yaml").exists()
    r = _post(client, "/workflows/bug-hunter/delete", {"confirm_name": "bug-hunter"})
    assert r.status_code == 400 and "cannot be deleted" in r.text


def test_delete_broken_file_from_the_gallery(client, workspace):
    (workspace.workflows_dir / "broken.yaml").write_text("name: [", encoding="utf-8")
    page = _get(client, "/workflows").text
    assert "/workflows/broken/delete" in page
    r = _post(client, "/workflows/broken/delete", {"confirm_name": "broken"})
    assert r.status_code == 303
    assert not (workspace.workflows_dir / "broken.yaml").exists()


def test_delete_refused_while_sessions_are_open(client, workspace):
    from grayson.core.session import Session

    set_user_id("kcg")
    create_workflow(workspace.workflows_dir, "mine", fork_of="table-health", user_id="kcg")
    Session.create(
        workspace,
        workflow="mine",
        targets=["DB.S.T"],
        guard=workspace.config.guard_profiles["moderate"].model_copy(),
        guard_profile="moderate",
    )
    page = _get(client, "/workflows/mine").text
    assert "1 open now" in page
    r = _post(client, "/workflows/mine/delete", {"confirm_name": "mine"})
    assert r.status_code == 400 and "open session" in r.text
    assert (workspace.workflows_dir / "mine.yaml").exists()
