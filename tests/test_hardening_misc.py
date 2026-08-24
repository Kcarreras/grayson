"""Regression tests for non-guard review findings (executor, cache, budget, audit)."""

from __future__ import annotations

import pytest

from conftest import FakeExecutor
from grayson.cache.local import LocalQueryError, query_artifacts
from grayson.cache.store import CacheStore
from grayson.config import GuardSettings
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.executor.snow import ExecutionResult, classify_failure, metadata_query

# -- finding 7: metadata_query identifier anchor -------------------------


def test_metadata_query_rejects_trailing_newline():
    assert metadata_query(["DB.SCHEMA.TAB\n"]) is None
    assert metadata_query(["DB.SCHEMA.\nTAB"]) is None
    assert metadata_query(["DB.SCHEMA.TABLE"]) is not None


# -- finding 8: classify_failure precision -------------------------------


def test_classify_does_not_misfire_on_sso_substring():
    # 'invalid identifier GROSSO' must not be read as auth
    assert classify_failure("SQL compilation error: invalid identifier GROSSO") == "error"


def test_classify_catches_real_connect_failure():
    assert classify_failure("250001 could not connect to snowflake") == "auth_required"


def test_classify_still_detects_expired_token():
    assert classify_failure("Authentication token has expired") == "auth_required"


# -- finding 5: local-analysis DoS guards --------------------------------


@pytest.fixture
def store(tmp_path):
    s = CacheStore(tmp_path / "data")
    s.save("q_0001", [{"ID": i} for i in range(5)], sql="x", source_tables=[], truncated=False)
    return s


def test_local_query_blocks_load_extension(store):
    with pytest.raises(LocalQueryError):
        query_artifacts(store.data_dir, "SELECT load_extension('x') FROM q_0001")


def test_local_query_recursive_cte_times_out(store, monkeypatch):
    import grayson.cache.local as local

    monkeypatch.setattr(local, "LOCAL_TIMEOUT_SECONDS", 1)
    with pytest.raises(LocalQueryError, match="time limit"):
        query_artifacts(
            store.data_dir,
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT max(x) FROM c",
        )


# -- finding 10: budget cap holds under concurrency ----------------------


def test_budget_cap_counts_pending(workspace):
    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=2),
        guard_profile="moderate",
    )
    ex = FakeExecutor()
    assert run_statement(s, "SELECT * FROM DB.S.T1", executor=ex)["status"] == "executed"
    assert run_statement(s, "SELECT * FROM DB.S.T1", executor=ex)["status"] == "executed"
    # third exceeds cap of 2
    assert run_statement(s, "SELECT * FROM DB.S.T1", executor=ex)["status"] == "rejected"


def test_budget_counts_inflight_pending_row(workspace):
    # A leftover 'pending' row (an in-flight concurrent query) consumes budget.
    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=2),
        guard_profile="moderate",
    )
    s.allocate_qid("w-other", "SELECT 1")  # simulates another worker mid-flight
    ex = FakeExecutor()
    assert run_statement(s, "SELECT * FROM DB.S.T1", executor=ex)["status"] == "executed"
    # now 2 consumed (1 pending + 1 executed); next is blocked
    assert run_statement(s, "SELECT * FROM DB.S.T1", executor=ex)["status"] == "rejected"


# -- finding 6: audit row never stranded on executor raise ---------------


class RaisingExecutor:
    def execute(self, sql: str, timeout_seconds: int = 0) -> ExecutionResult:
        raise RuntimeError("connector blew up")


def test_executor_exception_marks_row_error_not_pending(workspace):
    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    out = run_statement(s, "SELECT * FROM DB.S.T1", executor=RaisingExecutor())
    assert out["status"] == "error"
    row = s.query_row(out["qid"])
    assert row["status"] == "error"
    assert "connector blew up" in row["error"]
    # no row left pending
    assert all(r["status"] != "pending" for r in s.query_log())
