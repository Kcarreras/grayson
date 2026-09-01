from __future__ import annotations

import pytest

from grayson.knowledge import KnowledgeStore


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


# -- format stability ------------------------------------------------------


def test_write_stamps_current_format(ks, workspace):
    from grayson.knowledge import KNOWLEDGE_FORMAT

    ks.add_fact("DB.S.T", "ID is a surrogate key", fact_id="id_meaning")
    text = (workspace.knowledge_dir / "DB" / "S" / "T.md").read_text()
    assert f"format: {KNOWLEDGE_FORMAT}" in text


def test_unknown_fields_round_trip_through_a_rewrite(ks, workspace):
    """A doc enriched by a newer grayson (or a hand edit) must survive a rewrite
    by this one: unknown frontmatter keys and fact fields are preserved, not
    silently stripped — the mixed-version-team guarantee."""
    path = workspace.knowledge_dir / "DB" / "S" / "T.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "table: DB.S.T\n"
        "domain: finance\n"
        "facts:\n"
        "- id: f1\n"
        "  fact: amounts are gross\n"
        "  status: proposed\n"
        "  weight: 3\n"
        "---\n\n# DB.S.T\n",
        encoding="utf-8",
    )
    doc = ks.read("DB.S.T")
    assert doc["extra"] == {"domain": "finance"}
    assert doc["facts"][0]["weight"] == 3
    ks.add_fact("DB.S.T", "region_id maps to legacy regions", fact_id="region")
    text = path.read_text()
    assert "domain: finance" in text
    assert "weight: 3" in text
    assert len(ks.read("DB.S.T")["facts"]) == 2


def test_newer_format_reads_best_effort_but_refuses_rewrite(ks, workspace):
    """Visible refusal beats silent loss: this version writes only the fields it
    defines, so rewriting a newer doc would discard what that format added."""
    from grayson.knowledge import KnowledgeDocError

    path = workspace.knowledge_dir / "DB" / "S" / "T.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "---\n"
        "table: DB.S.T\n"
        "format: 99\n"
        "facts:\n"
        "- id: f1\n"
        "  fact: amounts are gross\n"
        "  status: proposed\n"
        "---\n\n# DB.S.T\n"
    )
    path.write_text(original, encoding="utf-8")
    assert ks.read("DB.S.T")["facts"][0]["id"] == "f1"  # best-effort read still works
    with pytest.raises(KnowledgeDocError, match="refusing to rewrite"):
        ks.add_fact("DB.S.T", "another fact")
    assert path.read_text() == original  # untouched


def test_unstamped_docs_read_as_format_one(ks, workspace):
    path = workspace.knowledge_dir / "DB" / "S" / "T.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntable: DB.S.T\nfacts: []\n---\n\n# DB.S.T\n", encoding="utf-8")
    assert ks.read("DB.S.T")["format"] == 1


def test_garbage_format_key_is_a_named_doc_error(ks, workspace):
    from grayson.knowledge import KnowledgeDocError

    path = workspace.knowledge_dir / "DB" / "S" / "T.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntable: DB.S.T\nformat: banana\n---\n", encoding="utf-8")
    with pytest.raises(KnowledgeDocError, match="format"):
        ks.read("DB.S.T")


def test_answer_open_question(ks):
    ks.set_profile("DB.S.T1", {"open_questions": ["What is the grain?", "Who owns loads?"]})
    result = ks.answer_open_question("DB.S.T1", "what is the grain?", "one row per order")
    assert result["question"] == "What is the grain?"
    assert result["open_questions_left"] == 1
    doc = ks.read("DB.S.T1")
    assert doc["open_questions"] == ["Who owns loads?"]
    fact = doc["facts"][0]
    # the fact reads standalone: question and answer together
    assert fact["fact"] == "What is the grain? — one row per order"
    assert fact["status"] == "proposed"  # relayed, not confirmed


def test_answer_open_question_substring_match(ks):
    ks.set_profile("DB.S.T1", {"open_questions": ["Is AMOUNT gross or net of refunds?"]})
    result = ks.answer_open_question("DB.S.T1", "gross or net", "gross — refunds land separately")
    assert result["question"] == "Is AMOUNT gross or net of refunds?"
    assert ks.read("DB.S.T1")["open_questions"] == []


def test_answer_open_question_ambiguous_or_missing(ks):
    ks.set_profile(
        "DB.S.T1",
        {"open_questions": ["Is the grain daily?", "Is the grain hourly upstream?"]},
    )
    with pytest.raises(ValueError, match="matches 2 open questions"):
        ks.answer_open_question("DB.S.T1", "grain", "yes")
    with pytest.raises(KeyError, match="no open question"):
        ks.answer_open_question("DB.S.T1", "refunds", "n/a")
    assert len(ks.read("DB.S.T1")["open_questions"]) == 2  # nothing changed
