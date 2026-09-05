"""Evidence -> reviewed expectation -> replay -> shared history, across surfaces."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FakeExecutor
from grayson.checks import ChecksStore
from grayson.checks.regression import (
    Expectation,
    RegressionError,
    RegressionStore,
    decide_check,
    evaluate,
    propose_check,
    run_checks,
)
from grayson.checks.store import CheckResult
from grayson.cli import app
from grayson.config import GuardSettings
from grayson.core import engine
from grayson.core.brief import build_brief, render_brief
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.mcp.server import build_server
from grayson.ui.server import build_app
from grayson.workspace import Workspace


@pytest.fixture
def session(workspace):
    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=1000),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    return s


def propose(session, check_id="orders_clean", rows=None, expectation=None, sql=None):
    qid = run_statement(
        session,
        sql or "SELECT COUNT(*) AS N FROM DB.S.T1",
        executor=FakeExecutor(rows=[{"N": 0}] if rows is None else rows),
    )["qid"]
    out = propose_check(
        session,
        qid,
        check_id,
        "Orders stay clean",
        "Prevent the duplicate import bug",
        expectation or {"kind": "scalar", "column": "N", "value": 0},
    )
    return out["check"]


def activate(session, check):
    return decide_check(session.workspace, check["id"], "activate", check["digest"], actor="user")


def test_full_loop_reuses_the_rule_but_requires_new_evidence(session):
    check = propose(session)
    with pytest.raises(RegressionError, match="review"):
        run_checks(session, [check["id"]], executor=FakeExecutor())
    activate(session, check)
    first = run_checks(session, executor=FakeExecutor(rows=[{"N": 0}]))
    second = run_checks(session, executor=FakeExecutor(rows=[{"N": 7}]))
    assert first["ok"] and first["counts"] == {"pass": 1, "fail": 0, "error": 0}
    assert not second["ok"] and second["counts"]["fail"] == 1
    qids = {check["source_qid"], first["results"][0]["qid"], second["results"][0]["qid"]}
    assert len(qids) == 3 and qids <= session.executed_qids()
    assert second["results"][0]["evidence"] == [second["results"][0]["qid"]]
    history = ChecksStore(session.workspace.checks_dir).history("regression.orders_clean")
    assert [r.status for r in history] == ["fail", "pass"]
    assert (
        ChecksStore(session.workspace.checks_dir).summary()["failing"][0]["metrics"]["observed"]
        == "7"
    )
    assert all(c["status"] == "open" for c in session.checkpoints())
    assert session.findings() == []
    brief = build_brief(session)
    assert brief["regression_runs"][0]["qid"] == second["results"][0]["qid"]
    assert "Orders stay clean: fail" in render_brief(brief)


@pytest.mark.parametrize(
    "operator,value,upper,observed,expected",
    [
        ("eq", "9007199254740993", None, "9007199254740993", "pass"),
        ("eq", "9007199254740993", None, "9007199254740992", "fail"),
        ("ne", "1", None, "2", "pass"),
        ("lt", "3", None, "3", "fail"),
        ("lte", "3", None, "3", "pass"),
        ("gt", "3", None, "4", "pass"),
        ("gte", "3", None, "3", "pass"),
        ("between", "0.1", "0.3", "0.3", "pass"),
        ("between", "0.1", "0.3", "0.30000000000000001", "fail"),
    ],
)
def test_precise_scalar_expectations(session, operator, value, upper, observed, expected):
    qid = run_statement(
        session, "SELECT N FROM DB.S.T1", executor=FakeExecutor(rows=[{"N": observed}])
    )["qid"]
    rule = Expectation(kind="scalar", column="n", operator=operator, value=value, upper=upper)
    assert evaluate(session, qid, rule)["status"] == expected
    assert isinstance(rule.value, Decimal)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"N": None}],
        [{"N": "NaN"}],
        [{"N": "Infinity"}],
        [{"N": "oops"}],
        [{"RENAMED": 0}],
        [{"N": 0}, {"N": 0}],
    ],
)
def test_incomplete_or_changed_results_are_errors_never_passes(session, rows):
    check = propose(session)
    activate(session, check)
    out = run_checks(session, executor=FakeExecutor(rows=rows))
    assert out["counts"] == {"pass": 0, "fail": 0, "error": 1}


def test_empty_violations_pass_nonempty_fail_and_truncation_cannot_hide_them(session):
    check = propose(
        session, rows=[], expectation={"kind": "no_rows"}, sql="SELECT * FROM DB.S.T1 WHERE N < 0"
    )
    activate(session, check)
    assert run_checks(session, executor=FakeExecutor(rows=[]))["ok"]
    assert run_checks(session, executor=FakeExecutor(rows=[{"N": -1}]))["counts"]["fail"] == 1


def test_budget_and_strict_scope_are_enforced(session):
    check = propose(session)
    activate(session, check)
    session.set_meta("guard", GuardSettings(budget_cap=1).model_dump_json())
    ex = FakeExecutor(rows=[{"N": 0}])
    out = run_checks(session, executor=ex)
    assert not ex.calls and out["counts"]["error"] == 1
    assert out["results"][0]["evidence"] == []
    other = Session.create(
        session.workspace,
        workflow="table-health",
        targets=["DB.S.OTHER"],
        guard=GuardSettings(),
        guard_profile="moderate",
        strict_scope=True,
    )
    out = run_checks(other, [check["id"]], executor=ex)
    assert not ex.calls and out["counts"]["error"] == 1
    assert "DB.S.T1" not in other.scope_tables


def test_editing_approved_sql_or_expectation_requires_review_again(session):
    check = propose(session)
    activate(session, check)
    store = RegressionStore(session.workspace.checks_dir)
    changed = store.read(check["id"])
    changed.expectation.value = Decimal(7)
    store.save(changed)
    ex = FakeExecutor()
    with pytest.raises(RegressionError, match="review"):
        run_checks(session, executor=ex)
    with pytest.raises(RegressionError, match="changed since review"):
        decide_check(session.workspace, check["id"], "activate", check["digest"], actor="user")
    with pytest.raises(RegressionError, match="source evidence"):
        decide_check(session.workspace, check["id"], "activate", changed.digest(), actor="user")
    assert not ex.calls


def test_no_constant_rejected_metadata_or_unexecuted_source(session):
    for sql in ("SELECT 1", "DESCRIBE TABLE DB.S.T1", "DROP TABLE DB.S.T1"):
        qid = run_statement(session, sql, executor=FakeExecutor())["qid"]
        with pytest.raises(RegressionError):
            propose_check(session, qid, "bad", "Bad", "Invalid source", {"kind": "no_rows"})
    with pytest.raises(RegressionError):
        propose_check(session, "q_9999", "bad", "Bad", "Invalid source", {"kind": "no_rows"})


def test_selection_preflight_and_duplicate_definition_protect_existing_work(session):
    check = propose(session)
    activate(session, check)
    with pytest.raises(RegressionError, match="already exists"):
        propose(session)
    ex = FakeExecutor()
    with pytest.raises(RegressionError, match="no regression"):
        run_checks(session, [check["id"], "typo"], executor=ex)
    assert not ex.calls
    empty = run_checks(session, [], executor=ex)
    assert not ex.calls and not empty["ok"] and not empty["results"]


def test_human_only_retirement_keeps_history_and_definition(session):
    check = propose(session)
    with pytest.raises(RegressionError, match="user action"):
        decide_check(session.workspace, check["id"], "activate", check["digest"])
    activate(session, check)
    run_checks(session, executor=FakeExecutor(rows=[{"N": 7}]))
    assert ChecksStore(session.workspace.checks_dir).summary()["failing"]
    decide_check(session.workspace, check["id"], "retire", check["digest"], actor="user")
    assert run_checks(session, executor=FakeExecutor())["results"] == []
    assert RegressionStore(session.workspace.checks_dir).read(check["id"]).state == "retired"
    assert ChecksStore(session.workspace.checks_dir).history(check["result_id"])
    assert not ChecksStore(session.workspace.checks_dir).summary()["failing"]
    with pytest.raises(RegressionError, match="retired"):
        activate(session, check)


def test_newer_definition_is_reported_without_modification(session):
    check = propose(session)
    path = RegressionStore(session.workspace.checks_dir).path(check["id"])
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["format"] = 99
    data["future_field"] = {"retain": True}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    before = path.read_bytes()
    inventory = RegressionStore(session.workspace.checks_dir).inventory()
    assert inventory["errors"] and not inventory["checks"]
    with pytest.raises(RegressionError):
        activate(session, check)
    assert path.read_bytes() == before


def test_cli_propose_and_human_gate(session):
    qid = run_statement(session, "SELECT N FROM DB.S.T1", executor=FakeExecutor(rows=[{"N": 0}]))[
        "qid"
    ]
    runner = CliRunner()
    out = runner.invoke(
        app,
        [
            "checks",
            "propose",
            session.id,
            qid,
            "--id",
            "cli_check",
            "--name",
            "CLI check",
            "--description",
            "Prevent duplicates",
            "--expect",
            "scalar",
            "--column",
            "N",
            "--value",
            "0",
        ],
    )
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)["check"]["state"] == "proposed"
    blocked = runner.invoke(app, ["checks", "activate", "cli_check"])
    assert blocked.exit_code == 1 and "interactive terminal" in blocked.output
    listed = runner.invoke(app, ["checks", "regressions"])
    assert listed.exit_code == 0 and len(json.loads(listed.output)["checks"]) == 1


def test_mcp_has_proposal_and_replay_but_no_human_approval_tool(session, monkeypatch):
    import grayson.core.run as run

    monkeypatch.setattr(run, "get_executor", lambda *a: FakeExecutor(rows=[{"N": 0}]))
    check = propose(session)
    server = build_server(session.workspace)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"checks_propose", "checks_run", "checks_regressions"} <= names
    assert not {"checks_activate", "checks_retire"}.intersection(names)
    activate(session, check)
    response = asyncio.run(server.call_tool("checks_run", {"session_id": session.id}))
    result = json.loads(response.content[0].text)
    assert result["ok"] and result["results"][0]["evidence"]


def test_console_query_to_review_to_replay_and_stale_review_refusal(session, monkeypatch):
    import grayson.core.run as run

    monkeypatch.setattr(run, "get_executor", lambda *a: FakeExecutor(rows=[{"N": 0}]))
    qid = run_statement(session, "SELECT N FROM DB.S.T1")["qid"]
    client = TestClient(build_app(session.workspace, token="test"), base_url="http://127.0.0.1")
    query = client.get(f"/session/{session.id}/query/{qid}?t=test")
    assert query.status_code == 200 and "Save as regression check" in query.text
    saved = client.post(
        f"/session/{session.id}/query/{qid}/regression?t=test",
        data={
            "name": "No duplicate orders",
            "description": "Catch the import bug returning",
            "kind": "scalar",
            "column": "N",
            "operator": "eq",
            "value": "0",
        },
    )
    assert saved.status_code == 200 and "Activate this check" in saved.text
    check = RegressionStore(session.workspace.checks_dir).inventory()["checks"][0]
    url = f"/checks/regression/{check['id']}"
    assert (
        client.post(
            url + "/decide?t=test", data={"action": "activate", "digest": "stale"}
        ).status_code
        == 400
    )
    active = client.post(
        url + "/decide?t=test", data={"action": "activate", "digest": check["digest"]}
    )
    assert active.status_code == 200 and "Run check" in active.text
    ran = client.post(url + "/run?t=test", data={"session_id": session.id})
    assert ran.status_code == 200 and "Observed N = 0" in ran.text
    assert client.get("/checks?t=test").status_code == 200


def test_parallel_results_preserve_every_run_and_retry_is_idempotent(session):
    store = ChecksStore(session.workspace.checks_dir)
    now = datetime.now(UTC)
    results = [
        CheckResult(
            check_id="regression.shared",
            status="pass",
            run_at=(now + timedelta(microseconds=i)).isoformat(),
            metrics={"worker": i},
        )
        for i in range(24)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(store.record, results))
    store.record(results[0])
    assert len(set(paths)) == 24
    assert {r.metrics["worker"] for r in store.history("regression.shared")} == set(range(24))
    assert len(store.history("regression.shared")) == 24
    assert not store.load()[1]


def test_native_proposals_cannot_take_over_an_existing_external_check_id(session):
    store = ChecksStore(session.workspace.checks_dir)
    old = CheckResult(
        check_id="regression.orders_clean",
        status="fail",
        source="existing-automation",
        run_at=datetime.now(UTC).isoformat(),
        tables=["DB.S.T1"],
    )
    path = store.record(old)
    before = path.read_bytes()
    with pytest.raises(RegressionError, match="results already use"):
        propose(session)
    assert path.read_bytes() == before
    assert store.summary()["failing"][0]["source"] == "existing-automation"


def test_team_replay_needs_no_source_session_and_preserves_existing_library(session, tmp_path):
    from grayson.library import set_library_config

    existing = session.workspace.knowledge_dir / "custom.md"
    existing.write_bytes(b"# Team knowledge\r\n\r\nKeep this exact prose.\r\n")
    before = {p: p.read_bytes() for p in session.workspace.knowledge_dir.rglob("*") if p.is_file()}
    check = propose(session)
    activate(session, check)
    other = Workspace.init(tmp_path / "teammate")
    set_library_config(other.root, session.workspace.root, auto_push=False)
    replay = Session.create(
        other,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(),
        guard_profile="moderate",
        connection="teammate_warehouse",
    )
    result = run_checks(replay, executor=FakeExecutor(rows=[{"N": 7}]))
    assert result["counts"]["fail"] == 1
    assert not other.session_dir(check["source_session"]).exists()
    history = ChecksStore(session.workspace.checks_dir).history(check["result_id"])
    assert history[0].metrics["connection"] == "teammate_warehouse"
    assert history[0].metrics["session_id"] == replay.id
    client = TestClient(build_app(other, token="test"), base_url="http://127.0.0.1")
    page = client.get(f"/checks/regression/{check['id']}?t=test")
    assert page.status_code == 200 and "source session on another workspace" in page.text
    assert all(p.read_bytes() == data for p, data in before.items())


def test_result_write_failure_is_inconclusive_and_keeps_session_evidence(session, monkeypatch):
    check = propose(session)
    activate(session, check)

    def fail_write(*args):
        raise OSError("read-only library")

    monkeypatch.setattr(ChecksStore, "record", fail_write)
    result = run_checks(session, executor=FakeExecutor(rows=[{"N": 0}]))
    assert not result["ok"]
    entry = result["results"][0]
    assert entry["persistence_error"] == "read-only library"
    assert entry["qid"] in session.executed_qids()
    assert build_brief(session)["regression_runs"][0]["persistence_error"]


def test_git_timeout_does_not_lose_the_saved_proposal(session, monkeypatch):
    import subprocess

    import grayson.library as library

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 30)

    monkeypatch.setattr(library, "commit_library_paths", timeout)
    check = propose(session)
    assert RegressionStore(session.workspace.checks_dir).read(check["id"])


def test_capped_scalar_and_executor_failure_cannot_pass(session):
    check = propose(session, sql="SELECT N FROM DB.S.T1")
    activate(session, check)
    session.set_meta("guard", GuardSettings(auto_limit=1).model_dump_json())
    result = run_checks(session, executor=FakeExecutor(rows=[{"N": 0}]))
    assert result["counts"]["error"] == 1
    assert "complete result row" in result["results"][0]["details"]
    result = run_checks(session, executor=FakeExecutor(status="error", error="warehouse offline"))
    assert result["counts"]["error"] == 1 and not result["results"][0]["evidence"]


def test_real_sql_catches_a_reintroduced_duplicate(session, tmp_path):
    import sqlite3

    from grayson.sandbox.executor import SandboxExecutor

    warehouse = tmp_path / "test-warehouse.db"
    with sqlite3.connect(warehouse) as conn:
        conn.execute('CREATE TABLE "DB.S.T1" (ORDER_ID INTEGER)')
        conn.executemany('INSERT INTO "DB.S.T1" VALUES (?)', [(1,), (2,), (3,)])
    executor = SandboxExecutor(warehouse)
    sql = (
        "SELECT COUNT(*) AS DUPLICATE_ORDERS FROM "
        "(SELECT ORDER_ID FROM DB.S.T1 GROUP BY ORDER_ID HAVING COUNT(*) > 1)"
    )
    original = run_statement(session, sql, executor=executor)
    check = propose_check(
        session,
        original["qid"],
        "orders_unique",
        "Orders remain unique",
        "Catch duplicate order IDs returning after the import fix",
        {"kind": "scalar", "column": "DUPLICATE_ORDERS", "value": "0"},
    )["check"]
    activate(session, check)
    assert run_checks(session, executor=executor)["ok"]
    # Test fixture simulates a later warehouse import; Grayson itself only reads.
    with sqlite3.connect(warehouse) as conn:
        conn.execute('INSERT INTO "DB.S.T1" VALUES (2)')
    failed = run_checks(session, executor=executor)
    assert failed["counts"] == {"pass": 0, "fail": 1, "error": 0}
    assert failed["results"][0]["metrics"]["observed"] == "1"
    assert failed["results"][0]["qid"] != original["qid"]


def test_console_reports_sharing_failure_and_bulk_redirect_keeps_token(session, monkeypatch):
    import grayson.core.run as run
    import grayson.library as library

    monkeypatch.setattr(run, "get_executor", lambda *a: FakeExecutor(rows=[{"N": 0}]))
    check = propose(session)
    monkeypatch.setattr(library, "commit_library_paths", lambda *a, **kw: {"ok": False})
    client = TestClient(build_app(session.workspace, token="test"), base_url="http://127.0.0.1")
    active = client.post(
        f"/checks/regression/{check['id']}/decide?t=test",
        data={"action": "activate", "digest": check["digest"]},
    )
    assert active.status_code == 200 and "sharing did not finish" in active.text
    ran = client.post(f"/session/{session.id}/regressions/run?t=test", follow_redirects=False)
    assert ran.status_code == 303
    assert ran.headers["location"].endswith("?sync=failed&t=test#regressions")
    assert "Results saved locally" in client.get(ran.headers["location"]).text
