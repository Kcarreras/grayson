"""Workflows tab: gallery, detail, fork-on-edit, and ownership enforcement."""

from __future__ import annotations

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


def _get(client, path, **kw):
    return client.get(path, params={"t": TOKEN}, **kw)


def _post(client, path, data, **kw):
    return client.post(path, params={"t": TOKEN}, data=data, follow_redirects=False, **kw)


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


def test_detail_core_shows_canonical(client):
    page = _get(client, "/workflows/bug-hunter")
    assert page.status_code == 200
    assert "Canonical" in page.text
    assert "evidence" in page.text  # the flow rail's gate
    assert "replicate_anomaly" in page.text
    assert "/workflows/bug-hunter/edit" not in page.text  # core: no edit affordance


def test_detail_unknown_404(client):
    assert _get(client, "/workflows/nope").status_code == 404


def test_yaml_export(client):
    r = _get(client, "/workflows/bug-hunter/yaml")
    assert r.status_code == 200
    assert "name: bug-hunter" in r.text


def test_fork_then_edit_flow(client, workspace):
    set_user_id("kcg")
    r = _post(client, "/workflows/bug-hunter/fork", {"new_name": "bug-hunter-kcg"})
    assert r.status_code == 303
    assert "/workflows/bug-hunter-kcg/edit" in r.headers["location"]
    tpl = get_workflow("bug-hunter-kcg", workspace.workflows_dir)
    assert tpl.forked_from == "bug-hunter"
    assert tpl.created_by == "kcg"
    edit = _get(client, "/workflows/bug-hunter-kcg/edit")
    assert edit.status_code == 200
    new_yaml = _get(client, "/workflows/bug-hunter-kcg/yaml").text.replace(
        "Bug Hunter (fork)", "My Hunter"
    )
    save = _post(client, "/workflows/bug-hunter-kcg/edit", {"yaml": new_yaml})
    assert save.status_code == 303
    assert get_workflow("bug-hunter-kcg", workspace.workflows_dir).title == "My Hunter"


def test_edit_core_forbidden(client):
    assert _get(client, "/workflows/bug-hunter/edit").status_code == 403
    r = _post(client, "/workflows/bug-hunter/edit", {"yaml": "name: bug-hunter\n"})
    assert r.status_code == 400  # save path also refuses core names


def test_edit_someone_elses_forbidden(client, workspace):
    create_workflow(workspace.workflows_dir, "theirs", user_id="mkoval2")
    set_user_id("kcg")
    assert _get(client, "/workflows/theirs/edit").status_code == 403
    r = _post(client, "/workflows/theirs/edit", {"yaml": "name: theirs\ncreated_by: mkoval2\n"})
    assert r.status_code == 400


def test_fork_name_collision_rerenders_gallery(client, workspace):
    create_workflow(workspace.workflows_dir, "taken")
    r = _post(client, "/workflows/bug-hunter/fork", {"new_name": "taken"})
    assert r.status_code == 400
    assert "already exists" in r.text


def test_create_from_scratch(client, workspace):
    set_user_id("kcg")
    r = _post(client, "/workflows/new", {"new_name": "fresh-check", "fork_of": ""})
    assert r.status_code == 303
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
