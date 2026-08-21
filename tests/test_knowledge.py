from __future__ import annotations

import pytest

from seekql.knowledge import KnowledgeStore


@pytest.fixture
def ks(workspace):
    return KnowledgeStore(workspace.knowledge_dir)


def test_empty_read(ks):
    doc = ks.read("ANALYTICS.WEB.PAGE_EVENTS")
    assert doc["facts"] == [] and doc["table"] == "ANALYTICS.WEB.PAGE_EVENTS"


def test_add_and_read_fact(ks):
    fact = ks.add_fact(
        "analytics.web.page_events",
        "url_category is assigned by regex; 'other' should be <5%",
        status="data_inferred",
        evidence=["q_0031"],
    )
    assert fact["status"] == "data_inferred"
    doc = ks.read("ANALYTICS.WEB.PAGE_EVENTS")
    assert len(doc["facts"]) == 1
    assert doc["facts"][0]["evidence"] == ["q_0031"]


def test_fact_persists_to_markdown_with_provenance(ks, workspace):
    ks.add_fact("DB.S.T", "ID is a surrogate key", fact_id="id_meaning", status="proposed")
    path = workspace.knowledge_dir / "DB" / "S" / "T.md"
    assert path.is_file()
    text = path.read_text()
    assert "id_meaning" in text and "# DB.S.T" in text


def test_confirm_fact(ks):
    ks.add_fact("DB.S.T", "region_id maps to legacy regions", fact_id="region")
    confirmed = ks.confirm_fact("DB.S.T", "region", by="kane")
    assert confirmed["status"] == "user_confirmed"
    assert confirmed["confirmed_by"] == "kane"
    assert confirmed["confirmed_at"]


def test_confirm_unknown_raises(ks):
    ks.add_fact("DB.S.T", "x", fact_id="a")
    with pytest.raises(KeyError):
        ks.confirm_fact("DB.S.T", "missing")


def test_duplicate_fact_id_rejected(ks):
    ks.add_fact("DB.S.T", "one", fact_id="dup")
    with pytest.raises(ValueError, match="already exists"):
        ks.add_fact("DB.S.T", "two", fact_id="dup")


def test_definition_files(ks):
    doc = ks.set_definition_files("DB.S.T", ["models/t.sql", "models/t_stage.sql"])
    assert doc["definition_files"] == ["models/t.sql", "models/t_stage.sql"]
    assert ks.read("DB.S.T")["definition_files"] == ["models/t.sql", "models/t_stage.sql"]


def test_search(ks):
    ks.add_fact("DB.S.URLS", "url_category fallback is 'other'", fact_id="cat")
    ks.add_fact("DB.S.ORDERS", "order_total excludes tax", fact_id="total")
    hits = ks.search("fallback")
    assert len(hits) == 1 and hits[0]["fact_id"] == "cat"
    assert ks.search("tax")[0]["source"] == "DB.S.ORDERS"


def test_invalid_table_name(ks):
    with pytest.raises(ValueError):
        ks.read("not_qualified")
    with pytest.raises(ValueError):
        ks.add_fact("a.b", "x")


def test_heading_not_captured_as_notes(ks):
    ks.add_fact("DB.S.T", "one", fact_id="a")
    assert ks.read("DB.S.T")["notes"] == ""


def test_repeated_writes_do_not_duplicate_heading(ks, workspace):
    ks.add_fact("DB.S.T", "one", fact_id="a")
    ks.add_fact("DB.S.T", "two", fact_id="b")
    ks.confirm_fact("DB.S.T", "a")
    text = (workspace.knowledge_dir / "DB" / "S" / "T.md").read_text()
    assert text.count("# DB.S.T") == 1


def test_all_tables(ks):
    ks.add_fact("DB.S.A", "x", fact_id="1")
    ks.add_fact("DB.S.B", "y", fact_id="2")
    assert set(ks.all_tables()) == {"DB.S.A", "DB.S.B"}
