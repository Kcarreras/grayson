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
    assert len(problems_for(["SANDBOX.SHOP.PAYMENTS_V2"])) == 3
    assert [p.id for p in problems_for(["SANDBOX.SHOP.ORDERS_DAILY"])] == [
        "orders_join_fanout",
        "orders_amount_in_cents",
        "daily_month_end_dropped",
    ]
    assert problems_for(["DB.S.T1"]) == [] and problems_for(["SANDBOX.SHOP.PROMOS"]) == []


def test_quantities_match_within_tolerance():
    from grayson.sandbox.score import _near

    assert _near({85.0}, 86) and _near({87.0}, 86) and not _near({83.0}, 86)
    assert _near({1000.0}, 1015) and not _near({1000.0}, 1030)
    assert _near({0.0}, 0) and not _near({1.0}, 0)


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
    a, b, c = truth["duplicate_customer_ids"]
    dups = _finding(
        s, q, "Duplicate CUSTOMER_ID values",
        f"Customer ids {a}, {b} and {c} each appear twice, with conflicting emails on the "
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
    n = ground_truth()["customers"]["null_email_count"]
    _finding(
        s, q, "Some emails are missing", f"Roughly {round(n * 1.2)} customers have a NULL email."
    )
    result = score_session(s)
    row = next(p for p in result["problems"] if p["id"] == "customers_null_email")
    # identified, but neither the onset nor a count within 2% of the planted value
    assert (row["identified"], row["explained"], row["quantified"]) == (True, False, False)
    text = render_score(result)
    assert "explained needs" in text and "quantified needs" in text and "±2%" in text
    # the count itself, plus the onset, earns the remaining two points
    _finding(s, q, "Missing emails, again", f"{n} NULL emails, all after the cutoff date.")
    result = score_session(s)
    row = next(p for p in result["problems"] if p["id"] == "customers_null_email")
    assert row["points"] == 3


def test_rejected_and_superseded_findings_do_not_score(sandbox_ws):
    sid = _start("table-health", "SANDBOX.SHOP.CUSTOMERS")
    q = _query(sid, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.CUSTOMERS WHERE BIRTH_DATE > '2026'")
    s = Session(sandbox_ws, sid)
    ids = ground_truth()["customers"]["future_birthdate_ids"]
    wrong = _finding(
        s, q, "Birthdates in the future", f"Ids {ids[0]}, {ids[1]} and {ids[2]} were born in 2031."
    )
    s.reject_finding(wrong, "not this one")
    result = score_session(s)
    row = next(p for p in result["problems"] if p["id"] == "customers_future_birthdates")
    assert row["identified"] is False
    assert [r["fid"] for r in result["findings"]["rejected"]] == [wrong]
    assert "rejected: not this one" in render_score(result)

    old = _finding(s, q, "Future birthdates", "Three customers have birth dates in 2031.")
    new = _finding(
        s, q, "Future birthdates (corrected)",
        f"Ids {ids[0]} and {ids[1]} have birth dates in 2031.",
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
    # ORDERS_ENRICHED carries two planted problems: fan-out (tier 1) and the
    # minor-units window (tier 2) — this run found one of them
    assert result["score"] == {"points": 3, "possible": 6}
    assert next(p for p in result["problems"] if p["id"] == "orders_join_fanout")["points"] == 3
    assert next(p for p in result["problems"] if p["id"] == "orders_amount_in_cents")["by"] is None

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
    assert result2["score"] == {"points": 6, "possible": 9}  # PAID_AT precision loss not found
    board = render_leaderboard([result, result2])
    assert sid in board and sid2 in board and "3/6" in board and "6/9" in board
    assert "false+" in board


def test_score_refuses_sessions_without_planted_targets(sandbox_ws):
    sid = _start("table-health", "SANDBOX.SHOP.PROMOS")
    with pytest.raises(ValueError, match="no planted problems"):
        score_session(Session(sandbox_ws, sid))


def test_cli_score_is_a_user_action(sandbox_ws, monkeypatch):
    sid = _start("table-health", "SANDBOX.SHOP.CUSTOMERS")
    q = _query(sid, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.CUSTOMERS WHERE EMAIL IS NULL")
    _finding(Session(sandbox_ws, sid), q, "NULL emails", "NULL emails since 2026-07-15.")

    # an agent shelling out cannot read the key
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    denied = runner.invoke(app, ["sandbox", "score", sid])
    assert denied.exit_code != 0 and "interactive terminal" in denied.output

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    out = invoke("sandbox", "score", sid)
    assert out["score"] == {"points": 2, "possible": 9}  # the channel/date, not the count
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


def test_tier_two_problems_score(sandbox_ws):
    truth = ground_truth()
    # bug-hunter on ORDERS: the minor-units window, evidenced by the payments join
    sid = _start("bug-hunter", "SANDBOX.SHOP.ORDERS")
    q = _query(
        sid,
        "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.ORDERS o JOIN SANDBOX.SHOP.PAYMENTS p "
        "ON o.ORDER_ID = p.ORDER_ID WHERE o.AMOUNT > p.AMOUNT * 50",
    )
    s = Session(sandbox_ws, sid)
    _finding(
        s, q, "Android order amounts 100x too high in March",
        f"{truth['orders']['cents_affected_orders']} android orders between 2026-03-04 and "
        "2026-03-19 have AMOUNT exactly 100× the payment; a release sent totals in cents.",
        extra={
            "resolution": "root_caused",
            "root_cause": "android release sent minor units",
            "blast_radius": f"{truth['orders']['cents_affected_orders']} orders",
            "alternatives_tested": "web and ios are unaffected; other months are clean",
        },
    )  # fmt: skip
    result = score_session(s)
    cents = next(p for p in result["problems"] if p["id"] == "orders_amount_in_cents")
    assert cents["points"] == 3

    # semantic-rule-qa on ORDERS + CUSTOMERS: the WELCOME10 leak
    sid2 = _start("semantic-rule-qa", "SANDBOX.SHOP.ORDERS", "SANDBOX.SHOP.CUSTOMERS")
    q2 = _query(
        sid2, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.ORDERS WHERE PROMO_CODE = 'WELCOME10'"
    )
    s2 = Session(sandbox_ws, sid2)
    _finding(
        s2, q2, "WELCOME10 reused on repeat orders",
        f"{truth['orders']['welcome_violations']} WELCOME10 orders are not the customer's "
        "first order; all belong to partner-channel signups.",
        extra={
            "finding_kind": "rule_defect",
            "rule_location": "checkout promo validation",
            "observed_behaviour": "partner customers get WELCOME10 on later orders",
            "expected_behaviour": "first order only, per the marketing rule",
        },
    )  # fmt: skip
    result2 = score_session(s2)
    leak = next(p for p in result2["problems"] if p["id"] == "orders_welcome_leak")
    assert leak["points"] == 3
    # CUSTOMERS is a target too, so its three problems are in scope and unfound
    assert result2["score"]["possible"] == 3 * 5

    # pipeline-qa on ORDERS_DAILY: month-end days missing, inflation attributed upstream
    sid3 = _start("pipeline-qa", "SANDBOX.SHOP.ORDERS_DAILY", "SANDBOX.SHOP.ORDERS_ENRICHED")
    q3 = _query(sid3, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.ORDERS_DAILY")
    s3 = Session(sandbox_ws, sid3)
    common = {
        "stage_boundary": "ORDERS_ENRICHED → ORDERS_DAILY",
        "quantified_impact": "see summary",
    }
    _finding(
        s3, q3, "Rollup has no rows for the last day of any month",
        f"{truth['orders_daily']['missing_days']} month-end days are missing from ORDERS_DAILY; "
        "the batch bounds each month with an exclusive upper date (off-by-one).",
        extra=common,
    )  # fmt: skip
    _finding(
        s3, q3, "Rollup revenue inflated by upstream duplicates",
        "ORDER_COUNT and REVENUE are inflated because ORDERS_ENRICHED fans out orders using "
        "re-issued promo codes (duplicate CODE rows in PROMOS); the rollup itself is faithful.",
        extra=common,
    )  # fmt: skip
    result3 = score_session(s3)
    by_id = {p["id"]: p for p in result3["problems"]}
    assert by_id["daily_month_end_dropped"]["points"] == 3
    assert by_id["orders_join_fanout"]["identified"] and by_id["orders_join_fanout"]["explained"]

    # migration-parity: the PAID_AT precision loss
    sid4 = _start("migration-parity", "SANDBOX.SHOP.PAYMENTS", "SANDBOX.SHOP.PAYMENTS_V2")
    q4 = _query(sid4, "SELECT COUNT(*) AS N FROM SANDBOX.SHOP.PAYMENTS_V2")
    s4 = Session(sandbox_ws, sid4)
    _finding(
        s4, q4, "PAID_AT truncated to a date in V2",
        f"All {truth['payments']['paid_at_truncated_rows']} V2 rows have PAID_AT at midnight: "
        "the column was declared as DATE, so the time of day was lost.",
        extra={
            "old_object": "SANDBOX.SHOP.PAYMENTS",
            "new_object": "SANDBOX.SHOP.PAYMENTS_V2",
            "parity_result": "fail: PAID_AT precision",
        },
    )  # fmt: skip
    result4 = score_session(s4)
    assert (
        next(p for p in result4["problems"] if p["id"] == "payments_paid_at_truncated")["points"]
        == 3
    )


def test_decoys_reported_as_defects_are_called_out(sandbox_ws):
    sid = _start("bug-hunter", "SANDBOX.SHOP.ORDERS_ENRICHED")
    q = _query(sid, "SELECT ORDER_DATE, COUNT(*) AS N FROM SANDBOX.SHOP.ORDERS_ENRICHED GROUP BY 1")
    s = Session(sandbox_ws, sid)
    extra = {
        "resolution": "inconclusive",
        "blast_radius": "one day",
        "alternatives_tested": "none",
        "remaining_hypotheses": "bot traffic",
    }
    spike = _finding(
        s, q, "Order volume spike on 2025-11-28",
        "Orders quadruple on 2025-11-28 — an anomalous spike, possibly duplicated loads.",
        extra=extra,
    )  # fmt: skip
    noted = _finding(
        s, q, "Black Friday volume (context)",
        "Orders spike on 2025-11-28, which is Black Friday; not a defect.",
        severity="info", extra=extra,
    )  # fmt: skip
    result = score_session(s)
    flagged = result["findings"]["decoys_flagged"]
    assert [d["fid"] for d in flagged] == [spike]  # the info-severity note is correct use
    assert flagged[0]["decoy"].startswith("Black Friday")
    assert {u["fid"] for u in result["findings"]["unmatched"]} == {spike, noted}
    text = render_score(result)
    assert "reports a decoy as a defect: Black Friday" in text
    assert "(1 of them a decoy)" in text
    board = render_leaderboard([result])
    assert "false+" in board


def test_answer_key_covers_every_planted_problem_and_decoy(tmp_path):
    from grayson.sandbox.score import DECOYS
    from grayson.sandbox.seed import render_answer_key

    key = render_answer_key(ground_truth())
    for term in ("tier 2", "Not defects", "sandbox score", "minor units", "WELCOME10",
                 "last day", "PAID_AT", "Black Friday"):  # fmt: skip
        assert term in key, term
    assert len(DECOYS) >= 5 and len(PLANTED) == 10
