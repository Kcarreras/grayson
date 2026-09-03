"""Profiling: the descriptive battery as citable evidence, cheaply."""

from __future__ import annotations

import pytest

from conftest import FakeExecutor, close_checkpoint
from grayson.config import GuardSettings
from grayson.core import engine
from grayson.core.session import Session
from grayson.profile import correlations, plan, profile_table, summarize
from grayson.profile.plan import Column, ProfilePlanError


@pytest.fixture
def session(workspace):
    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    return s


# -- planning: wide, not narrow -------------------------------------------


def _cols(*specs: tuple[str, str]) -> list[Column]:
    return [Column(name=n, raw_type=t) for n, t in specs]


def test_one_statement_covers_every_column():
    """The whole point: 40 columns is one query, not 160."""
    columns = _cols(*[(f"C{i}", "NUMBER") for i in range(40)])
    sql, alias_map = plan.aggregate_sql("DB.S.T1", columns)
    assert sql.count("SELECT") == 1
    assert len({c for c, _ in alias_map.values()}) == 40
    assert len(plan.batches(columns)) == 1


def test_very_wide_tables_batch_rather_than_emit_one_monster():
    columns = _cols(*[(f"C{i}", "NUMBER") for i in range(101)])
    assert len(plan.batches(columns)) == 3


def test_statistics_match_the_column_kind():
    _, aliases = plan.aggregate_sql(
        "DB.S.T1", _cols(("N", "NUMBER"), ("T", "VARCHAR"), ("D", "DATE"), ("V", "VARIANT"))
    )
    by_column: dict[str, set[str]] = {}
    for column, stat in aliases.values():
        by_column.setdefault(column, set()).add(stat)
    assert "avg" in by_column["N"]
    assert "max_length" in by_column["T"]
    # text still gets a value range: dates stored as VARCHAR are how impossible
    # dates hide, and a lexicographic max exposes them
    assert "max" in by_column["T"]
    assert by_column["D"] >= {"min", "max"}
    # MIN/MAX over semi-structured data means nothing, so it is not asked for
    assert by_column["V"] == {"non_null"}


def test_generated_sql_refuses_hostile_identifiers():
    with pytest.raises(ProfilePlanError):
        plan.aggregate_sql("DB.S.T1", _cols(('X" ; DROP TABLE T --', "VARCHAR")))
    with pytest.raises(ProfilePlanError):
        plan.qualify("DB.S.T1; SELECT 1")


def test_describe_parsing_is_case_insensitive():
    columns = plan.parse_describe(
        [{"name": "A", "type": "NUMBER"}, {"NAME": "B", "TYPE": "VARCHAR"}]
    )
    assert [(c.name, c.kind) for c in columns] == [("A", "numeric"), ("B", "text")]


# -- running: every statement is evidence ---------------------------------


def test_profile_runs_few_queries_and_returns_citable_ids(session):
    doc = profile_table(session, "DB.S.T1", executor=FakeExecutor())
    assert doc["queries_run"] <= 5, "profiling should cost a handful of queries, not dozens"
    executed = session.executed_qids()
    assert set(doc["evidence"]) <= executed, "every profile query must be real evidence"
    # and they close checkpoints like any other query
    close_checkpoint(session, "null_completeness", doc["evidence"], "profiled")
    assert session.checkpoint("null_completeness")["status"] == "complete"


def test_profile_reports_per_column_facts(session):
    doc = profile_table(session, "DB.S.T1", executor=FakeExecutor())
    assert doc["columns"], "profile returned no columns"
    for col in doc["columns"]:
        assert "column" in col and "kind" in col


def test_profile_failure_is_loud_not_partial(session):
    class Refusing(FakeExecutor):
        def execute(self, sql, timeout_seconds=0):
            result = super().execute(sql, timeout_seconds)
            result.ok = False
            result.status = "error"
            result.error = "boom"
            return result

    from grayson.profile import ProfileError

    with pytest.raises(ProfileError, match="did not execute"):
        profile_table(session, "DB.S.T1", executor=Refusing())


# -- local statistics: honest about what they are -------------------------


def test_summaries_cover_numeric_columns_only():
    columns = ["N", "T"]
    rows = [(float(i), f"row{i}") for i in range(50)]
    out = summarize(columns, rows)
    assert [s["column"] for s in out] == ["N"]
    assert out[0]["n"] == 50
    assert out[0]["quantiles"]["p50"] == pytest.approx(24.5)
    assert out[0]["computed"] == "local"


def test_perfect_correlation_is_found_and_flagged_notable():
    rows = [(float(i), float(i) * 2 + 1, float(i % 3)) for i in range(100)]
    out = correlations(["A", "B", "C"], rows)
    top = out["pairs"][0]
    assert set(top["columns"]) == {"A", "B"}
    assert top["r"] == pytest.approx(1.0)
    assert out["notable"], "a perfect correlation should be called out"


def test_correlation_carries_its_own_caveat_and_ceiling():
    """A correlation looks like a measurement of the table but is arithmetic over
    a cached sample — the response has to say so, because a finding will cite it."""
    rows = [(float(i), float(i) * 2) for i in range(100)]
    out = correlations(["A", "B"], rows)
    assert out["computed"] == "local"
    assert out["confidence_ceiling"] == "medium"
    assert "not by the warehouse" in out["caveat"]


def test_too_few_pairs_is_skipped_not_reported_as_a_number():
    rows = [(float(i), float(i)) for i in range(5)]
    out = correlations(["A", "B"], rows)
    assert out["pairs"] == []
    assert out["skipped"][0]["usable_pairs"] == 5


def test_constant_columns_correlate_with_nothing():
    rows = [(1.0, float(i)) for i in range(100)]
    out = correlations(["CONST", "B"], rows)
    assert out["pairs"] == []
    assert "constant" in out["skipped"][0]["reason"]


def test_pairs_use_their_own_usable_rows():
    """Listwise deletion per pair, not per row — one sparse column should not
    shrink every other pair's sample."""
    rows = [(float(i), float(i), None if i < 60 else float(i)) for i in range(100)]
    out = correlations(["A", "B", "SPARSE"], rows)
    by_pair = {tuple(sorted(p["columns"])): p["n"] for p in out["pairs"]}
    assert by_pair[("A", "B")] == 100
    assert by_pair[("A", "SPARSE")] == 40


def test_spearman_catches_monotone_but_nonlinear():
    rows = [(float(i), float(i) ** 3) for i in range(1, 100)]
    assert correlations(["A", "B"], rows, "spearman")["pairs"][0]["r"] == pytest.approx(1.0)


def test_unknown_method_rejected():
    with pytest.raises(ValueError, match="pearson or spearman"):
        correlations(["A"], [(1.0,)], "kendall")


def test_oddly_named_columns_are_skipped_not_fatal(session):
    """One quoted-identifier column ("order id") must not sink the battery —
    it is skipped and said so, and every other column still profiles."""
    from grayson.executor.snow import ExecutionResult

    class OddDescribe(FakeExecutor):
        def execute(self, sql, timeout_seconds=0):
            if sql.strip().upper().startswith(("DESCRIBE", "DESC ")):
                rows = [
                    {"name": "order id", "type": "NUMBER", "kind": "COLUMN"},
                    {"name": "VAL", "type": "VARCHAR", "kind": "COLUMN"},
                ]
                return ExecutionResult(
                    status="ok", rows=rows, columns=list(rows[0].keys()), duration_ms=5
                )
            return super().execute(sql, timeout_seconds)

    doc = profile_table(session, "DB.S.T1", executor=OddDescribe())
    assert doc["columns_skipped"] == ["order id"]
    assert [c["column"] for c in doc["columns"]] == ["VAL"]


def test_duplicate_column_names_are_skipped_not_miscomputed():
    """`columns.index` binds a duplicated name to its first occurrence, which
    would present a confidently wrong r — ambiguous names sit out instead."""
    rows = [(float(i), float(100 - i), float(i), float(i) * 2) for i in range(60)]
    out = correlations(["X", "X", "Y", "Z"], rows)
    assert {tuple(sorted(p["columns"])) for p in out["pairs"]} == {("Y", "Z")}
    assert out["columns_considered"] == ["Y", "Z"]
    assert any("duplicate" in s.get("reason", "") for s in out["skipped"])
