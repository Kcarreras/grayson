from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from grayson.executor.snow import SNOW_CMD_ENV, ExecutionResult
from grayson.workspace import Workspace

FAKE_SNOW = Path(__file__).parent / "fake_snow.py"


class FakeExecutor:
    """In-process executor double; responds based on SQL content."""

    def __init__(
        self, rows: list[dict] | None = None, status: str = "ok", error: str | None = None
    ):
        self.rows = rows
        self.status = status
        self.error = error
        self.calls: list[tuple[str, int]] = []

    def execute(self, sql: str, timeout_seconds: int = 0) -> ExecutionResult:
        self.calls.append((sql, timeout_seconds))
        if self.status != "ok":
            return ExecutionResult(status=self.status, error=self.error)
        if sql.strip().upper().startswith(("DESCRIBE", "DESC ")):
            rows = [
                {"name": "ID", "type": "NUMBER", "kind": "COLUMN"},
                {"name": "VAL", "type": "VARCHAR", "kind": "COLUMN"},
            ]
        elif "INFORMATION_SCHEMA.TABLES" in sql.upper():
            rows = [
                {
                    "TABLE_CATALOG": "DB",
                    "TABLE_SCHEMA": "S",
                    "TABLE_NAME": "T1",
                    "ROW_COUNT": 100,
                    "LAST_ALTERED": "2026-08-20 00:00:00",
                }
            ]
        else:
            rows = (
                self.rows
                if self.rows is not None
                else [{"ID": i, "VAL": f"v{i}"} for i in range(1, 6)]
            )
        return ExecutionResult(
            status="ok", rows=rows, columns=list(rows[0].keys()) if rows else [], duration_ms=5
        )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    ws = Workspace.init(tmp_path / "ws")
    monkeypatch.chdir(ws.root)
    return ws


@pytest.fixture(autouse=True)
def _isolated_user_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # keep the developer's real ~/.grayson/config.toml user id out of test facts
    monkeypatch.setenv("GRAYSON_CONFIG_DIR", str(tmp_path / "user-config"))
    monkeypatch.delenv("GRAYSON_USER_ID", raising=False)


@pytest.fixture
def fake_snow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SNOW_CMD_ENV, json.dumps([sys.executable, str(FAKE_SNOW)]))


#: rows any required chart can be drawn from: a category, two measures, and
#: enough of them for a correlation (MIN_PAIRS_FOR_CORRELATION)
CHART_ROWS = [{"K": f"k{i:02d}", "V": i * 3 % 17, "W": (i * 7) % 11} for i in range(40)]


def close_checkpoint(
    session,
    key: str,
    evidence: list[str],
    note: str = "",
    actor: str = "agent",
    overrides_dir=None,
):
    """`engine.complete_checkpoint`, satisfying the workflow's chart
    requirements the way an agent would: a chart of the first allowed kind per
    requirement, built from a query over the session's target and cited as
    evidence. Checkpoints with no requirement pass straight through."""
    from grayson.charts import add_chart
    from grayson.core import engine
    from grayson.core.run import run_statement

    check = engine.workflow_for(session, overrides_dir).check(key)
    charts: list[str] = []
    if check is not None and check.charts:
        target = (session.targets or ["DB.S.T1"])[0]
        out = run_statement(
            session, f"SELECT * FROM {target}", executor=FakeExecutor(rows=CHART_ROWS)
        )
        evidence = [*evidence, out["qid"]]
        for n, req in enumerate(check.charts, 1):
            kind = next(
                k for k in ("bar", "line", "histogram", "scatter", "correlation") if req.allows(k)
            )
            args = {
                "bar": dict(x="K", y=["V"]),
                "line": dict(x="K", y=["V"]),
                "histogram": dict(x="V", y=[]),
                "scatter": dict(x="V", y=["W"]),
                "correlation": dict(x="", y=[], columns=["V", "W"]),
            }[kind]
            spec = add_chart(session, out["qid"], kind, title=f"{key} chart {n}", **args)
            charts.append(spec["chart_id"])
    return engine.complete_checkpoint(session, key, evidence, note, actor, overrides_dir, charts)
