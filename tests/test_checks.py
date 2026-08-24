"""External checks library: format parsing, latest/summary, ingest, surfacing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from grayson.checks import ChecksStore
from grayson.checks.store import MAX_INGESTED_RUNS
from grayson.cli import app

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _result(check_id="orders_null_email", status="fail", run_at=None, **extra):
    return {
        "check_id": check_id,
        "status": status,
        "run_at": run_at or "2026-08-24T06:00:00Z",
        "tables": extra.pop("tables", ["ANALYTICS.SHOP.ORDERS"]),
        **extra,
    }


@pytest.fixture
def store(workspace) -> ChecksStore:
    return ChecksStore(workspace.checks_dir)


def test_scaffolded_with_readme(workspace):
    readme = workspace.checks_dir / "README.md"
    assert readme.is_file()
    assert "check_id" in readme.read_text(encoding="utf-8")


def test_load_accepts_object_list_and_wrapper(store, workspace):
    _write(workspace.checks_dir / "one.json", _result(check_id="a"))
    _write(workspace.checks_dir / "two.json", [_result(check_id="b"), _result(check_id="c")])
    _write(workspace.checks_dir / "sub" / "three.json", {"results": [_result(check_id="d")]})
    results, errors = store.load()
    assert {r.check_id for r in results} == {"a", "b", "c", "d"}
    assert errors == []


def test_invalid_entries_reported_not_fatal(store, workspace):
    _write(workspace.checks_dir / "ok.json", _result(check_id="good"))
    _write(workspace.checks_dir / "bad_status.json", _result(check_id="x", status="exploded"))
    (workspace.checks_dir / "not_json.json").write_text("{nope", encoding="utf-8")
    results, errors = store.load()
    assert [r.check_id for r in results] == ["good"]
    assert len(errors) == 2


def test_latest_picks_newest_run_and_filters_by_table(store, workspace):
    _write(
        workspace.checks_dir / "runs.json",
        [
            _result(status="pass", run_at="2026-08-22T06:00:00Z"),
            _result(status="fail", run_at="2026-08-24T06:00:00Z"),
            _result(check_id="other_table", tables=["DB.S.T9"], status="pass"),
        ],
    )
    latest = store.latest(["analytics.shop.orders"])
    assert len(latest) == 1
    assert latest[0].status == "fail"
    assert store.latest(["DB.S.T9"])[0].check_id == "other_table"
    assert len(store.latest()) == 2


def test_summary_failing_and_overdue(store, workspace):
    stale = (datetime.now(UTC) - timedelta(hours=50)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    _write(
        workspace.checks_dir / "runs.json",
        [
            _result(check_id="failing", status="fail", run_at=fresh, details="812 NULLs"),
            _result(check_id="overdue_pass", status="pass", run_at=stale, ttl_hours=26),
            _result(check_id="fresh_pass", status="pass", run_at=fresh, ttl_hours=26),
        ],
    )
    s = store.summary()
    assert s["total_checks"] == 3
    assert [f["check_id"] for f in s["failing"]] == ["failing"]
    assert s["failing"][0]["details"] == "812 NULLs"
    assert [o["check_id"] for o in s["overdue"]] == ["overdue_pass"]
    by_id = {c["check_id"]: c for c in s["checks"]}
    assert by_id["overdue_pass"]["overdue"] is True
    assert by_id["fresh_pass"]["overdue"] is False


def test_ingest_normalizes_dedups_and_trims(store, workspace, tmp_path):
    drop = tmp_path / "airflow_dump.json"
    _write(drop, {"results": [_result(run_at="2026-08-24T06:00:00Z")]})
    out = store.ingest(drop, source="airflow")
    assert out["ingested"] == 1 and out["errors"] == []
    # idempotent on the same (check_id, run_at)
    again = store.ingest(drop)
    assert again["ingested"] == 0 and again["skipped_duplicates"] == 1
    # source filled in where missing
    assert store.latest()[0].source == "airflow"
    # history bounded
    many = [_result(run_at=f"2026-07-{d:02d}T06:00:00Z") for d in range(1, MAX_INGESTED_RUNS + 6)]
    _write(drop, many)
    store.ingest(drop)
    target = workspace.checks_dir / "ingested" / "orders_null_email.json"
    assert len(json.loads(target.read_text())) == MAX_INGESTED_RUNS


def test_ingest_rejects_bad_entries_with_detail(store, tmp_path):
    drop = tmp_path / "bad.json"
    _write(drop, [_result(check_id="ok"), _result(check_id="bad id with spaces")])
    out = store.ingest(drop)
    assert out["ingested"] == 1
    assert len(out["errors"]) == 1 and "check_id" in out["errors"][0]["error"]


def test_cli_status_show_and_ingest(workspace, tmp_path):
    drop = tmp_path / "results.json"
    _write(drop, [_result(status="fail"), _result(check_id="c2", status="pass")])
    out = invoke("checks", "ingest", str(drop), "--source", "airflow")
    assert out["ingested"] == 2
    status = invoke("checks", "status", "--table", "ANALYTICS.SHOP.ORDERS")
    assert status["total_checks"] == 2
    assert [f["check_id"] for f in status["failing"]] == ["orders_null_email"]
    history = invoke("checks", "show", "orders_null_email")
    assert history[0]["source"] == "airflow"
    listed = invoke("checks", "list")
    assert len(listed) == 2


def test_session_start_surfaces_failing_checks(workspace, fake_snow_env):
    _write(
        workspace.checks_dir / "airflow.json",
        _result(check_id="t1_dupes", status="fail", tables=["DB.S.T1"], sql="SELECT 1"),
    )
    out = invoke(
        "session", "start", "--workflow", "table-health", "--table", "DB.S.T1",
        "--guard-profile", "moderate",
    )  # fmt: skip
    ext = out["external_checks"]
    assert [f["check_id"] for f in ext["failing"]] == ["t1_dupes"]
    assert any("t1_dupes" in h for h in out["hints"])


def test_session_start_ignores_checks_on_other_tables(workspace, fake_snow_env):
    _write(
        workspace.checks_dir / "airflow.json",
        _result(check_id="elsewhere", status="fail", tables=["DB.S.OTHER"]),
    )
    out = invoke(
        "session", "start", "--workflow", "table-health", "--table", "DB.S.T1",
        "--guard-profile", "moderate",
    )  # fmt: skip
    assert out["external_checks"]["failing"] == []
    assert not any("elsewhere" in h for h in out["hints"])
