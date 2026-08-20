"""Adversarial guard suite: the guard must be the airtight wall."""

from __future__ import annotations

import pytest

from seekql.config import GuardSettings
from seekql.guard.rules import GuardContext, validate_statement

OPEN = GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0)


def verdict(sql, settings=OPEN, **ctx):
    return validate_statement(sql, settings, GuardContext(**ctx))


# -- allowed statements --------------------------------------------------

ALLOWED = [
    "SELECT 1",
    "SELECT * FROM db.sch.t WHERE id > 5",
    "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
    "SELECT a FROM db.s.t1 UNION ALL SELECT a FROM db.s.t2",
    "SELECT COUNT(*), MAX(ts) FROM db.s.t",
    "SELECT a, COUNT(*) FROM db.s.t GROUP BY a HAVING COUNT(*) > 1",
    "SELECT * FROM db.s.t SAMPLE (10)",
    "SELECT 'semi;colon' AS x",
    "SELECT $1 FROM db.s.t",
    "DESCRIBE TABLE db.sch.t",
    "DESC VIEW db.sch.v",
    "SHOW TABLES IN SCHEMA db.sch",
    "SHOW COLUMNS IN db.sch.t",
    "EXPLAIN SELECT * FROM db.sch.t",
    "SELECT * FROM db.INFORMATION_SCHEMA.TABLES",
    "SELECT a FROM db.s.t QUALIFY ROW_NUMBER() OVER (ORDER BY a) = 1",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    v = verdict(sql)
    assert v.allowed, f"{sql!r} rejected: {v.rule} {v.reason}"


# -- blocked statements --------------------------------------------------

BLOCKED = [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = 1",
    "CREATE TABLE t (a INT)",
    "CREATE TABLE t AS SELECT * FROM x",
    "CREATE OR REPLACE VIEW v AS SELECT 1",
    "CREATE TEMPORARY TABLE tmp AS SELECT 1",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN b INT",
    "ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 1",
    "TRUNCATE TABLE t",
    "GRANT SELECT ON t TO ROLE r",
    "REVOKE SELECT ON t FROM ROLE r",
    "CALL my_proc()",
    "USE DATABASE db",
    "USE ROLE accountadmin",
    "COPY INTO @stage FROM t",
    "COPY INTO t FROM @stage",
    "SET myvar = 1",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "EXPLAIN DROP TABLE t",
    "EXPLAIN USING TABULAR DELETE FROM t",
    "SHOW GRANTS ON TABLE t",  # unsupported SHOW form
    "PUT file:///x @stage",
    "GET @stage file:///x",
    "REMOVE @stage/x",
    "LIST @stage",
    "UNDROP TABLE t",
    "COMMENT ON TABLE t IS 'x'",
]


@pytest.mark.parametrize("sql", BLOCKED)
def test_blocked(sql):
    v = verdict(sql)
    assert not v.allowed, f"{sql!r} should be rejected but passed"


MULTI = [
    "SELECT 1; DROP TABLE t",
    "SELECT 1;DELETE FROM t",
    "SELECT 1;\n-- innocent\nDROP TABLE t",
    "DROP TABLE t; SELECT 1",
    "SELECT 1; SELECT 2",
]


@pytest.mark.parametrize("sql", MULTI)
def test_multi_statement_blocked(sql):
    v = verdict(sql)
    assert not v.allowed
    assert v.rule in {"multi_statement", "statement_type"}


def test_trailing_semicolon_and_comment_ok():
    assert verdict("SELECT 1;").allowed
    assert verdict("SELECT 1; -- trailing comment").allowed


def test_semicolon_inside_string_is_single_statement():
    v = verdict("SELECT 'a;b' AS x")
    assert v.allowed


def test_empty_and_garbage():
    assert not verdict("").allowed
    assert not verdict(";;;").allowed
    assert not verdict("SELEC * FRM t").allowed


# -- auto-limit ----------------------------------------------------------

LIM = GuardSettings(auto_limit=100, timeout_seconds=0, budget_warn=0, budget_cap=0)


def test_limit_injected_on_raw_select():
    v = verdict("SELECT * FROM db.s.t", LIM)
    assert v.allowed and v.injected_limit == 100
    assert "LIMIT 100" in v.executed_sql.upper()


def test_existing_small_limit_kept():
    v = verdict("SELECT * FROM db.s.t LIMIT 5", LIM)
    assert v.injected_limit is None
    assert "LIMIT 5" in v.executed_sql.upper()


def test_oversized_limit_clamped():
    v = verdict("SELECT * FROM db.s.t LIMIT 999999", LIM)
    assert v.injected_limit == 100


def test_aggregate_bypasses_limit():
    v = verdict("SELECT COUNT(*), AVG(x) FROM db.s.t", LIM)
    assert v.aggregate_only and v.injected_limit is None


def test_group_by_bypasses_limit():
    v = verdict("SELECT a, COUNT(*) FROM db.s.t GROUP BY a", LIM)
    assert v.aggregate_only and v.injected_limit is None


def test_window_function_does_not_bypass_limit():
    v = verdict("SELECT id, SUM(x) OVER (PARTITION BY id) FROM db.s.t", LIM)
    assert not v.aggregate_only and v.injected_limit == 100


def test_union_gets_limit():
    v = verdict("SELECT a FROM db.s.t1 UNION ALL SELECT a FROM db.s.t2", LIM)
    assert v.injected_limit == 100


def test_explain_gets_no_limit():
    v = verdict("EXPLAIN SELECT * FROM db.s.t", LIM)
    assert v.allowed and v.injected_limit is None
    assert v.executed_sql.upper().startswith("EXPLAIN")


# -- budget --------------------------------------------------------------


def test_budget_cap_blocks():
    s = GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=3)
    v = verdict("SELECT 1", s, executed_count=3)
    assert not v.allowed and v.rule == "budget_exceeded"


def test_budget_warn_warns():
    s = GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=5, budget_cap=0)
    v = verdict("SELECT 1", s, executed_count=4)
    assert v.allowed and any("budget" in w for w in v.warnings)


# -- scope ---------------------------------------------------------------


def test_in_scope_no_warning():
    v = verdict("SELECT * FROM db.sch.t", scope_tables={"DB.SCH.T"})
    assert v.allowed and not v.warnings


def test_out_of_scope_warns_by_default():
    v = verdict("SELECT * FROM other.sch.t")
    assert v.allowed
    assert any("outside the session scope" in w for w in v.warnings)


def test_out_of_scope_blocks_in_strict_mode():
    v = verdict("SELECT * FROM other.sch.t", strict_scope=True)
    assert not v.allowed and v.rule == "out_of_scope"


def test_glob_scope_allows():
    v = verdict("SELECT * FROM analytics.web.events", allowed_globs=["ANALYTICS.*"])
    assert v.allowed and not v.warnings


def test_information_schema_always_in_scope():
    v = verdict("SELECT * FROM other.INFORMATION_SCHEMA.TABLES", strict_scope=True)
    assert v.allowed


def test_snowflake_account_usage_always_in_scope():
    v = verdict("SELECT * FROM snowflake.account_usage.query_history", strict_scope=True)
    assert v.allowed


def test_cte_names_not_scope_checked():
    v = verdict(
        "WITH helper AS (SELECT 1 AS a) SELECT * FROM helper",
        strict_scope=True,
    )
    assert v.allowed


def test_tables_reported():
    v = verdict("SELECT * FROM db.s.a JOIN db.s.b USING (id)")
    assert set(v.tables) == {"DB.S.A", "DB.S.B"}
