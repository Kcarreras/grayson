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
        if "INFORMATION_SCHEMA.TABLES" in sql.upper():
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
