"""Executed evidence becomes a reviewed, repeatable test of a team's expectations.

Definitions are additive YAML assets under checks/regressions/. Every replay
uses the ordinary session guard and audit; results use the existing checks
format, so old clients can still consume them without understanding definitions.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import sqlglot
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp

from grayson.checks.store import CheckResult, ChecksStore
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.executor.snow import Executor
from grayson.identity import get_user_id
from grayson.util import atomic_write_text, ensure_within, utcnow
from grayson.workspace import Workspace

_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class RegressionError(ValueError):
    pass


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["no_rows", "scalar"] = "no_rows"
    column: str = ""
    operator: Literal["eq", "ne", "lt", "lte", "gt", "gte", "between"] = "eq"
    value: Decimal | None = None
    upper: Decimal | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Expectation:
        if self.kind == "no_rows":
            if (
                self.column
                or self.value is not None
                or self.upper is not None
                or self.operator != "eq"
            ):
                raise ValueError("no_rows takes no column, operator, or numeric bounds")
        elif not self.column.strip() or self.value is None:
            raise ValueError("scalar requires a column and numeric value")
        for bound in (self.value, self.upper):
            if bound is not None and not bound.is_finite():
                raise ValueError("expectation bounds must be finite")
        if self.operator == "between":
            if self.upper is None or self.value is None or self.upper < self.value:
                raise ValueError("between requires upper >= value (inclusive bounds)")
        elif self.upper is not None:
            raise ValueError("upper is only valid for between")
        return self

    def label(self) -> str:
        if self.kind == "no_rows":
            return "Query returns no violating rows"
        if self.operator == "between":
            return f"{self.column} between {self.value} and {self.upper} (inclusive)"
        symbols = {"eq": "=", "ne": "≠", "lt": "<", "lte": "≤", "gt": ">", "gte": "≥"}
        return f"{self.column} {symbols[self.operator]} {self.value}"


class RegressionCheck(BaseModel):
    # Preserve additive fields from other clients; never rewrite a newer format.
    model_config = ConfigDict(extra="allow")

    format: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=4000)
    sql: str = Field(min_length=1, max_length=100000)
    tables: list[str] = Field(min_length=1)
    expectation: Expectation
    state: Literal["proposed", "active", "retired"] = "proposed"
    source_session: str
    source_qid: str
    source_connection: str
    created_at: str
    created_by: str | None = None
    source_observation: dict = Field(default_factory=dict)
    source_digest: str = ""
    approved_digest: str = ""
    reviewed_by: str | None = None
    reviewed_at: str | None = None

    def digest(self) -> str:
        content = self.model_dump(
            mode="json",
            include={
                "format",
                "id",
                "name",
                "description",
                "sql",
                "tables",
                "expectation",
                "source_session",
                "source_qid",
                "source_connection",
            },
        )
        return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    def view(self) -> dict:
        out = self.model_dump(mode="json")
        out.update(expectation_text=self.expectation.label(), digest=self.digest())
        out["review_current"] = (
            self.state == "active" and self.approved_digest == self.digest() == self.source_digest
        )
        out["result_id"] = f"regression.{self.id}"
        return out


class RegressionStore:
    def __init__(self, checks_dir: Path):
        self.dir = checks_dir / "regressions"

    def path(self, check_id: str) -> Path:
        if not _ID.fullmatch(check_id):
            raise RegressionError(
                "check id must be 1–64 lowercase letters, digits, - or _, starting with a letter"
            )
        path = self.dir / f"{check_id}.yaml"
        ensure_within(self.dir.parent, path)
        if path.is_symlink():
            raise RegressionError(f"{path}: check definitions must not be symlinks")
        return path

    def read(self, check_id: str) -> RegressionCheck:
        path = self.path(check_id)
        if not path.is_file():
            raise RegressionError(f"no regression check '{check_id}'")
        try:
            check = RegressionCheck.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (ValueError, yaml.YAMLError) as e:
            raise RegressionError(
                f"{path.name}: invalid or unsupported regression definition: {e}"
            ) from e
        if check.id != check_id:
            raise RegressionError(f"{path.name}: check id does not match its filename")
        return check

    def inventory(self, tables: list[str] | None = None) -> dict:
        wanted = {t.upper() for t in tables or []}
        checks, errors = [], []
        for path in sorted(self.dir.glob("*.yaml")):
            try:
                check = self.read(path.stem)
                if not wanted or wanted.intersection(t.upper() for t in check.tables):
                    checks.append(check.view())
            except (ValueError, OSError) as e:
                errors.append({"file": path.name, "error": str(e)})
        return {"checks": checks, "errors": errors}

    def save(self, check: RegressionCheck, *, create: bool = False) -> None:
        path = self.path(check.id)
        text = yaml.safe_dump(check.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        if create:
            try:
                with path.open("x", encoding="utf-8", newline="") as stream:
                    stream.write(text)
            except FileExistsError as e:
                raise RegressionError(
                    f"check '{check.id}' already exists — choose a new id for a new expectation"
                ) from e
        else:
            atomic_write_text(path, text)


def _sync(workspace: Workspace, paths: Path | list[Path], message: str) -> dict:
    from grayson.library import commit_library_paths

    root = workspace.config.library_path or workspace.root
    try:
        return commit_library_paths(
            workspace,
            [
                path.relative_to(root).as_posix()
                for path in ([paths] if isinstance(paths, Path) else paths)
            ],
            message,
            via="regression",
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        return {"ok": False, "detail": str(e)}


def evaluate(session: Session, qid: str, expectation: Expectation) -> dict:
    query = session.query_row(qid)
    if query is None or query["status"] != "executed":
        raise RegressionError("expectations need a successfully executed query")
    if expectation.kind == "no_rows":
        count = query["row_count"]
        if count is None or (count == 0 and query.get("truncated")):
            raise RegressionError("row count is unavailable or incomplete")
        return {
            "status": "pass" if count == 0 else "fail",
            "observed": count,
            "details": f"{count} violating row(s) returned"
            + (" (capped)" if query.get("truncated") else ""),
        }
    if query["row_count"] != 1 or query.get("truncated"):
        raise RegressionError("scalar expectation requires exactly one complete result row")
    columns, rows = session.cache.rows(qid, limit=2)
    if len(rows) != 1:
        raise RegressionError("cached result is unavailable — rerun the query")
    matches = [c for c in columns if c == expectation.column]
    if not matches:
        matches = [c for c in columns if c.casefold() == expectation.column.casefold()]
    if len(matches) != 1:
        raise RegressionError(f"expected one column named '{expectation.column}', got {columns}")
    raw = rows[0][columns.index(matches[0])]
    try:
        if raw is None or isinstance(raw, bool):
            raise InvalidOperation
        observed = Decimal(str(raw))
        if not observed.is_finite():
            raise InvalidOperation
    except InvalidOperation as e:
        raise RegressionError(f"'{expectation.column}' is not a finite number") from e
    value = expectation.value
    verdict = {
        "eq": lambda: observed == value,
        "ne": lambda: observed != value,
        "lt": lambda: observed < value,
        "lte": lambda: observed <= value,
        "gt": lambda: observed > value,
        "gte": lambda: observed >= value,
        "between": lambda: value <= observed <= expectation.upper,
    }[expectation.operator]()
    return {
        "status": "pass" if verdict else "fail",
        "observed": str(observed),
        "details": f"Observed {expectation.column} = {observed}; expected {expectation.label()}",
    }


def propose_check(
    session: Session, qid: str, check_id: str, name: str, description: str, expectation: dict
) -> dict:
    store = RegressionStore(session.workspace.checks_dir)
    store.path(check_id)  # validate before touching any data
    if ChecksStore(session.workspace.checks_dir).history(f"regression.{check_id}"):
        raise RegressionError(
            f"results already use 'regression.{check_id}' — "
            "choose a new id to preserve their history"
        )
    query = session.query_row(qid)
    if query is None or query["status"] != "executed":
        raise RegressionError(
            "save a check from a query that successfully executed in this session"
        )
    sql = query["sql_raw"]
    tree = sqlglot.parse_one(sql, read="snowflake")
    if not isinstance(tree, exp.Select | exp.Union | exp.Intersect | exp.Except):
        raise RegressionError("a regression check must come from a SELECT query")
    tables = json.loads(query.get("tables_json") or "[]")
    if not tables:
        raise RegressionError(
            "the source query must read a table — SELECT constants cannot guard a regression"
        )
    expected = Expectation.model_validate(expectation)
    observation = evaluate(session, qid, expected)
    check = RegressionCheck(
        id=check_id,
        name=name.strip(),
        description=description.strip(),
        sql=sql,
        tables=tables,
        expectation=expected,
        source_session=session.id,
        source_qid=qid,
        source_connection=session.connection,
        created_at=utcnow(),
        created_by=get_user_id(),
        source_observation=observation,
    )
    check.source_digest = check.digest()
    store.save(check, create=True)
    session.log_event("agent", "regression_proposed", {"check_id": check.id, "qid": qid})
    return {
        "check": check.view(),
        "library_sync": _sync(
            session.workspace, store.path(check.id), f"grayson regression: propose {check.id}"
        ),
        "next": "The user reviews SQL and expectation in the Checks console, then activates it.",
    }


def decide_check(
    workspace: Workspace, check_id: str, action: str, digest: str, *, actor: str = "agent"
) -> dict:
    if actor != "user":
        raise RegressionError("activating or retiring a regression check is a user action")
    if action not in {"activate", "retire"}:
        raise RegressionError("action must be activate or retire")
    store = RegressionStore(workspace.checks_dir)
    check = store.read(check_id)
    if digest != check.digest():
        raise RegressionError(
            "the check changed since review — reload and review its SQL and expectation"
        )
    if action == "activate" and check.state == "retired":
        raise RegressionError("retired checks stay in history — propose a new check to replace one")
    if action == "activate" and check.source_digest != check.digest():
        raise RegressionError(
            "the definition changed since its source evidence — "
            "execute the revised query and propose a new check"
        )
    check.state = "active" if action == "activate" else "retired"
    check.approved_digest = check.digest() if action == "activate" else check.approved_digest
    check.reviewed_at, check.reviewed_by = utcnow(), get_user_id()
    store.save(check)
    return {
        "check": check.view(),
        "library_sync": _sync(
            workspace, store.path(check.id), f"grayson regression: {action} {check.id}"
        ),
    }


def run_checks(
    session: Session, check_ids: list[str] | None = None, *, executor: Executor | None = None
) -> dict:
    if session.stage == "closed":
        raise RegressionError("start an open session to rerun regression checks")
    store = RegressionStore(session.workspace.checks_dir)
    inventory = store.inventory(session.targets)
    ids = (
        list(dict.fromkeys(check_ids))
        if check_ids is not None
        else [c["id"] for c in inventory["checks"] if c["state"] == "active"]
    )
    # Validate the entire selection first; a typo must not cause half a run.
    selected = [store.read(check_id) for check_id in ids]
    for check in selected:
        if not check.view()["review_current"]:
            raise RegressionError(f"'{check.id}' needs a current human review before it can run")
    results = []
    recorded_paths = []
    for check in selected:
        output = run_statement(
            session, check.sql, label=f"regression: {check.name}", executor=executor
        )
        qid = output["qid"]
        try:
            if output["status"] != "executed":
                raise RegressionError(
                    output.get("reason") or output.get("error") or output["status"]
                )
            outcome = evaluate(session, qid, check.expectation)
        except (ValueError, OSError) as e:
            outcome = {"status": "error", "observed": None, "details": str(e)}
        result = CheckResult(
            check_id=f"regression.{check.id}",
            name=check.name,
            status=outcome["status"],
            tables=check.tables,
            run_at=datetime.now(UTC).isoformat(),
            source="grayson",
            details=outcome["details"],
            sql=check.sql,
            metrics={
                "session_id": session.id,
                "connection": session.connection,
                "qid": qid,
                "definition_digest": check.digest(),
                "observed": outcome["observed"],
                "expectation": check.expectation.label(),
            },
        )
        entry = {
            **result.model_dump(),
            "id": check.id,
            "qid": qid,
            "evidence": [qid] if output["status"] == "executed" else [],
        }
        try:
            recorded_paths.append(ChecksStore(session.workspace.checks_dir).record(result))
        except (ValueError, OSError) as e:
            entry["persistence_error"] = str(e)
        session.log_event("agent", "regression_run", entry)
        results.append(entry)
    report = {
        "session_id": session.id,
        "results": results,
        "counts": {
            status: sum(r["status"] == status for r in results)
            for status in ("pass", "fail", "error")
        },
        "definition_errors": inventory["errors"],
        "ok": bool(results)
        and all(r["status"] == "pass" and not r.get("persistence_error") for r in results)
        and not inventory["errors"],
        "note": "Results test the approved expectation, not root cause. Cite the new query ids.",
    }
    if recorded_paths:
        report["library_sync"] = _sync(
            session.workspace, recorded_paths, "grayson regression: record replays"
        )
    return report
