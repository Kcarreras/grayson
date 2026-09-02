from __future__ import annotations

import json

from grayson.executor.snow import (
    classify_failure,
    explain_timeout,
    metadata_query,
    parse_snow_json,
)


def test_parse_flat_rows():
    rows = parse_snow_json(json.dumps([{"A": 1}, {"A": 2}]))
    assert len(rows) == 2


def test_parse_multi_result_sets_takes_last():
    payload = [[{"status": "ok"}], [{"A": 1}, {"A": 2}]]
    rows = parse_snow_json(json.dumps(payload))
    assert rows == [{"A": 1}, {"A": 2}]


def test_parse_drops_status_rows_when_prepended():
    payload = [{"status": "Statement executed successfully."}, {"A": 1}]
    rows = parse_snow_json(json.dumps(payload), drop_status_rows=True)
    assert rows == [{"A": 1}]


def test_parse_empty():
    assert parse_snow_json("") == []
    assert parse_snow_json("[]") == []


def test_classify_auth():
    assert classify_failure("Authentication token has expired. 390114") == "auth_required"
    assert classify_failure("JWT token is invalid") == "auth_required"
    assert classify_failure("Error: connection 'work' not found") == "auth_required"


def test_classify_timeout():
    assert classify_failure("Statement reached its statement or warehouse timeout") == "timeout"


def test_classify_generic():
    assert classify_failure("SQL compilation error: object does not exist") == "error"


def test_metadata_query_groups_by_catalog():
    sql = metadata_query(["DB1.S.A", "DB1.S.B", "DB2.X.C"])
    assert sql.count("INFORMATION_SCHEMA.TABLES") == 2
    assert "DB1.INFORMATION_SCHEMA" in sql and "DB2.INFORMATION_SCHEMA" in sql


def test_metadata_query_rejects_injection_attempts():
    assert metadata_query(["bad'name.s.t"]) is None
    assert metadata_query(["db.s.t' OR '1'='1"]) is None
    assert metadata_query(["not_qualified"]) is None


_SNOW_TIMEOUT = (
    "000630 (57014): Statement reached its statement or warehouse timeout of 300 "
    "second(s) and was canceled."
)


def test_explain_timeout_names_a_lower_warehouse_cap():
    note = explain_timeout(_SNOW_TIMEOUT, 800)
    assert "asked for 800s" in note and "enforced 300s" in note
    assert "warehouse" in note


def test_explain_timeout_owns_the_guard_timeout():
    note = explain_timeout(_SNOW_TIMEOUT, 300)
    assert "session guard's 300s" in note
    assert "grayson session guard" in note


def test_explain_timeout_silent_without_a_number_or_a_guard():
    assert explain_timeout("Statement reached its statement or warehouse timeout", 800) == ""
    assert explain_timeout(_SNOW_TIMEOUT, 0) == ""
