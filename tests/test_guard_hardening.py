"""Regression tests for the adversarial security review findings (Phase 1)."""

from __future__ import annotations

import pytest

from grayson.config import GuardSettings
from grayson.guard.rules import GuardContext, validate_statement

OPEN = GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0)


def v(sql, **ctx):
    return validate_statement(sql, OPEN, GuardContext(**ctx))


# -- SYSTEM$ side-effect functions (finding 1) ---------------------------

SYSTEM_FUNCS = [
    "SELECT SYSTEM$ABORT_SESSION(999)",
    "SELECT SYSTEM$CANCEL_QUERY('x')",
    "SELECT SYSTEM$CANCEL_ALL_QUERIES(1)",
    "SELECT SYSTEM$ABORT_TRANSACTION(1)",
    "SELECT SYSTEM$WAIT(60)",
    "SELECT SYSTEM$WAIT(60) FROM db.s.t",
]


@pytest.mark.parametrize("sql", SYSTEM_FUNCS)
def test_system_functions_blocked(sql):
    verdict = v(sql)
    assert not verdict.allowed and verdict.rule == "denied_function"


def test_system_function_blocked_even_in_subquery():
    assert not v("SELECT * FROM db.s.t WHERE 1 = SYSTEM$WAIT(1)").allowed


# -- RESULT_SCAN scope bypass (finding 4) --------------------------------

RESULT_SCANS = [
    "SELECT * FROM TABLE(RESULT_SCAN('01b0-any-query-id'))",
    "SELECT * FROM RESULT_SCAN('01b0')",
    "SELECT * FROM TABLE(RESULT_SCAN(LAST_QUERY_ID()))",
]


@pytest.mark.parametrize("sql", RESULT_SCANS)
def test_result_scan_blocked(sql):
    assert not v(sql).allowed


# -- single-part unqualified table in strict mode (finding 2) ------------


def test_unqualified_table_blocked_in_strict_mode():
    verdict = v("SELECT * FROM SECRETS", strict_scope=True, scope_tables={"DB.S.T"})
    assert not verdict.allowed and verdict.rule == "out_of_scope"


def test_unqualified_join_blocked_in_strict_mode():
    verdict = v(
        "SELECT * FROM CUSTOMERS c JOIN ACCOUNTS a ON c.id = a.id",
        strict_scope=True,
        scope_tables={"DB.S.T"},
    )
    assert not verdict.allowed and verdict.rule == "out_of_scope"


def test_unqualified_table_still_warns_in_non_strict():
    verdict = v("SELECT * FROM SECRETS")
    assert verdict.allowed and any("unqualified" in w for w in verdict.warnings)


def test_in_scope_unqualified_is_noise_free_when_registered():
    # if the bare name is registered exactly, it is in scope
    verdict = v("SELECT * FROM T", strict_scope=True, scope_tables={"T"})
    assert verdict.allowed


# -- UDTF table-function scope bypass (finding 3) ------------------------


def test_udtf_table_source_blocked_in_strict_mode():
    verdict = v("SELECT * FROM TABLE(my_udtf(1))", strict_scope=True, scope_tables={"DB.S.T"})
    assert not verdict.allowed and verdict.rule == "table_function_scope"


def test_udtf_table_source_warns_in_non_strict():
    verdict = v("SELECT * FROM TABLE(my_udtf(1))")
    assert verdict.allowed and any("table function" in w for w in verdict.warnings)


def test_builtin_table_functions_allowed():
    # GENERATOR / FLATTEN are safe built-ins, not UDTFs
    assert v("SELECT * FROM TABLE(GENERATOR(ROWCOUNT => 10))", strict_scope=True).allowed
    assert v(
        "SELECT * FROM db.s.t, LATERAL FLATTEN(input => t.arr)",
        strict_scope=True,
        scope_tables={"DB.S.T"},
    ).allowed


# -- regular scalar functions must still work ----------------------------


def test_ordinary_scalar_functions_allowed():
    for sql in [
        "SELECT PARSE_JSON(col) FROM db.s.t",
        "SELECT REGEXP_SUBSTR(url, '[a-z]+') FROM db.s.t",
        "SELECT DATEADD(day, 1, ts) FROM db.s.t",
        "SELECT COALESCE(a, b) FROM db.s.t",
    ]:
        assert v(sql, scope_tables={"DB.S.T"}).allowed, sql
