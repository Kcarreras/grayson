"""Sandbox scoring: findings against the planted truth, deterministically."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from grayson import cli
from grayson.cli import app
from grayson.core import engine
from grayson.core.session import Session
from grayson.sandbox.executor import sandbox_db_path
from grayson.sandbox.score import (
    PLANTED,
    _numbers,
    ground_truth,
    problems_for,
    render_leaderboard,
    render_score,
    score_session,
)
from grayson.sandbox.seed import seed_sandbox
from grayson.workspace import Workspace

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_warehouse_store(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAYSON_SANDBOX_DIR", str(tmp_path / "wh-store"))


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def sandbox_ws(tmp_path, monkeypatch) -> Workspace:
    invoke("sandbox", "init", str(tmp_path / "demo"))
    monkeypatch.chdir(tmp_path / "demo")
    return Workspace.find()


def _start(workflow: str, *tables: str) -> str:
    args = ["session", "start", "--workflow", workflow]
    for t in tables:
        args += ["--table", t]
    return invoke(*args)["session"]["id"]


def _query(sid: str, sql: str) -> str:
    out = invoke("query", "run", sid, "-q", sql)
    assert out["status"] == "executed", out
    return out["qid"]


def _finding(s: Session, qid: str, title: str, summary: str, **extra) -> str:
    payload = {
        "title": title,
        "severity": "medium",
        "confidence": "medium",
        "summary": summary,
        "evidence": [qid],
        "affected_objects": list(s.targets),
    }
    payload.update(extra)
    return engine.record_finding(s, payload)["fid"]


def test_ground_truth_matches_the_seed(tmp_path):
    truth = seed_sandbox(tmp_path / "wh.db")
    assert ground_truth() == truth
    assert truth["customers"]["null_email_count"] > 0


def test_every_planted_problem_targets_a_seeded_table():
    truth = ground_truth()
    for p in PLANTED:
        assert p.section in truth
        for key in p.quantities:
            assert key in truth[p.section]
        if p.ids:
            assert isinstance(truth[p.section][p.ids], list)
    assert [p.id for p in problems_for(["sandbox.shop.customers"])] == [
        "customers_null_email",
        "customers_duplicate_ids",
        "customers_future_birthdates",
    ]
    assert len(problems_for(["SANDBOX.SHOP.PAYMENTS_V2"])) == 2
    assert problems_for(["DB.S.T1"]) == []


def test_numbers_ignore_dates_and_identifiers():
    found = _numbers("86 rows since 2026-07-15 (q_0003, f_001); 1,234 total at 2026-08-01T06:00")
    assert 86 in found and 1234 in found
    assert not ({2026, 7, 15, 3, 1, 8, 6} & found)


def test_table_health_scores_each_planted_problem(sandbox_ws):
    truth = ground_truth()["customers"]
    sid = _start("table-health", "SANDBOX.SHOP.CUSTOMERS")
    q = _query(sid, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.CUSTOMERS WHERE EMAIL IS NULL")
    s = Session(sandbox_ws, sid)
    nulls = _finding(
        s, q, "EMAIL is NULL for recent signups",
        f"{truth['null_email_count']} rows have a NULL email; every one signed up on or "
        "after 2026-07-15, none before — a signup-form regression at that cutoff.",
    )  # fmt: skip
    dups = _finding(
        s, q, "Duplicate CUSTOMER_ID values",
        "Customer ids 101, 202 and 303 each appear twice, with conflicting emails on the "
        "two rows.",
    )  # fmt: skip
    noise = _finding(
        s, q, "COUNTRY uses two-letter codes",
        "COUNTRY holds ISO-2 codes rather than names; worth a note in the descriptor.",
    )  # fmt: skip
    s.accept_finding(nulls)

    result = score_session(s)
    by_id = {p["id"]: p for p in result["problems"]}
    assert result["score"] == {"points": 6, "possible": 9}
    assert by_id["customers_null_email"]["points"] == 3
    assert by_id["customers_null_email"]["by"] == nulls
    assert by_id["customers_null_email"]["accepted"] is True
    assert by_id["customers_duplicate_ids"]["points"] == 3
    assert by_id["customers_duplicate_ids"]["accepted"] is False
    assert by_id["customers_future_birthdates"]["points"] == 0
    assert by_id["customers_future_birthdates"]["by"] is None
    assert [u["fid"] for u in result["findings"]["unmatched"]] == [noise]
    assert result["findings"]["accepted"] == 1 and result["findings"]["rejected"] == []
    assert result["effort"]["queries_executed"] == 1
    assert dups in {p["by"] for p in result["problems"]}

    text = render_score(result)
    assert "6 / 9" in text and "✓ ✓ ✓" in text and "· · ·" in text
    assert "not accepted" in text  # the duplicate-id finding was never accepted
    assert "false positive" in text and "COUNTRY" in text


def test_partial_credit_says_what_was_missing(sandbox_ws):
    sid = _start("table-health", "SANDBOX.SHOP.CUSTOMERS")
    q = _query(sid, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.CUSTOMERS WHERE EMAIL IS NULL")
    s = Session(sandbox_ws, sid)
    _finding(s, q, "Some emails are missing", "Roughly 80 customers have a NULL email.")
    result = score_session(s)
    row = next(p for p in result["problems"] if p["id"] == "customers_null_email")
    # identified, but neither the onset nor a count within 2% of the planted value
    assert (row["identified"], row["explained"], row["quantified"]) == (True, False, False)
    text = render_score(result)
    assert "explained needs" in text and "quantified needs" in text and "±2%" in text
    # within tolerance counts: 85 is 1.2% off 86
    _finding(s, q, "Missing emails, again", "About 85 NULL emails, all after the cutoff date.")
    result = score_session(s)
    row = next(p for p in result["problems"] if p["id"] == "customers_null_email")
    assert row["points"] == 3


def test_rejected_and_superseded_findings_do_not_score(sandbox_ws):
    sid = _start("table-health", "SANDBOX.SHOP.CUSTOMERS")
    q = _query(sid, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.CUSTOMERS WHERE BIRTH_DATE > '2026'")
    s = Session(sandbox_ws, sid)
    wrong = _finding(s, q, "Birthdates in the future", "Ids 77, 411 and 902 were born in 2031.")
    s.reject_finding(wrong, "not this one")
    result = score_session(s)
    row = next(p for p in result["problems"] if p["id"] == "customers_future_birthdates")
    assert row["identified"] is False
    assert [r["fid"] for r in result["findings"]["rejected"]] == [wrong]
    assert "rejected: not this one" in render_score(result)

    old = _finding(s, q, "Future birthdates", "Three customers have birth dates in 2031.")
    new = _finding(
        s, q, "Future birthdates (corrected)", "Ids 77 and 411 have birth dates in 2031.",
        supersedes=old,
    )  # fmt: skip
    s.accept_finding(new)
    result = score_session(s)
    row = next(p for p in result["problems"] if p["id"] == "customers_future_birthdates")
    assert row["by"] == new and row["points"] == 3 and row["also"] == []


def test_bug_hunter_and_parity_schemas_score(sandbox_ws):
    truth = ground_truth()
    sid = _start("bug-hunter", "SANDBOX.SHOP.ORDERS_ENRICHED")
    q = _query(sid, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.ORDERS_ENRICHED")
    s = Session(sandbox_ws, sid)
    _finding(
        s, q, "ORDERS_ENRICHED duplicates orders",
        "Orders using a re-issued promo code appear twice after the LEFT JOIN.",
        extra={
            "resolution": "root_caused",
            "root_cause": "PROMOS holds SUMMER25 and FLASH5 twice, so the join fans out",
            "blast_radius": f"{truth['orders_enriched']['extra_rows']} extra rows",
            "alternatives_tested": "ORDERS itself has no duplicate ORDER_ID",
        },
    )  # fmt: skip
    result = score_session(s)
    assert result["score"] == {"points": 3, "possible": 3}

    sid2 = _start("migration-parity", "SANDBOX.SHOP.PAYMENTS", "SANDBOX.SHOP.PAYMENTS_V2")
    q2 = _query(sid2, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.PAYMENTS_V2")
    s2 = Session(sandbox_ws, sid2)
    parity = {
        "old_object": "SANDBOX.SHOP.PAYMENTS",
        "new_object": "SANDBOX.SHOP.PAYMENTS_V2",
    }
    _finding(
        s2, q2, "V2 is missing refunded payments",
        f"{truth['payments']['missing_refunded_rows']} rows with STATUS = 'refunded' are "
        "absent from V2: the backfill copied only settled rows.",
        extra={**parity, "parity_result": "fail: refunded rows missing"},
    )  # fmt: skip
    _finding(
        s2, q2, "EUR amounts differ between PAYMENTS and V2",
        "EUR rows lost their decimals in V2 — cast to integer.",
        extra={
            **parity,
            "parity_result": f"fail: {truth['payments']['eur_amount_mismatches']} rows",
        },
    )  # fmt: skip
    result2 = score_session(s2)
    assert result2["score"] == {"points": 6, "possible": 6}
    board = render_leaderboard([result, result2])
    assert sid in board and sid2 in board and "3/3" in board and "6/6" in board


def test_score_refuses_sessions_without_planted_targets(sandbox_ws):
    sid = _start("table-health", "SANDBOX.SHOP.ORDERS")
    with pytest.raises(ValueError, match="no planted problems"):
        score_session(Session(sandbox_ws, sid))


def test_cli_score_is_a_user_action(sandbox_ws, monkeypatch):
    sid = _start("table-health", "SANDBOX.SHOP.CUSTOMERS")
    q = _query(sid, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.CUSTOMERS WHERE EMAIL IS NULL")
    _finding(Session(sandbox_ws, sid), q, "NULL emails", "86 NULL emails since 2026-07-15.")

    # an agent shelling out cannot read the key
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    denied = runner.invoke(app, ["sandbox", "score", sid])
    assert denied.exit_code != 0 and "interactive terminal" in denied.output

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    out = invoke("sandbox", "score", sid)
    assert out["score"] == {"points": 3, "possible": 9}
    assert "Sandbox score" in out["text"]
    board = invoke("sandbox", "score", "--all")
    assert [r["session"] for r in board["sessions"]] == [sid]
    assert sid in board["text"]
    empty = runner.invoke(app, ["sandbox", "score"])
    assert empty.exit_code != 0 and "--all" in empty.output
    assert not (sandbox_db_path(sandbox_ws.root) / "nothing").exists()


def test_cli_score_needs_a_sandbox_workspace(tmp_path, monkeypatch):
    ws = Workspace.init(tmp_path / "plain")
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    result = runner.invoke(app, ["sandbox", "score", "--all"])
    assert result.exit_code != 0 and "not a sandbox" in result.output


def test_mcp_has_no_score_tool(sandbox_ws):
    import asyncio

    from grayson.mcp.server import build_server

    names = {t.name for t in asyncio.run(build_server(sandbox_ws).list_tools())}
    assert "session_brief" in names
    assert not any("score" in n for n in names)
