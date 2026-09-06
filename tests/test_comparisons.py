import asyncio
import json
import sqlite3

import pytest
import sqlglot
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from grayson.cli import app
from grayson.config import GuardSettings
from grayson.core import comparisons, engine
from grayson.core.session import Session
from grayson.executor.snow import ExecutionResult
from grayson.mcp.server import build_server
from grayson.ui.server import build_app


class Warehouse:
    def __init__(self, rows, candidate=False):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            f"CREATE TABLE ORDERS ({'NEW_ID' if candidate else 'ID'} INTEGER, "
            "REVENUE TEXT, REGION TEXT)"
        )
        self.db.executemany("INSERT INTO ORDERS VALUES (?, ?, ?)", rows)
        self.calls = []

    def execute(self, sql, timeout_seconds=0):
        self.calls.append(sql)
        tree = sqlglot.parse_one(sql, read="snowflake")
        for table in tree.find_all(sqlglot.exp.Table):
            table.set("catalog", None)
            table.set("db", None)
        cursor = self.db.execute(tree.sql(dialect="sqlite"))
        rows = [dict(row) for row in cursor.fetchall()]
        return ExecutionResult(
            status="ok", rows=rows, columns=[d[0] for d in cursor.description], duration_ms=1
        )


@pytest.fixture
def pair(workspace):
    sessions = [
        Session.create(
            workspace,
            workflow="table-health",
            targets=["DB.S.ORDERS"],
            guard=GuardSettings(auto_limit=1000),
            guard_profile="moderate",
            connection=connection,
        )
        for connection in ("production", "candidate")
    ]
    for s in sessions:
        engine.seed_from_workflow(s)
    return sessions


def spec(pair, **kwargs):
    return {
        "format": 1,
        "id": "orders_release",
        "name": "Orders release",
        "left": {"session_id": pair[0].id, "table": "DB.S.ORDERS", "label": "Production"},
        "right": {"session_id": pair[1].id, "table": "DB.S.ORDERS", "label": "Candidate"},
        "keys": [{"left": "ID", "right": "NEW_ID"}],
        "columns": [
            {
                "left": "REVENUE",
                "right": "REVENUE",
                "kind": "numeric",
                "relative_percent": "0.1",
                "aggregate": True,
            },
            {"left": "REGION", "right": "REGION"},
        ],
        "group_by": ["REGION"],
        **kwargs,
    }


def run(pair, left, right, **kwargs):
    comparisons.create(pair[0], spec(pair, **kwargs))
    return comparisons.run(
        pair[0],
        "orders_release",
        executors={"left": Warehouse(left), "right": Warehouse(right, candidate=True)},
    )


def test_real_sql_mapping_tolerance_and_provenance(pair):
    report = run(
        pair, [(1, "100", "UK"), (2, "200", "US")], [(2, "200.1", "US"), (1, "100.1", "UK")]
    )
    assert report["verdict"] == "pass"
    assert report["coverage"]["record_parity"] == "complete"
    assert report["left"]["connection"] == "production"
    assert report["right"]["connection"] == "candidate"
    for side, s in zip(("left", "right"), pair, strict=True):
        assert len(report[side]["evidence"]) == 3
        assert all(
            e["qid"] in s.executed_qids() and e["observed_at"] and e["sql"]
            for e in report[side]["evidence"]
        )
    assert comparisons.latest_report(pair[0], "orders_release") == report


def test_missing_duplicate_and_changed_values_concentrations(pair):
    report = run(
        pair,
        [(1, "100", "UK"), (2, "200", "US"), (4, "10", "US")],
        [(1, "101", "UK"), (3, "200", "US"), (4, "10", "US"), (4, "10", "US")],
    )
    results = {r["name"]: r for r in report["results"]}
    assert report["verdict"] == "fail"
    assert results["right_duplicate_keys"]["observed"] == 1
    assert results["missing_left"]["observed"] == 1
    assert results["missing_right"]["observed"] == 1
    assert results["changed_rows"]["status"] == "unproven"  # duplicate key has no unique partner
    assert report["examples"]["changed"][0]["key"] == [1]
    assert report["column_mismatches"] == {"REVENUE": 1}
    assert {r["value"] for r in report["concentrations"]} == {"UK", "US"}


def test_capped_extract_cannot_pass_but_full_aggregates_still_detect_failure(pair):
    for s in pair:
        s.set_meta("guard", GuardSettings(auto_limit=2).model_dump_json())
    rows = [(i, "100", "UK") for i in range(4)]
    report = run(pair, rows, rows)
    assert report["verdict"] == "unproven"
    assert report["row_counts"] == {"left": 4, "right": 4}
    assert report["aggregates"][0]["status"] == "pass"
    assert report["coverage"]["record_parity"] == "incomplete"
    failed = comparisons.run(
        pair[0],
        "orders_release",
        executors={
            "left": Warehouse(rows),
            "right": Warehouse([*rows[:3], (3, "1000", "UK")], True),
        },
    )
    assert failed["aggregates"][0]["status"] == "fail" and failed["verdict"] == "fail"


def test_filters_empty_relations_and_null_keys(pair):
    data = spec(pair)
    data["left"]["filter"] = "REGION = 'XX'"
    data["right"]["filter"] = "REGION = 'XX'"
    comparisons.create(pair[0], data)
    report = comparisons.run(
        pair[0],
        data["id"],
        executors={"left": Warehouse([(1, "100", "UK")]), "right": Warehouse([], True)},
    )
    assert report["verdict"] == "pass" and report["row_counts"] == {"left": 0, "right": 0}
    data["id"] = "null_keys"
    data["left"]["filter"] = data["right"]["filter"] = ""
    comparisons.create(pair[0], data)
    report = comparisons.run(
        pair[0],
        data["id"],
        executors={
            "left": Warehouse([(None, "100", "UK")]),
            "right": Warehouse([(None, "100", "UK")], True),
        },
    )
    assert report["verdict"] == "fail"


@pytest.mark.parametrize(
    "filter", ["1=1; DROP TABLE DB.S.ORDERS", "1=1 LIMIT 0", "ID IN (SELECT ID FROM DB.S.OTHER)"]
)
def test_invalid_filters_rejected_before_execution(pair, filter):
    data = spec(pair)
    data["left"]["filter"] = filter
    with pytest.raises(ValueError):
        comparisons.create(pair[0], data)
    assert pair[0].executed_qids() == set()


def test_budget_failures_changed_connections_and_future_formats(pair):
    comparisons.create(pair[0], spec(pair))
    pair[1].set_meta("connection", "changed")
    with pytest.raises(ValueError, match="connection changed"):
        comparisons.run(pair[0], "orders_release")
    pair[1].set_meta("connection", "candidate")
    pair[1].set_meta("guard", GuardSettings(budget_cap=1).model_dump_json())
    report = comparisons.run(
        pair[0], "orders_release", executors={"left": Warehouse([]), "right": Warehouse([], True)}
    )
    assert report["verdict"] == "unproven"
    with pytest.raises(ValueError, match="already exists"):
        comparisons.create(pair[0], spec(pair))
    pair[0].set_meta("comparison:future", json.dumps({"format": 99}))
    assert (
        comparisons.inventory(pair[0])[-1].get("spec") or comparisons.inventory(pair[0])[0]["error"]
    )
    assert json.loads(pair[0].get_meta("comparison:future")) == {"format": 99}


def test_console_cli_mcp_parity_and_export(pair, monkeypatch):
    comparisons.create(pair[0], spec(pair))
    import grayson.core.run as queries

    warehouses = {
        "production": Warehouse([(1, "100", "UK")]),
        "candidate": Warehouse([(1, "110", "UK")], True),
    }
    monkeypatch.setattr(queries, "get_executor", lambda connection, root: warehouses[connection])
    client = TestClient(build_app(pair[0].workspace, token="secret"), base_url="http://localhost")
    path = f"/session/{pair[0].id}/comparisons"
    assert client.get(path).status_code == 403
    assert client.get(path + "?t=secret").status_code == 200
    page = client.post(path + "/orders_release/run?t=secret")
    assert page.status_code == 200 and "Differences exceed the contract" in page.text
    export = client.get(path + "/orders_release/report.json?t=secret")
    assert export.json()["verdict"] == "fail"
    assert "attachment" in export.headers["content-disposition"]
    result = CliRunner().invoke(app, ["comparison", "run", pair[0].id, "orders_release"])
    assert result.exit_code == 1 and json.loads(result.stdout)["verdict"] == "fail"
    names = {t.name for t in asyncio.run(build_server(pair[0].workspace).list_tools())}
    assert {
        "comparison_create",
        "comparison_show",
        "comparison_list",
        "comparison_run",
        "comparison_report",
    } <= names


def test_decimal_boundaries_and_zero_baseline():
    mapping = comparisons.Mapping(left="N", right="N", kind="numeric", relative_percent="0.1")
    assert comparisons._equal("100", "100.1", mapping)
    assert not comparisons._equal("100", "100.10000000000000001", mapping)
    assert not comparisons._equal("0", "0.0001", mapping)
    assert comparisons._equal("9007199254740993", "9007199254740993", mapping)
    with pytest.raises(ValueError):
        comparisons._equal("NaN", "NaN", mapping)
    mapping.absolute_tolerance = comparisons.Decimal("1e50")
    mapping.relative_percent = comparisons.Decimal(0)
    assert not comparisons._equal("1e50", "-1e-10", mapping)


def test_future_report_is_not_overwritten(pair):
    comparisons.create(pair[0], spec(pair))
    pair[0].set_meta("comparison_report:orders_release", '{"format":99,"future":"keep"}')
    with pytest.raises(ValueError, match="preserved unchanged"):
        comparisons.run(pair[0], "orders_release")
    assert json.loads(pair[0].get_meta("comparison_report:orders_release"))["future"] == "keep"


def test_mcp_runs_same_comparison_and_preserves_failed_verdict(pair, monkeypatch):
    from test_mcp import _call

    import grayson.core.run as queries

    warehouses = {
        "production": Warehouse([(1, "10", "UK")]),
        "candidate": Warehouse([(1, "20", "UK")], True),
    }
    monkeypatch.setattr(queries, "get_executor", lambda connection, root: warehouses[connection])
    mcp = build_server(pair[0].workspace)
    out = _call(mcp, "comparison_create", {"session_id": pair[0].id, "spec": spec(pair)})
    assert out["spec"]["id"] == "orders_release"
    report = _call(
        mcp, "comparison_run", {"session_id": pair[0].id, "comparison_id": "orders_release"}
    )
    assert report["verdict"] == "fail"
    assert report == comparisons.latest_report(pair[0], "orders_release")
