"""A mangled knowledge doc (merge conflict, bad YAML) must degrade, not 500."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from grayson.knowledge import KnowledgeDocError, KnowledgeStore
from grayson.ui.server import build_app

TOKEN = "test-token"

CONFLICTED = """\
---
table: DB.S.ORDERS
grain: one row per ORDER_ID
<<<<<<< HEAD
freshness: hourly
=======
freshness: daily
>>>>>>> main
facts: []
---

# DB.S.ORDERS
"""


@pytest.fixture
def store(workspace):
    s = KnowledgeStore(workspace.knowledge_dir)
    s.add_fact("DB.S.CUSTOMERS", "one row per customer", status="data_inferred")
    bad = workspace.knowledge_dir / "DB" / "S" / "ORDERS.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(CONFLICTED, encoding="utf-8")
    return s


def test_read_raises_knowledge_doc_error_naming_the_file(store):
    with pytest.raises(KnowledgeDocError) as e:
        store.read("DB.S.ORDERS")
    assert "ORDERS.md" in str(e.value)
    # a healthy doc still reads fine
    assert store.read("DB.S.CUSTOMERS")["facts"]


def test_knowledge_pages_survive_a_broken_doc(workspace, store):
    client = TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")
    # list page renders, marks the broken doc, keeps the healthy one clickable
    r = client.get(f"/knowledge?t={TOKEN}")
    assert r.status_code == 200
    assert "unreadable" in r.text and "ORDERS.md" in r.text
    assert "DB.S.CUSTOMERS" in r.text
    # healthy table page renders (the relationship canvas skips the broken doc)
    assert client.get(f"/knowledge/DB.S.CUSTOMERS?t={TOKEN}").status_code == 200
    # the broken table's own page reports the parse error, not a 500
    broken = client.get(f"/knowledge/DB.S.ORDERS?t={TOKEN}")
    assert broken.status_code == 400
    assert "ORDERS.md" in broken.text
