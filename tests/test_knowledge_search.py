"""Library discovery shares normalized reads and preserves actionable search context."""

from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from grayson.knowledge import KnowledgeStore
from grayson.ui.server import build_app

TABLE = "SHOP.PUBLIC.ORDERS"
TOKEN = "search-test"


@pytest.fixture
def store(workspace):
    store = KnowledgeStore(workspace.knowledge_dir)
    store.set_profile(
        TABLE,
        {
            "grain": "One row per checkout",
            "freshness": "Refreshed hourly",
            "owners": ["Commerce Analytics"],
            "columns": [
                {"name": "ID", "type": "NUMBER", "description": "Purchase identifier"},
                {"name": "AMOUNT", "description": "Gross value before refunds"},
            ],
            "relationships": [{"to": "SHOP.PUBLIC.CUSTOMERS", "on": "ID", "cardinality": "N:1"}],
            "definitions": [{"path": "models/transactions.sql", "repo": "example.com/warehouse"}],
            "open_questions": ["How are cancellations handled?"],
        },
    )
    snapshot = store.write_snapshot(TABLE, "ddl", "create table ORDERS (ID NUMBER);")
    store.upsert_definition(TABLE, {"kind": "ddl", **snapshot})
    store.add_fact(TABLE, "AMOUNT includes tax", fact_id="tax")
    store.confirm_fact(TABLE, "tax")
    store.add_fact(TABLE, "Legacy receipts are excluded", fact_id="old")
    store.retire_fact(TABLE, "old", reason="The backfill is complete", by="user")
    doc = store.read(TABLE)
    doc["notes"] = "Backfill completed in May.\nKeep the original timestamps."
    store.save(TABLE, doc)
    return store


@pytest.mark.parametrize(
    ("query", "kind"),
    [
        ("  orders  ", "table"),
        ("CHECKOUT", "grain"),
        ("hourly", "freshness"),
        ("commerce analytics", "owners"),
        ("purchase identifier", "column"),
        ("number", "column"),
        ("customers", "relationship"),
        ("many-to-one", "relationship"),
        ("transactions.sql", "definition"),
        ("orders.ddl.sql", "definition"),
        ("cancellations", "question"),
        ("original timestamps", "notes"),
        ("includes tax", "fact"),
    ],
)
def test_search_table_context_without_rewriting_files(store, query, kind):
    path = store.table_path(TABLE)
    before = path.read_bytes()
    hits = store.search(query)
    assert any(h["source"] == TABLE and h["kind"] == kind for h in hits)
    assert path.read_bytes() == before


def test_search_keeps_fact_contract_and_ignores_empty_queries(store):
    hit = store.search("includes tax")[0]
    assert hit["fact_id"] == "tax"
    assert hit["fact"] == "AMOUNT includes tax"
    assert hit["status"] == "user_confirmed"
    assert store.search("") == store.search(" \n ") == []


@pytest.mark.parametrize(
    "broken",
    [
        b"---\n- not a mapping\n---\n",
        b"---\nfacts: [plain-text]\n---\n",
        b"---\nfacts: {bad: shape}\n---\n",
        b"---\nfacts: [\n---\n",
        b"---\nfacts: []\n",
        b"\xff\xfe",
    ],
)
def test_broken_neighbors_do_not_break_search_or_console(workspace, store, broken):
    store.table_path("SHOP.PUBLIC.BROKEN").write_bytes(broken)
    assert store.search("tax")[0]["fact_id"] == "tax"
    client = TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")
    page = client.get("/knowledge", params={"t": TOKEN, "q": "tax"})
    assert page.status_code == 200 and "AMOUNT includes tax" in page.text
    page = client.get("/knowledge", params={"t": TOKEN})
    assert page.status_code == 200 and 'data-tags="error"' in page.text


def test_search_uses_table_paths_and_does_not_index_scaffold_files(store):
    doc = store.read(TABLE)
    doc["table"] = "SHOP.PUBLIC.MOVED"
    store.save(TABLE, doc)
    (store.dir / "README.md").write_text("---\nfacts: [bad]\n---\n", encoding="utf-8")
    (store.dir / "glossary.md").write_text("Chargeback: a reversed card payment", encoding="utf-8")
    assert store.search("tax")[0]["source"] == TABLE
    assert any(hit["kind"] == "table" for hit in store.search("orders"))
    assert store.search("chargeback") == [{"source": "glossary", "match": "glossary.md"}]


class PageLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = set()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "a" and "href" in attrs:
            self.links.append(attrs["href"])


@pytest.mark.parametrize(
    ("query", "anchor"),
    [
        ("refunds", "column-AMOUNT"),
        ("includes tax", "fact-tax"),
        ("legacy receipts", "fact-old"),
        ("commerce analytics", "overview"),
        ("transactions.sql", "definitions"),
        ("customers", "relationships"),
        ("many-to-one", "relationships"),
        ("orders.ddl.sql", "definitions"),
        ("cancellations", "questions"),
        ("original timestamps", "notes"),
    ],
)
def test_console_search_links_to_existing_target(workspace, store, query, anchor):
    client = TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")
    page = client.get("/knowledge", params={"t": TOKEN, "q": query})
    assert page.status_code == 200
    link = next(
        link for link in PageLinks(page.text).links if unquote(urlsplit(link).fragment) == anchor
    )
    target = client.get(link)
    assert target.status_code == 200
    assert anchor in PageLinks(target.text).ids


def test_console_matches_show_computed_standing_and_escape_content(workspace, store):
    store.sync_columns(TABLE, [{"name": "ID", "type": "NUMBER"}])
    store.add_fact(TABLE, '<script>alert("example")</script>', fact_id="markup")
    client = TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")
    page = client.get("/knowledge", params={"t": TOKEN, "q": "includes tax"}).text
    assert "column AMOUNT was dropped" in page
    assert ">stale</span>" in page and ">user confirmed</span>" in page
    page = client.get("/knowledge", params={"t": TOKEN, "q": "legacy receipts"}).text
    assert ">retired</span>" in page and "The backfill is complete" in page
    page = client.get("/knowledge", params={"t": TOKEN, "q": "<script>"}).text
    assert '<script>alert("example")</script>' not in page
    assert "&lt;script&gt;alert" in page


def test_filtered_library_keeps_the_complete_schema_map(workspace, store):
    store.set_profile("SHOP.PUBLIC.CUSTOMERS", {"grain": "One row per customer"})
    client = TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")
    page = client.get("/knowledge", params={"t": TOKEN, "q": "refunds"}).text
    assert "2 tables · 1 relationships" in page
    assert 'data-s-name="SHOP.PUBLIC.ORDERS"' in page
    assert 'data-s-name="SHOP.PUBLIC.CUSTOMERS"' not in page
