"""The canonical relationship shape and the shorthands it absorbs.

Agents wrote join keys in whatever shape the moment suggested, and the schema
map printed them verbatim. Everything here pins the contract that replaces
that: one canonical form on disk, every accepted spelling read into it, and a
clear signal for anything that cannot be."""

from __future__ import annotations

import pytest

from grayson.knowledge.relationships import (
    CARDINALITIES,
    cardinality_ends,
    flip_cardinality,
    for_disk,
    join_label,
    join_text,
    normalize_cardinality,
    normalize_relationship,
    normalize_relationships,
    parse_join,
    qualify_table,
    relationship_issues,
)

T = "DB.S.ORDERS"


@pytest.mark.parametrize(
    ("on", "pairs"),
    [
        ("ORDER_ID", [("ORDER_ID", "ORDER_ID")]),
        (" customer_id ", [("CUSTOMER_ID", "CUSTOMER_ID")]),
        ("PROMO_CODE = CODE", [("PROMO_CODE", "CODE")]),
        ("PROMO_CODE=CODE", [("PROMO_CODE", "CODE")]),
        ("PROMO_CODE -> CODE", [("PROMO_CODE", "CODE")]),
        ("PROMO_CODE → CODE", [("PROMO_CODE", "CODE")]),
        # qualified on both sides, written from either point of view
        ("ORDERS.CUSTOMER_ID = CUSTOMERS.ID", [("CUSTOMER_ID", "ID")]),
        ("CUSTOMERS.ID = ORDERS.CUSTOMER_ID", [("CUSTOMER_ID", "ID")]),
        ("DB.S.CUSTOMERS.ID = DB.S.ORDERS.CUSTOMER_ID", [("CUSTOMER_ID", "ID")]),
        # composite keys, in the three separators people use
        ("ORDER_ID, LINE_NO = LINE", [("ORDER_ID", "ORDER_ID"), ("LINE_NO", "LINE")]),
        ("A = X AND B = Y", [("A", "X"), ("B", "Y")]),
        ("A = X and B", [("A", "X"), ("B", "B")]),
        (["ORDER_ID", "LINE_NO = LINE"], [("ORDER_ID", "ORDER_ID"), ("LINE_NO", "LINE")]),
        ([["A", "X"], ["B"]], [("A", "X"), ("B", "B")]),
        ({"from": "A", "to": "X"}, [("A", "X")]),
        ({"left": "A", "right": "X"}, [("A", "X")]),
        ({"A": "X", "B": "B"}, [("A", "X"), ("B", "B")]),
        ([{"from": "A", "to": "X"}, {"column": "B"}], [("A", "X"), ("B", "B")]),
        ('"Mixed Case" = OTHER', [("Mixed Case", "OTHER")]),
    ],
)
def test_parse_join_reads_every_accepted_shape(on, pairs):
    assert parse_join(on, T, "DB.S.CUSTOMERS") == [{"from": a, "to": b} for a, b in pairs]


@pytest.mark.parametrize(
    "on",
    [
        "lower(email) = lower(EMAIL)",
        "fuzzy match on customer name",
        "A = B = C",
        "ORDER_ID BETWEEN START_ID AND END_ID",
        [42],
        [["A", "B", "C"]],
        {"x": 1},
    ],
)
def test_parse_join_refuses_what_is_not_a_column_equality(on):
    assert parse_join(on, T, "DB.S.CUSTOMERS") is None


def test_parse_join_empty_is_no_key_not_a_failure():
    assert parse_join(None, T, "DB.S.X") == []
    assert parse_join("", T, "DB.S.X") == []
    assert parse_join([], T, "DB.S.X") == []


def test_join_text_and_label_round_trip():
    keys = parse_join("ORDER_ID, LINE_NO = LINE", T, "DB.S.LINES")
    assert join_text(keys) == "ORDER_ID, LINE_NO = LINE"
    assert parse_join(join_text(keys), T, "DB.S.LINES") == keys  # canonical form re-reads
    assert join_label(keys) == "ORDER_ID\nLINE_NO → LINE"  # one pair per line on the canvas
    assert join_label(keys, T, "DB.S.LINES") == "ORDER_ID\nORDERS.LINE_NO = LINES.LINE"


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("one-to-many", "one-to-many"),
        ("one_to_many", "one-to-many"),
        ("One To Many", "one-to-many"),
        ("1:N", "one-to-many"),
        ("1:n", "one-to-many"),
        ("N:1", "many-to-one"),
        ("many:1", "many-to-one"),
        ("*:1", "many-to-one"),
        ("1 -> *", "one-to-many"),
        ("M:N", "many-to-many"),
        ("many-to-many", "many-to-many"),
        ("1:1", "one-to-one"),
        ("  one to one ", "one-to-one"),
        ("lots", "lots"),  # unknown: kept, flagged elsewhere
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_cardinality(raw, canonical):
    assert normalize_cardinality(raw) == canonical


def test_cardinality_helpers():
    assert flip_cardinality("one-to-many") == "many-to-one"
    assert flip_cardinality("many-to-one") == "one-to-many"
    assert flip_cardinality("one-to-one") == "one-to-one"
    assert flip_cardinality("lots") == "lots"
    assert cardinality_ends("many-to-one") == ("many", "one")
    assert cardinality_ends("lots") == ("", "")
    assert all(cardinality_ends(c) != ("", "") for c in CARDINALITIES)


def test_qualify_table_completes_from_the_declaring_table():
    assert qualify_table("customers", T) == "DB.S.CUSTOMERS"
    assert qualify_table("OTHER.CUSTOMERS", T) == "DB.OTHER.CUSTOMERS"
    assert qualify_table("X.Y.Z", T) == "X.Y.Z"
    assert qualify_table(" x.y.z ", None) == "X.Y.Z"
    assert qualify_table("", T) == ""


def test_normalize_relationship_canonical_form_and_aliases():
    rel, warnings = normalize_relationship(
        {"table": "promos", "join": "PROMO_CODE = CODE", "kind": "N:1", "note": " x ", "keep": 1},
        T,
    )
    assert rel == {
        "to": "DB.S.PROMOS",
        "on": "PROMO_CODE = CODE",
        "keys": [{"from": "PROMO_CODE", "to": "CODE"}],
        "cardinality": "many-to-one",
        "note": "x",
        "keep": 1,  # unknown fields round-trip
    }
    assert warnings == ["target 'promos' is not fully qualified; recorded as DB.S.PROMOS"]
    assert for_disk([rel]) == [{k: v for k, v in rel.items() if k != "keys"}]


def test_normalize_relationship_entry_level_column_pairs():
    rel, _ = normalize_relationship(
        {"to": "DB.S.X", "from_column": ["A", "B"], "to_column": ["P", "Q"]}, T
    )
    assert rel["on"] == "A = P, B = Q"
    rel, _ = normalize_relationship({"to": "DB.S.X", "from_column": "A"}, T)
    assert rel["on"] == "A"


def test_normalize_relationship_flags_what_it_cannot_read():
    rel, warnings = normalize_relationship(
        {"to": "DB.S.X", "on": "lower(a) = lower(b)", "cardinality": "lots"}, T
    )
    assert rel["on"] == "lower(a) = lower(b)" and rel["keys"] == []
    assert rel["cardinality"] == "lots"
    assert any("not a column equality" in w for w in warnings)
    assert any("'lots'" in w for w in warnings)
    rel, warnings = normalize_relationship({"to": "DB.S.X"}, T)
    assert rel["on"] == "" and warnings == ["relationship to DB.S.X has no join key ('on')"]


def test_normalize_relationships_drops_the_unreadable_and_says_so():
    rels, warnings = normalize_relationships(["DB.S.A", 42, {"on": "X"}, "", {"to": "DB.S.B"}], T)
    assert [r["to"] for r in rels] == ["DB.S.A", "DB.S.B"]
    assert len([w for w in warnings if "dropped" in w]) == 2
    assert normalize_relationships("not a list", T) == ([], [])


def test_relationship_issues_checks_keys_against_the_column_list():
    doc = {
        "table": T,
        "columns": [{"name": "ORDER_ID"}, {"name": "CUSTOMER_ID"}],
        "relationships": normalize_relationships(
            [
                {"to": "DB.S.CUSTOMERS", "on": "CUSTOMER_ID"},
                {"to": "DB.S.PROMOS", "on": "CODE = PROMO_CODE"},  # sides swapped
                {"to": "DB.S.X", "on": "fuzzy match on name"},
                {"to": "DB.S.Y"},
            ],
            T,
        )[0],
    }
    issues = relationship_issues(doc)
    assert any("CODE" in i and "not in this table's recorded columns" in i for i in issues)
    assert any("free text" in i for i in issues)
    assert any("records no join key" in i for i in issues)
    assert not any("CUSTOMER_ID" in i for i in issues)


def test_unquoted_yaml_on_key_is_read_back():
    """YAML 1.1 reads a bare `on:` as the boolean True — the shape a hand edit
    or an agent writing the file directly produces."""
    import yaml

    loaded = yaml.safe_load("- to: DB.S.CUSTOMERS\n  on: CUSTOMER_ID\n  cardinality: N:1\n")
    assert True in loaded[0]  # the trap is real
    rels, warnings = normalize_relationships(loaded, T)
    assert rels[0]["on"] == "CUSTOMER_ID" and rels[0]["cardinality"] == "many-to-one"
    assert True not in rels[0] and not warnings
