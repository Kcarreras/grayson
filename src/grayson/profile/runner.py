"""Running a profile: generated SQL through the ordinary guarded path.

The point of a profiling primitive is not convenience, it is citability. Every
statement here goes through `run_statement` exactly as an agent's own query
would — guarded, capped, cached, audited, and assigned a `q_XXXX` id. So the
resulting artifacts are evidence in the only sense grayson recognises, and a
checkpoint can close against them without the agent hand-rolling forty queries
whose ids differ run to run.

grayson interprets nothing here. It fetches the numbers and lays them out; what
they mean is the agent's problem, and what to do about them is the user's.
"""

from __future__ import annotations

from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.executor.snow import Executor
from grayson.profile import plan
from grayson.profile.plan import Column, ProfilePlanError

#: a null rate at or above this is worth saying out loud. Not a threshold for
#: "wrong" — plenty of columns are legitimately sparse — just for "look at this".
NOTABLE_NULL_RATE = 0.05

#: distinct-rate at or above this, without being unique, reads as a key with
#: duplicates rather than as a genuinely repeating dimension
NEAR_UNIQUE_RATE = 0.95


class ProfileError(ValueError):
    pass


def _run(session: Session, sql: str, label: str, executor: Executor | None) -> dict:
    out = run_statement(session, sql, label=label, executor=executor)
    if out.get("status") != "executed":
        raise ProfileError(
            f"profile step '{label}' did not execute ({out.get('status')}): "
            f"{out.get('reason') or out.get('error') or 'unknown'}. "
            "The profile is incomplete; nothing was recorded as evidence for it."
        )
    return out


def describe_columns(
    session: Session, table: str, executor: Executor | None = None
) -> tuple[list[Column], str]:
    """DESCRIBE the table through the guard. Returns columns and the query id."""
    out = _run(
        session, f"DESCRIBE TABLE {plan.qualify(table)}", f"profile: describe {table}", executor
    )
    rows = session.cache.preview(out["qid"], limit=10000)
    columns = plan.parse_describe(rows)
    if not columns:
        raise ProfileError(
            f"could not read any columns for {table} — DESCRIBE returned nothing usable"
        )
    return columns, out["qid"]


def profile_table(
    session: Session,
    table: str,
    sample_rows: int = plan.DEFAULT_SAMPLE_ROWS,
    frequencies: bool = True,
    sample: bool = True,
    executor: Executor | None = None,
) -> dict:
    """Profile one table in a handful of guarded queries.

    Shape: DESCRIBE, one wide aggregate per batch of columns, optionally one
    UNION ALL of value frequencies for the low-cardinality columns, and
    optionally one sample for local statistics. A 40-column table costs four
    queries, not 160.
    """
    try:
        columns, describe_qid = describe_columns(session, table, executor)
    except ProfilePlanError as e:
        raise ProfileError(str(e.args[0] if e.args else e)) from e

    evidence = [describe_qid]
    facts: dict[str, dict] = {c.name: {"type": c.raw_type, "kind": c.kind} for c in columns}
    row_total: int | None = None

    for batch in plan.batches(columns):
        if not batch:
            continue
        sql, alias_map = plan.aggregate_sql(table, batch)
        out = _run(session, sql, f"profile: aggregates {table}", executor)
        evidence.append(out["qid"])
        rows = session.cache.preview(out["qid"], limit=1)
        if not rows:
            continue
        row = {str(k).upper(): v for k, v in rows[0].items()}
        row_total = _as_int(row.get("ROW_TOTAL")) if row_total is None else row_total
        for alias, (column, stat) in alias_map.items():
            facts[column][stat] = row.get(alias.upper())

    for fact in facts.values():
        _derive(fact, row_total)

    out_doc = {
        "table": table.upper(),
        "row_count": row_total,
        "columns": [{"column": name, **facts[name]} for name in facts],
        "evidence": evidence,
        "queries_run": len(evidence),
    }

    if frequencies:
        low_card = [
            c
            for c in columns
            if c.kind not in ("opaque",)
            and _as_int(facts[c.name].get("distinct")) is not None
            and 0 < _as_int(facts[c.name].get("distinct")) <= plan.MAX_DISTINCT_FOR_FREQUENCIES
        ]
        sql = plan.frequency_sql(table, low_card)
        if sql is not None:
            freq = _run(session, sql, f"profile: value frequencies {table}", executor)
            evidence.append(freq["qid"])
            out_doc["frequencies_qid"] = freq["qid"]
            out_doc["frequencies"] = _fold_frequencies(
                session.cache.preview(freq["qid"], limit=5000)
            )
        else:
            out_doc["frequencies_skipped"] = (
                "no column had few enough distinct values for a frequency breakdown to "
                f"mean anything (threshold {plan.MAX_DISTINCT_FOR_FREQUENCIES})"
            )

    if sample:
        smp = _run(
            session,
            plan.sample_sql(table, columns, sample_rows),
            f"profile: sample {table}",
            executor,
        )
        evidence.append(smp["qid"])
        out_doc["sample_qid"] = smp["qid"]
        out_doc["sample_rows"] = smp.get("row_count")
        out_doc["sample_note"] = (
            "cite this qid for statistics computed locally over the sample "
            "(`grayson profile stats`, `grayson profile correlate`)"
        )

    out_doc["evidence"] = evidence
    out_doc["queries_run"] = len(evidence)
    out_doc["observations"] = observations(out_doc)
    session.log_event(
        "agent",
        "table_profiled",
        {"table": table.upper(), "queries": len(evidence), "evidence": evidence},
    )
    return out_doc


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _derive(fact: dict, row_total: int | None) -> None:
    """Rates alongside counts. A null count means nothing without the denominator,
    and every consumer of this would otherwise divide it themselves."""
    non_null = _as_int(fact.get("non_null"))
    if row_total is None or non_null is None:
        return
    fact["nulls"] = row_total - non_null
    fact["null_rate"] = round((row_total - non_null) / row_total, 6) if row_total else None
    distinct = _as_int(fact.get("distinct"))
    if distinct is not None and row_total:
        fact["distinct_rate"] = round(distinct / row_total, 6)
        fact["unique"] = distinct == row_total and fact["nulls"] == 0
        fact["constant"] = distinct <= 1


def _fold_frequencies(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in rows:
        upper = {str(k).upper(): v for k, v in row.items()}
        column = str(upper.get("COLUMN_NAME") or "")
        if not column:
            continue
        out.setdefault(column, []).append(
            {"value": upper.get("VALUE"), "frequency": _as_int(upper.get("FREQUENCY"))}
        )
    for values in out.values():
        values.sort(key=lambda v: v["frequency"] or 0, reverse=True)
    return out


def observations(doc: dict) -> list[dict]:
    """Flat, mechanical notes on what the numbers show.

    Deliberately not judgements: an all-null column is *stated*, never called a
    defect. Whether it is one depends on what the column is for, which grayson
    does not know and will not guess. These are leads for the agent to chase and
    the user to adjudicate.
    """
    out = []
    total = doc.get("row_count") or 0
    for col in doc.get("columns") or []:
        name = col["column"]
        rate = col.get("null_rate") or 0
        distinct_rate = col.get("distinct_rate") or 0
        if col.get("constant") and total:
            out.append(
                {
                    "column": name,
                    "observation": "single value across every row"
                    if col.get("nulls") == 0
                    else "single value (plus nulls)",
                }
            )
        if rate == 1.0:
            out.append({"column": name, "observation": "entirely null"})
        elif rate >= NOTABLE_NULL_RATE:
            out.append({"column": name, "observation": f"null in {rate:.1%} of rows"})
        if col.get("unique"):
            out.append({"column": name, "observation": "unique across all rows (key candidate)"})
        elif distinct_rate >= NEAR_UNIQUE_RATE and total:
            # a key that has just lost its uniqueness looks exactly like this,
            # and the count of repeats is the first number anyone will want
            repeats = total - (_as_int(col.get("distinct")) or 0)
            out.append(
                {
                    "column": name,
                    "observation": f"nearly unique ({distinct_rate:.1%} distinct) but not "
                    f"quite — {repeats} row(s) beyond the distinct count",
                }
            )
        blank = _as_int(col.get("blank"))
        if blank:
            out.append(
                {
                    "column": name,
                    "observation": f"{blank} empty-string value(s) alongside "
                    f"{col.get('nulls', 0)} null(s) — two ways of saying 'absent'",
                }
            )
    return out
