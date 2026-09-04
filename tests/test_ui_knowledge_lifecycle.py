"""The console's side of the knowledge lifecycle: standing on the table page
and the tab's tiles, the human's actions, the policy on Settings."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from grayson.knowledge import KnowledgeStore
from grayson.ui.server import build_app

TOKEN = "test-token"
T = "DB.S.T"


@pytest.fixture
def client(workspace):
    return TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")


@pytest.fixture
def ks(workspace):
    return KnowledgeStore(workspace.knowledge_dir)


def _seed(ks):
    ks.set_profile(T, {"columns": [{"name": "AMOUNT", "description": "d"}, {"name": "ID"}]})
    ks.add_fact(T, "AMOUNT is gross", fact_id="amt")
    ks.sync_columns(T, [{"name": "ID", "type": "NUMBER"}])  # AMOUNT dropped: amt is stale
    ks.add_fact(T, "old", fact_id="old")
    ks.confirm_fact(T, "old")
    ks.add_fact(T, "new", fact_id="new", supersedes="old", evidence=["q_1"])
    ks.add_fact(T, "gone", fact_id="gone")
    ks.retire_fact(T, "gone", by="agent", evidence=["q_2"])
    ks.set_profile(T, {"open_questions": ["Is AMOUNT signed?"]})


def test_table_page_shows_standing_contested_retired_and_agent_actions(client, ks):
    _seed(ks)
    page = client.get(f"/knowledge/{T}?t={TOKEN}").text
    assert 'data-standing="stale"' in page and "column AMOUNT was dropped" in page
    assert "Contested" in page and "Mark compatible" in page and "data-contested" in page
    assert "data-retired" in page and 'data-standing="retired"' in page
    assert "Recent agent actions" in page and "data-agent-actions" in page
    assert "Still holds" in page and "Retire" in page and "Dismiss" in page


def test_human_lifecycle_routes(client, ks):
    ks.add_fact(T, "x", fact_id="x")
    ks.add_fact(T, "y", fact_id="y")
    ks.set_profile(T, {"open_questions": ["moot?"]})
    # retire needs a reason
    bad = client.post(f"/knowledge/{T}/fact/x/retire?t={TOKEN}", data={}, follow_redirects=False)
    assert bad.status_code == 400
    r = client.post(
        f"/knowledge/{T}/fact/x/retire?t={TOKEN}", data={"reason": "wrong"}, follow_redirects=False
    )
    assert r.status_code in (302, 303)
    fact = ks.fact(T, "x")
    assert fact["standing"] == "retired" and fact["retired_by"] == "user"
    r = client.post(f"/knowledge/{T}/fact/x/restore?t={TOKEN}", follow_redirects=False)
    assert r.status_code in (302, 303)
    fact = ks.fact(T, "x")
    assert fact["standing"] is None and fact["restored_by"] == "user"
    # a correction from the console: recorded, confirmed, executed in one action
    r = client.post(
        f"/knowledge/{T}/fact/x/supersede?t={TOKEN}",
        data={"fact": "the corrected x"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    old = ks.fact(T, "x")
    new = next(f for f in ks.read(T)["facts"] if f["fact"] == "the corrected x")
    assert old["superseded_by"] == new["id"] and new["status"] == "user_confirmed"
    r = client.post(
        f"/knowledge/{T}/resolve?t={TOKEN}",
        data={"fact_a": "y", "fact_b": new["id"], "note": "both"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert new["id"] in ks.fact(T, "y")["compatible_with"]
    r = client.post(
        f"/knowledge/{T}/question/dismiss?t={TOKEN}",
        data={"question": "moot", "reason": "answered elsewhere"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    doc = ks.read(T)
    assert doc["open_questions"] == [] and doc["retired_questions"][0]["by"] == "user"
    r = client.post(f"/knowledge/{T}/reanchor?t={TOKEN}", follow_redirects=False)
    assert r.status_code in (302, 303)
    unknown = client.post(
        f"/knowledge/{T}/fact/nope/retire?t={TOKEN}", data={"reason": "r"}, follow_redirects=False
    )
    assert unknown.status_code == 400


def test_knowledge_tab_tiles_carry_standing_badges(client, ks):
    _seed(ks)
    page = client.get(f"/knowledge?t={TOKEN}").text
    assert "1 stale" in page and "1 contested" in page and "1 agent action" in page
    assert "contested attention agent" in page  # data-tags on the tile
    assert "1 contested" in page.split("<summary><h2>Tables")[1].split("</h2>")[0]


def test_settings_page_shows_the_effective_policy(client):
    page = client.get(f"/settings?t={TOKEN}").text
    assert "data-knowledge-policy" in page and "curate" in page
    assert "retire: agent" in page and "restore: user" in page
