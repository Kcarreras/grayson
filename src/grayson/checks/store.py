"""External checks library: deterministic check results dropped in by automation.

Teams already run scheduled deterministic checks outside grayson — Airflow DAGs,
dbt tests, data-quality jobs. This store makes those results a library asset:
automation dumps JSON files under `checks/` (committed, shared via the team
library like knowledge and views), and grayson surfaces the latest result per
check at session start. A failing external check on a target table is a
pre-vetted lead an agent should replicate first, before open-ended hunting.

grayson never runs these checks; it only reads, validates, and reports them.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from grayson.util import read_json, write_json

CheckStatus = Literal["pass", "fail", "warn", "error", "skipped"]

_CHECK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

#: where `checks ingest` writes normalized results (files dropped anywhere
#: under checks/ are read either way; this just keeps ingested history tidy)
INGESTED_SUBDIR = "ingested"

#: runs kept per check file when ingesting — enough history to see a trend,
#: bounded so the library repo doesn't grow without limit
MAX_INGESTED_RUNS = 25

CHECKS_README = """\
# External checks

Drop deterministic check results here as JSON — from Airflow, dbt tests,
data-quality jobs, anything scheduled. grayson reads every `*.json` under this
directory and shows agents the latest result per check at session start, so a
failing check on a target table becomes an immediate, pre-vetted lead.

A file may contain a single result object, a list of them, or
`{"results": [...]}`. Each result:

```json
{
  "check_id": "orders_null_email",         // required, stable across runs
  "name": "orders: email should not be NULL",
  "status": "fail",                        // pass | fail | warn | error | skipped
  "tables": ["ANALYTICS.SHOP.ORDERS"],     // fully-qualified tables it covers
  "run_at": "2026-08-24T06:00:00Z",        // required, ISO timestamp
  "source": "airflow",                     // which system ran it
  "metrics": {"null_count": 812},          // any numbers worth keeping
  "details": "812 rows with NULL email since 2026-08-20",
  "sql": "SELECT COUNT(*) FROM ... WHERE email IS NULL",  // lets agents replicate
  "url": "https://airflow.internal/dags/qa_orders/runs/...",
  "ttl_hours": 26                          // expected cadence; older = overdue
}
```

Automation can write files here directly (any layout, e.g. one file per DAG),
or pipe results through `grayson checks ingest <file>` which validates and keeps
a bounded per-check history under `ingested/`. A dbt run_results.json is
detected and converted automatically (add `--manifest target/manifest.json` to
resolve tables and compiled SQL). Full setup guide: docs/CHECKS.md.
"""


class CheckResult(BaseModel):
    check_id: str
    status: CheckStatus
    run_at: str
    name: str = ""
    tables: list[str] = Field(default_factory=list)
    source: str = ""
    severity: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)
    details: str = ""
    sql: str = ""
    url: str = ""
    ttl_hours: float | None = Field(default=None, ge=0)

    @field_validator("check_id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not _CHECK_ID_RE.match(v):
            raise ValueError(
                f"check_id {v!r} must be 1-100 chars of letters, digits, '.', '_' or '-'"
            )
        return v

    @field_validator("run_at")
    @classmethod
    def _valid_run_at(cls, v: str) -> str:
        if _parse_ts(v) is None:
            raise ValueError(f"run_at {v!r} is not an ISO timestamp")
        return v

    @field_validator("tables")
    @classmethod
    def _upper_tables(cls, v: list[str]) -> list[str]:
        return [str(t).upper() for t in v]


def _parse_ts(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _age_hours(result: CheckResult, now: datetime) -> float | None:
    ts = _parse_ts(result.run_at)
    return None if ts is None else max(0.0, (now - ts).total_seconds() / 3600)


def _extract_results(data: object) -> list[dict]:
    """Accept a single result object, a list, or {'results': [...]}."""
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        return [r for r in data["results"] if isinstance(r, dict)]
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


class ChecksStore:
    def __init__(self, checks_dir: Path):
        self.dir = checks_dir

    # -- read ------------------------------------------------------------

    def load(self) -> tuple[list[CheckResult], list[dict]]:
        """Every valid result under checks/, plus per-file/per-entry errors.

        Invalid entries never fail the load — automation drops files here
        unattended, and one malformed run must not hide every other check.
        """
        results: list[CheckResult] = []
        errors: list[dict] = []
        if not self.dir.is_dir():
            return results, errors
        for path in sorted(self.dir.rglob("*.json")):
            rel = str(path.relative_to(self.dir))
            try:
                data = read_json(path)
            except (json.JSONDecodeError, OSError) as e:
                errors.append({"file": rel, "error": f"unreadable: {e}"})
                continue
            entries = _extract_results(data)
            if not entries:
                errors.append({"file": rel, "error": "no result objects found"})
                continue
            for i, entry in enumerate(entries):
                try:
                    results.append(CheckResult.model_validate(entry))
                except ValidationError as e:
                    first = e.errors()[0]
                    errors.append(
                        {
                            "file": rel,
                            "entry": i,
                            "error": f"{'.'.join(str(x) for x in first['loc'])}: {first['msg']}",
                        }
                    )
        return results, errors

    def latest(self, tables: list[str] | None = None) -> list[CheckResult]:
        """Latest run per check_id, optionally only checks touching `tables`."""
        results, _ = self.load()
        wanted = {t.upper() for t in tables} if tables else None
        by_id: dict[str, CheckResult] = {}
        for r in results:
            if wanted is not None and not (wanted & set(r.tables)):
                continue
            prev = by_id.get(r.check_id)
            if prev is None or r.run_at > prev.run_at:
                by_id[r.check_id] = r
        return sorted(by_id.values(), key=lambda r: r.check_id)

    def history(self, check_id: str) -> list[CheckResult]:
        results, _ = self.load()
        runs = [r for r in results if r.check_id == check_id]
        return sorted(runs, key=lambda r: r.run_at, reverse=True)

    def summary(self, tables: list[str] | None = None) -> dict:
        """The session-start picture: latest per check, failures first.

        `failing` carries the full result (details, metrics, sql) because those
        are the checks an agent acts on; everything else is compact counts.
        """
        now = datetime.now(UTC)
        latest = self.latest(tables)
        _, errors = self.load()
        by_status: dict[str, int] = {}
        failing: list[dict] = []
        overdue: list[dict] = []
        compact: list[dict] = []
        for r in latest:
            by_status[r.status] = by_status.get(r.status, 0) + 1
            age = _age_hours(r, now)
            is_overdue = bool(r.ttl_hours is not None and age is not None and age > r.ttl_hours)
            compact.append(
                {
                    "check_id": r.check_id,
                    "name": r.name or r.check_id,
                    "status": r.status,
                    "tables": r.tables,
                    "run_at": r.run_at,
                    "source": r.source,
                    "age_hours": round(age, 1) if age is not None else None,
                    "overdue": is_overdue,
                }
            )
            if r.status in {"fail", "error"}:
                failing.append(r.model_dump())
            if is_overdue:
                overdue.append(
                    {"check_id": r.check_id, "run_at": r.run_at, "age_hours": round(age or 0, 1)}
                )
        return {
            "total_checks": len(latest),
            "by_status": by_status,
            "checks": compact,
            "failing": failing,
            "overdue": overdue,
            "parse_errors": errors,
        }

    # -- ingest ----------------------------------------------------------

    def ingest(
        self,
        source_path: Path,
        source: str | None = None,
        manifest_path: Path | None = None,
        ttl_hours: float | None = None,
    ) -> dict:
        """Validate an external results file (or directory of them) and fold the
        results into the library under `ingested/<check_id>.json`.

        A dbt run_results.json is recognized automatically and converted via
        the dbt adapter (pass `manifest_path` to resolve each test to its
        tables and compiled SQL). `ttl_hours` stamps a cadence expectation on
        adapter-converted results that carry none of their own.

        Idempotent: a (check_id, run_at) pair already on file is skipped, so
        re-running an automation hand-off never duplicates history. History is
        trimmed to the newest MAX_INGESTED_RUNS runs per check.
        """
        from grayson.checks.adapters import dbt_run_results_to_checks, looks_like_dbt_run_results

        manifest: dict | None = None
        if manifest_path is not None:
            manifest = read_json(manifest_path)
        paths = sorted(source_path.rglob("*.json")) if source_path.is_dir() else [source_path]
        ingested: dict[str, int] = {}
        skipped = 0
        errors: list[dict] = []
        for path in paths:
            if not path.is_file():
                errors.append({"file": str(path), "error": "file not found"})
                continue
            try:
                data = read_json(path)
            except (json.JSONDecodeError, OSError) as e:
                errors.append({"file": str(path), "error": f"unreadable: {e}"})
                continue
            if looks_like_dbt_run_results(data):
                entries = dbt_run_results_to_checks(data, manifest, ttl_hours=ttl_hours)
                if not entries:
                    errors.append({"file": str(path), "error": "dbt run_results has no test nodes"})
                    continue
            else:
                entries = _extract_results(data)
            if not entries:
                errors.append({"file": str(path), "error": "no result objects found"})
                continue
            for i, entry in enumerate(entries):
                if source and not entry.get("source"):
                    entry = {**entry, "source": source}
                try:
                    result = CheckResult.model_validate(entry)
                except ValidationError as e:
                    first = e.errors()[0]
                    errors.append(
                        {
                            "file": str(path),
                            "entry": i,
                            "error": f"{'.'.join(str(x) for x in first['loc'])}: {first['msg']}",
                        }
                    )
                    continue
                if self._fold_in(result):
                    ingested[result.check_id] = ingested.get(result.check_id, 0) + 1
                else:
                    skipped += 1
        return {
            "ingested": sum(ingested.values()),
            "checks": sorted(ingested),
            "skipped_duplicates": skipped,
            "errors": errors,
            "dir": str(self.dir / INGESTED_SUBDIR),
        }

    def _fold_in(self, result: CheckResult) -> bool:
        target = self.dir / INGESTED_SUBDIR / f"{result.check_id}.json"
        existing: list[dict] = []
        if target.is_file():
            try:
                existing = [e for e in _extract_results(read_json(target)) if isinstance(e, dict)]
            except (json.JSONDecodeError, OSError):
                existing = []
        if any(
            e.get("check_id") == result.check_id and e.get("run_at") == result.run_at
            for e in existing
        ):
            return False
        existing.append(result.model_dump())
        existing.sort(key=lambda e: str(e.get("run_at", "")), reverse=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json(target, existing[:MAX_INGESTED_RUNS])
        return True


def scaffold_checks_dir(checks_dir: Path) -> None:
    """Create checks/ with its format README (idempotent)."""
    checks_dir.mkdir(parents=True, exist_ok=True)
    readme = checks_dir / "README.md"
    if not readme.exists():
        readme.write_text(CHECKS_README, encoding="utf-8")
