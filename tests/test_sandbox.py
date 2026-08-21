"""Sandbox: end-to-end sessions against the local mock warehouse (no snow CLI)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from seekql.cli import app
from seekql.sandbox.executor import SandboxExecutor, sandbox_db_path
from seekql.sandbox.seed import seed_sandbox

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def sandbox_ws(tmp_path, monkeypatch):
    out = invoke("sandbox", "init", str(tmp_path / "demo"))
    monkeypatch.chdir(tmp_path / "demo")
    return out


def test_init_scaffolds_workspace_and_answer_key(sandbox_ws, tmp_path):
    root = tmp_path / "demo"
    assert (root / "seekql.toml").is_file()
    assert (root / "SANDBOX_ANSWER_KEY.md").is_file()
    assert sandbox_db_path(root).is_file()
    key = (root / "SANDBOX_ANSWER_KEY.md").read_text(encoding="utf-8")
    assert "table-health" in key and "bug-hunter" in key and "migration-parity" in key


def test_doctor_checks_sandbox_not_snow(sandbox_ws):
    out = invoke("doctor")
    checks = {c["check"]: c for c in out["checks"]}
    assert checks["sandbox_warehouse"]["ok"] is True
    assert "snow_cli" not in checks


def test_session_and_metadata_snapshot(sandbox_ws):
    out = invoke(
        "session", "start", "--workflow", "table-health", "--table", "SANDBOX.SHOP.CUSTOMERS"
    )
    assert out["session"]["connection"] == "sandbox"
    assert out["metadata_snapshot"]["status"] == "ok"
    assert "SANDBOX.SHOP.CUSTOMERS" in out["metadata_snapshot"]["tables"]


def test_planted_problems_are_findable(sandbox_ws, tmp_path):
    truth = seed_sandbox(sandbox_db_path(tmp_path / "demo"))  # re-seed, get ground truth
    sid = invoke(
        "session", "start", "--workflow", "table-health", "--table", "SANDBOX.SHOP.CUSTOMERS"
    )["session"]["id"]

    # email NULL regression: count matches ground truth, all after the cutoff
    nulls = invoke(
        "query",
        "run",
        sid,
        "-q",
        "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.CUSTOMERS WHERE EMAIL IS NULL",
    )
    assert nulls["status"] == "executed"
    assert nulls["preview"][0]["N"] == truth["customers"]["null_email_count"]
    before_cutoff = invoke(
        "query",
        "run",
        sid,
        "-q",
        "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.CUSTOMERS "
        f"WHERE EMAIL IS NULL AND SIGNUP_DATE < '{truth['customers']['null_email_cutoff']}'",
    )
    assert before_cutoff["preview"][0]["N"] == 0

    # duplicate customer ids
    dups = invoke(
        "query",
        "run",
        sid,
        "-q",
        "SELECT CUSTOMER_ID FROM SANDBOX.SHOP.CUSTOMERS "
        "GROUP BY CUSTOMER_ID HAVING COUNT(*) > 1 ORDER BY CUSTOMER_ID",
    )
    assert [r["CUSTOMER_ID"] for r in dups["preview"]] == (
        truth["customers"]["duplicate_customer_ids"]
    )


def test_join_fanout_problem(sandbox_ws, tmp_path):
    truth = seed_sandbox(sandbox_db_path(tmp_path / "demo"))
    sid = invoke(
        "session",
        "start",
        "--workflow",
        "bug-hunter",
        "--table",
        "SANDBOX.SHOP.ORDERS_ENRICHED",
    )["session"]["id"]
    counts = invoke(
        "query",
        "run",
        sid,
        "-q",
        "SELECT (SELECT COUNT(*) FROM SANDBOX.SHOP.ORDERS_ENRICHED) AS ENRICHED, "
        "(SELECT COUNT(*) FROM SANDBOX.SHOP.ORDERS) AS ORDERS",
    )
    row = counts["preview"][0]
    assert row["ENRICHED"] - row["ORDERS"] == truth["orders_enriched"]["extra_rows"]
    assert truth["orders_enriched"]["extra_rows"] > 0


def test_migration_parity_problem(sandbox_ws, tmp_path):
    truth = seed_sandbox(sandbox_db_path(tmp_path / "demo"))
    sid = invoke(
        "session",
        "start",
        "--workflow",
        "migration-parity",
        "--table",
        "SANDBOX.SHOP.PAYMENTS",
        "--table",
        "SANDBOX.SHOP.PAYMENTS_V2",
    )["session"]["id"]
    missing = invoke(
        "query",
        "run",
        sid,
        "-q",
        "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.PAYMENTS p "
        "LEFT JOIN SANDBOX.SHOP.PAYMENTS_V2 v ON p.PAYMENT_ID = v.PAYMENT_ID "
        "WHERE v.PAYMENT_ID IS NULL",
    )
    assert missing["preview"][0]["N"] == truth["payments"]["missing_refunded_rows"]
    drift = invoke(
        "query",
        "run",
        sid,
        "-q",
        "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.PAYMENTS p "
        "JOIN SANDBOX.SHOP.PAYMENTS_V2 v ON p.PAYMENT_ID = v.PAYMENT_ID "
        "WHERE p.AMOUNT != v.AMOUNT",
    )
    assert drift["preview"][0]["N"] == truth["payments"]["eur_amount_mismatches"]


def test_guard_still_applies_in_sandbox(sandbox_ws):
    sid = invoke(
        "session", "start", "--workflow", "table-health", "--table", "SANDBOX.SHOP.CUSTOMERS"
    )["session"]["id"]
    out = invoke("query", "run", sid, "-q", "DELETE FROM SANDBOX.SHOP.CUSTOMERS")
    assert out["status"] == "rejected"


def test_describe_and_show_tables(sandbox_ws):
    sid = invoke(
        "session", "start", "--workflow", "table-health", "--table", "SANDBOX.SHOP.CUSTOMERS"
    )["session"]["id"]
    desc = invoke("query", "run", sid, "-q", "DESCRIBE TABLE SANDBOX.SHOP.CUSTOMERS")
    names = {r["name"] for r in desc["preview"]}
    assert {"CUSTOMER_ID", "EMAIL", "SIGNUP_DATE"} <= names
    show = invoke("query", "run", sid, "-q", "SHOW TABLES")
    assert any(r["name"] == "CUSTOMERS" for r in show["preview"])


def test_seed_is_deterministic(tmp_path):
    t1 = seed_sandbox(tmp_path / "a.db")
    t2 = seed_sandbox(tmp_path / "b.db")
    assert t1 == t2


def test_init_into_uncreatable_path_fails_cleanly(tmp_path):
    # parent is a regular file, so mkdir raises OSError — expect a clean JSON
    # error and exit 1, not a traceback (a fresh user running from a protected
    # cwd like C:\Windows\system32 hits the same path via PermissionError)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    for cmd in (["sandbox", "init"], ["init"]):
        result = runner.invoke(app, [*cmd, str(blocker / "demo")])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert json.loads(result.output)["error"]


def test_executor_errors_without_db(tmp_path):
    result = SandboxExecutor(tmp_path / "missing.db").execute("SELECT 1")
    assert result.status == "error"
    assert "sandbox init" in result.error
