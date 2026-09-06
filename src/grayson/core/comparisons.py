"""Versioned migration/release comparisons with explicit coverage and provenance.

Each environment is an existing session, so its own connection, scope, budget
and query trail apply. Full-relation summaries supplement bounded row extracts;
partial extracts can never certify record parity.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from typing import Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp

from grayson.core.criteria import decimal_precision, digest
from grayson.core.run import check_statement, run_statement
from grayson.core.session import Session, resolve_session_id
from grayson.util import is_object_name, utcnow


class Side(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    table: str
    label: str = Field(min_length=1, max_length=120)
    filter: str = Field(default="", max_length=10000)
    window: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def valid_table(self):
        if not is_object_name(self.table) or self.table.count(".") != 2:
            raise ValueError("comparison tables must be fully qualified names")
        self.table = self.table.upper()
        return self


class Mapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: str = Field(min_length=1, max_length=255)
    right: str = Field(min_length=1, max_length=255)
    kind: Literal["exact", "numeric"] = "exact"
    absolute_tolerance: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    relative_percent: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    aggregate: bool = False

    @model_validator(mode="after")
    def numeric_tolerance(self):
        if self.kind != "numeric" and (
            self.absolute_tolerance or self.relative_percent or self.aggregate
        ):
            raise ValueError("tolerances and SUM aggregates require numeric columns")
        return self


class Comparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    left: Side
    right: Side
    keys: list[Mapping] = Field(min_length=1, max_length=16)
    columns: list[Mapping] = Field(default_factory=list, max_length=100)
    group_by: list[str] = Field(default_factory=list, max_length=5)
    allowed_missing_left: int = Field(default=0, ge=0)
    allowed_missing_right: int = Field(default=0, ge=0)
    allowed_changed_rows: int = Field(default=0, ge=0)
    row_count_tolerance: int = Field(default=0, ge=0)
    max_rows: int = Field(default=100000, ge=1, le=1000000)
    example_limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_mappings(self):
        for side in ("left", "right"):
            names = [getattr(c, side).casefold() for c in [*self.keys, *self.columns]]
            if len(names) != len(set(names)):
                raise ValueError(f"each {side} column may be mapped only once")
        if any(c.kind != "exact" or c.aggregate for c in self.keys):
            raise ValueError("matching keys require exact equality")
        names = {c.left for c in [*self.keys, *self.columns]}
        if len(self.group_by) != len(set(self.group_by)) or not set(self.group_by) <= names:
            raise ValueError("group_by must name unique mapped left columns")
        return self


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _filter(text: str) -> str:
    if not text.strip():
        return ""
    trees = sqlglot.parse(f"SELECT 1 WHERE {text}", read="snowflake")
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise ValueError("filter must be one predicate")
    tree = trees[0]
    if set(k for k, v in tree.args.items() if v is not None) - {"expressions", "where"}:
        raise ValueError("filter must contain only a WHERE predicate")
    predicate = tree.args.get("where")
    if not predicate or any(predicate.find_all(exp.Select, exp.Table, exp.Subquery)):
        raise ValueError("filters cannot query other tables")
    return " " + predicate.sql(dialect="snowflake")


def _queries(spec: Comparison, side: str) -> dict:
    src = getattr(spec, side)
    mappings = [*spec.keys, *spec.columns]
    projection = ", ".join(
        f"{_ident(getattr(c, side))} AS {_ident('C' + str(i))}" for i, c in enumerate(mappings)
    )
    relation = exp.to_table(src.table, dialect="snowflake").sql(dialect="snowflake")
    where = _filter(src.filter)
    keys = ", ".join(_ident(getattr(c, side)) for c in spec.keys)
    nulls = " OR ".join(f"{_ident(getattr(c, side))} IS NULL" for c in spec.keys)
    sums = "".join(
        f", SUM({_ident(getattr(c, side))}) AS {_ident('A' + str(i))}"
        for i, c in enumerate(spec.columns)
        if c.aggregate
    )
    return {
        "rows": f"SELECT {projection} FROM {relation}{where}",
        "summary": f"SELECT COUNT(*) AS N, "
        f"COALESCE(SUM(CASE WHEN {nulls} THEN 1 ELSE 0 END),0) AS NULL_KEYS"
        f"{sums} FROM {relation}{where}",
        "duplicates": f"SELECT COUNT(*) AS DUPLICATE_KEYS FROM "
        f"(SELECT {keys} FROM {relation}{where} GROUP BY {keys} "
        "HAVING COUNT(*) > 1) AS DUPLICATES",
    }


def _session(owner: Session, side: Side) -> Session:
    return Session(owner.workspace, resolve_session_id(owner.workspace, side.session_id))


def create(owner: Session, data: dict) -> dict:
    if owner.stage == "closed":
        raise ValueError("create comparisons in an open session")
    spec = Comparison.model_validate(data)
    if owner.get_meta(f"comparison:{spec.id}") is not None:
        raise ValueError("comparison id already exists; use a new id for a revised contract")
    connections, sql = {}, {}
    for side in ("left", "right"):
        source = getattr(spec, side)
        s = _session(owner, source)
        source.session_id = s.id
        if source.table not in s.scope_tables:
            raise ValueError(f"{side} table is outside its session scope; request scope first")
        if s.stage == "closed":
            raise ValueError(f"{side} session must be open for fresh comparison")
        connections[side] = s.connection
        sql[side] = _queries(spec, side)
        for statement in sql[side].values():
            verdict = check_statement(s, statement)
            if not verdict["allowed"]:
                raise ValueError(verdict.get("reason") or "comparison query is blocked")
    value = {
        "format": 1,
        "spec": spec.model_dump(mode="json"),
        "connections": connections,
        "sql": sql,
        "created_at": utcnow(),
    }
    value["digest"] = digest({k: value[k] for k in ("spec", "connections", "sql")})
    con = owner._con()
    try:
        # Immutable definitions: a revised comparison gets a new id.
        con.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)",
            (f"comparison:{spec.id}", json.dumps(value)),
        )
        con.commit()
    except Exception as e:
        import sqlite3

        if isinstance(e, sqlite3.IntegrityError):
            raise ValueError(
                "comparison id already exists; use a new id for a revised contract"
            ) from e
        raise
    finally:
        con.close()
    owner.log_event("agent", "comparison_created", value)
    return value


def show(owner: Session, comparison_id: str) -> dict:
    value = json.loads(owner.get_meta(f"comparison:{comparison_id}") or "null")
    if not value or value.get("format") != 1:
        raise ValueError("no supported comparison with this id")
    Comparison.model_validate(value["spec"])
    if value["digest"] != digest({k: value[k] for k in ("spec", "connections", "sql")}):
        raise ValueError("comparison definition changed; create a new contract")
    return value


def inventory(owner: Session) -> list[dict]:
    out = []
    for key in sorted(owner.meta_all()):
        if key.startswith("comparison:"):
            try:
                out.append(show(owner, key.split(":", 1)[1]))
            except (ValueError, KeyError) as e:
                out.append({"id": key.split(":", 1)[1], "error": str(e)})
    return out


def _number(value) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("numeric observation is null or boolean")
    try:
        n = Decimal(str(value))
        if not n.is_finite():
            raise InvalidOperation
        return n
    except InvalidOperation as e:
        raise ValueError(f"non-finite or non-numeric observation: {value}") from e


def _equal(left, right, mapping: Mapping) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if mapping.kind == "exact":
        return type(left) is type(right) and left == right
    a, b = _number(left), _number(right)
    try:
        with localcontext() as ctx:
            ctx.prec = decimal_precision(
                a, b, mapping.absolute_tolerance, mapping.relative_percent, Decimal(100)
            )
            tolerance = max(mapping.absolute_tolerance, abs(a) * mapping.relative_percent / 100)
            return abs(a - b) <= tolerance
    except DecimalException as e:
        raise ValueError("observed values exceed supported numeric precision") from e


def _read(s: Session, output: dict, limit: int) -> tuple[list[dict], bool]:
    if output["status"] != "executed" or output.get("cache_error"):
        raise ValueError(
            output.get("reason")
            or output.get("error")
            or output.get("cache_error")
            or output["status"]
        )
    q = s.query_row(output["qid"])
    columns, rows = s.cache.rows(output["qid"], limit=limit + 1)
    if q["row_count"] and not rows:
        raise ValueError("cached evidence unavailable; rerun comparison")
    complete = not q.get("truncated") and q["row_count"] <= limit and len(rows) == q["row_count"]
    return [dict(zip(columns, r, strict=True)) for r in rows[:limit]], complete


def _capture(owner: Session, definition: dict, spec: Comparison, side: str, executor) -> dict:
    source = getattr(spec, side)
    s = _session(owner, source)
    evidence, errors, data = [], [], {}
    for kind in ("rows", "summary", "duplicates"):
        sql = definition["sql"][side][kind]
        output = run_statement(
            s, sql, label=f"Compare {spec.name}: {side} {kind}", executor=executor
        )
        evidence.append(
            {
                "session_id": s.id,
                "qid": output["qid"],
                "kind": kind,
                "sql": sql,
                "status": output["status"],
                "observed_at": utcnow(),
                "connection": s.connection,
                "table": source.table,
            }
        )
        try:
            rows, complete = _read(s, output, spec.max_rows if kind == "rows" else 1)
            if kind == "rows":
                data.update(rows=rows, rows_complete=complete)
            elif complete and len(rows) == 1:
                data[kind] = {k.upper(): v for k, v in rows[0].items()}
            else:
                raise ValueError(f"{kind} requires one complete result row")
        except (ValueError, OSError) as e:
            errors.append(f"{side} {kind}: {e}")
    return {
        "session_id": s.id,
        "connection": s.connection,
        "table": source.table,
        "label": source.label,
        "filter": source.filter,
        "window": source.window,
        "evidence": evidence,
        "errors": errors,
        **data,
    }


def run(owner: Session, comparison_id: str, *, executors: dict | None = None) -> dict:
    definition = show(owner, comparison_id)
    spec = Comparison.model_validate(definition["spec"])
    previous = json.loads(owner.get_meta(f"comparison_report:{spec.id}") or "null")
    if previous and previous.get("format") != 1:
        raise ValueError("unsupported saved report format; preserved unchanged")
    if owner.stage == "closed":
        raise ValueError("run comparisons in an open session")
    for side in ("left", "right"):
        source = getattr(spec, side)
        s = _session(owner, source)
        if s.connection != definition["connections"][side]:
            raise ValueError(f"{side} connection changed since the comparison was declared")
        if s.stage == "closed" or source.table not in s.scope_tables:
            raise ValueError(f"{side} needs an open session with the declared table in scope")
    start = utcnow()
    left = _capture(owner, definition, spec, "left", (executors or {}).get("left"))
    right = _capture(owner, definition, spec, "right", (executors or {}).get("right"))
    results, errors = [], [*left["errors"], *right["errors"]]

    def result(name, observed, allowed, complete=True, details="", *, failure_proven=False):
        if failure_proven and observed is not None and observed > allowed:
            status = "fail"
        else:
            status = "unproven" if not complete else "pass" if observed <= allowed else "fail"
        labels = {
            "left_null_keys": "Baseline · null keys",
            "right_null_keys": "Candidate · null keys",
            "left_duplicate_keys": "Baseline · duplicate keys",
            "right_duplicate_keys": "Candidate · duplicate keys",
            "row_count_difference": "Total row-count difference",
            "missing_left": "Candidate-only keys",
            "missing_right": "Baseline-only keys",
            "changed_rows": "Changed records",
        }
        results.append(
            {
                "name": name,
                "label": labels[name],
                "status": status,
                "observed": observed,
                "allowed": allowed,
                "details": details,
            }
        )

    for name, data in (("left", left), ("right", right)):
        for kind, field in (("summary", "NULL_KEYS"), ("duplicates", "DUPLICATE_KEYS")):
            try:
                n = _number(data[kind][field])
                if n < 0 or n != int(n):
                    raise ValueError("count must be a nonnegative integer")
                result(f"{name}_{field.lower()}", int(n), 0)
            except (KeyError, ValueError) as e:
                result(f"{name}_{field.lower()}", None, 0, False, str(e))
    counts = {}
    for side, data in (("left", left), ("right", right)):
        try:
            n = _number(data["summary"]["N"])
            if n < 0 or n != int(n):
                raise ValueError("row count must be a nonnegative integer")
            counts[side] = int(n)
            if data.get("rows_complete") and len(data.get("rows", [])) != int(n):
                data["rows_complete"] = False
                errors.append(f"{side}: row count changed between extract and summary queries")
        except (KeyError, ValueError) as e:
            errors.append(f"{side} row count: {e}")
    result(
        "row_count_difference",
        abs(counts["left"] - counts["right"]) if len(counts) == 2 else None,
        spec.row_count_tolerance,
        len(counts) == 2,
    )
    aggregates = []
    for i, mapping in enumerate(spec.columns):
        if not mapping.aggregate:
            continue
        try:
            a, b = left["summary"][f"A{i}"], right["summary"][f"A{i}"]
            equal = _equal(a, b, mapping)
            aggregates.append(
                {
                    "column": mapping.left,
                    "left": a,
                    "right": b,
                    "status": "pass" if equal else "fail",
                    "absolute_tolerance": str(mapping.absolute_tolerance),
                    "relative_percent": str(mapping.relative_percent),
                }
            )
        except (ValueError, KeyError) as e:
            aggregates.append({"column": mapping.left, "status": "unproven", "details": str(e)})
    nkeys = len(spec.keys)

    def index(data):
        groups = defaultdict(list)
        for row in data.get("rows", []):
            if any(f"C{i}" not in row for i in range(nkeys + len(spec.columns))):
                data["rows_complete"] = False
                errors.append("Extract schema does not match declared mappings")
                continue
            key = tuple((type(row[f"C{i}"]).__name__, row[f"C{i}"]) for i in range(nkeys))
            if any(v is None for _, v in key):
                continue
            groups[key].append(row)
        return groups

    li, ri = index(left), index(right)
    complete = bool(left.get("rows_complete") and right.get("rows_complete"))
    only_left, only_right = set(li) - set(ri), set(ri) - set(li)
    changed, unknown_values, column_counts = [], 0, Counter()
    concentrations = Counter()
    canonical = [c.left for c in [*spec.keys, *spec.columns]]

    def concentrate(row, category):
        for column in spec.group_by:
            value = row[f"C{canonical.index(column)}"]
            concentrations[(column, json.dumps(value, default=str), category)] += 1

    for key in only_left:
        concentrate(li[key][0], "missing_right")
    for key in only_right:
        concentrate(ri[key][0], "missing_left")
    ambiguous = 0
    for key in li.keys() & ri.keys():
        if len(li[key]) != 1 or len(ri[key]) != 1:
            ambiguous += 1
            continue
        a, b = li[key][0], ri[key][0]
        differences = []
        for i, mapping in enumerate(spec.columns, nkeys):
            try:
                if not _equal(a[f"C{i}"], b[f"C{i}"], mapping):
                    differences.append(
                        {"column": mapping.left, "left": a[f"C{i}"], "right": b[f"C{i}"]}
                    )
                    column_counts[mapping.left] += 1
            except ValueError:
                unknown_values += 1
        if differences:
            changed.append({"key": [v for _, v in key], "differences": differences})
            concentrate(a, "changed_values")
    result("missing_left", len(only_right), spec.allowed_missing_left, complete)
    result("missing_right", len(only_left), spec.allowed_missing_right, complete)
    # A capped extract gives a lower bound on changed records only when unseen
    # duplicate keys cannot invalidate the observed one-to-one matches.
    unique_keys = all(
        r["status"] == "pass"
        for r in results
        if r["name"] in {"left_duplicate_keys", "right_duplicate_keys"}
    )
    result(
        "changed_rows",
        len(changed),
        spec.allowed_changed_rows,
        complete and not ambiguous and not unknown_values,
        f"{ambiguous} ambiguous keys; {unknown_values} non-numeric observations",
        failure_proven=unique_keys,
    )
    statuses = [r["status"] for r in [*results, *aggregates]]
    verdict = "fail" if "fail" in statuses else "unproven" if "unproven" in statuses else "pass"
    if errors and verdict == "pass":
        verdict = "unproven"
    report = {
        "format": 1,
        "id": spec.id,
        "name": spec.name,
        "definition_digest": definition["digest"],
        "started_at": start,
        "completed_at": utcnow(),
        "verdict": verdict,
        "left": {k: v for k, v in left.items() if k not in {"rows", "summary", "duplicates"}},
        "right": {k: v for k, v in right.items() if k not in {"rows", "summary", "duplicates"}},
        "row_counts": counts,
        "results": results,
        "aggregates": aggregates,
        "coverage": {
            "record_parity": "complete" if complete else "incomplete",
            "extracted_rows": {
                "left": len(left.get("rows", [])),
                "right": len(right.get("rows", [])),
            },
            "mapped_columns": canonical,
            "unmapped_columns": "not compared",
            "snapshot": "Sequential observations, not an atomic cross-environment snapshot.",
            "note": "Incomplete extracts cannot prove parity or missing keys. Observed value "
            "mismatches can still fail when full-relation checks prove unique matching keys. "
            "Aggregates and key-quality summaries scan each filtered "
            "relation independently. Use stable release inputs or snapshot tables "
            "when concurrent writes could change the data.",
        },
        "examples": {
            "missing_left": [
                [v for _, v in k] for k in sorted(only_right, key=repr)[: spec.example_limit]
            ],
            "missing_right": [
                [v for _, v in k] for k in sorted(only_left, key=repr)[: spec.example_limit]
            ],
            "changed": sorted(changed, key=lambda r: repr(r["key"]))[: spec.example_limit],
            "duplicate_left": [[v for _, v in k] for k in sorted(li, key=repr) if len(li[k]) > 1][
                : spec.example_limit
            ],
            "duplicate_right": [[v for _, v in k] for k in sorted(ri, key=repr) if len(ri[k]) > 1][
                : spec.example_limit
            ],
        },
        "example_limit": spec.example_limit,
        "column_mismatches": dict(column_counts),
        "concentrations": [
            {"column": c, "value": json.loads(v), "category": category, "count": n}
            for (c, v, category), n in concentrations.most_common(50)
        ],
        "concentrations_omitted": max(0, len(concentrations) - 50),
        "errors": errors,
    }
    owner.set_meta(f"comparison_report:{spec.id}", json.dumps(report, default=str))
    owner.log_event("system", "comparison_run", report)
    return report


def latest_report(owner: Session, comparison_id: str) -> dict:
    show(owner, comparison_id)
    report = json.loads(owner.get_meta(f"comparison_report:{comparison_id}") or "null")
    if not report or report.get("format") != 1:
        raise ValueError("no supported comparison report; run the comparison first")
    return report
