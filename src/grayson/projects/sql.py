"""Compile candidate graphs and generate full-relation verification probes.

Raw query nodes have one input and no nested SELECTs or joins. Every join is
therefore built from an approved contract; an agent cannot hide a second join
inside a CTE, a scalar subquery, comma syntax, or a UNION branch.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from grayson.checks.regression import Expectation
from grayson.projects.models import Candidate, Contract
from grayson.util import is_object_name


def deployment_target(value: str) -> str:
    """Use only names compatible with the scope registry's uppercase semantics."""
    if not is_object_name(value) or '"' in value:
        raise ValueError("deployment target must use unquoted DB.SCHEMA.OBJECT identifiers")
    return value.upper()


def ident(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value):
        raise ValueError(f"unsupported column identifier {value!r}; use a simple alias")
    return '"' + value.upper() + '"'


def select(sql: str) -> exp.Select:
    statements = sqlglot.parse(sql, read="snowflake")
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("one read-only SELECT is required")
    tree = statements[0]
    if any(tree.find_all(exp.Limit, exp.Offset, exp.TableSample, exp.Into)):
        raise ValueError("LIMIT, OFFSET, sampling and SELECT INTO cannot establish full coverage")
    return tree


def sources(tree: exp.Expression) -> set[str]:
    out = set()
    for scope in traverse_scope(tree):
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                if not isinstance(source.this, exp.Identifier):
                    raise ValueError("table functions are not project input relations")
                if len(source.parts) > 1 and any(p.args.get("quoted") for p in source.parts):
                    raise ValueError(
                        "project source tables must use unquoted DB.SCHEMA.OBJECT identifiers"
                    )
                out.add(".".join(p.name for p in source.parts).upper())
    return out


def baseline(sql: str, scope: set[str]) -> str:
    tree = select(sql)
    refs = sources(tree)
    if not refs or not refs <= scope:
        raise ValueError("baseline must read only explicit, fully qualified scoped tables")
    return tree.sql(dialect="snowflake")


def compile_candidate(candidate: Candidate, contract: Contract) -> dict:
    joins = {j.id: j for j in contract.joins}
    ctes, seen, dependencies, risks = [], set(), {}, []
    for node in candidate.nodes:
        if node.kind == "query":
            tree = select(node.sql)
            if len(list(tree.find_all(exp.Select))) != 1 or tree.find(exp.Join):
                raise ValueError(
                    f"{node.id}: use explicit join nodes; nested SELECTs are unsupported"
                )
            refs = sources(tree)
            if len(refs) != 1 or not refs <= set(contract.scope) | {n.upper() for n in seen}:
                raise ValueError(f"{node.id}: query must read one scoped table or earlier node")
            dependencies[node.id] = {n for n in seen if n.upper() in refs}
            for clause in ("where", "distinct", "group", "qualify", "having"):
                if tree.args.get(clause):
                    risks.append(
                        {
                            "node": node.id,
                            "kind": clause,
                            "review": "Check population, grain, nulls and measure preservation",
                        }
                    )
            sql = tree.sql(dialect="snowflake")
        else:
            j = joins.get(node.id)
            if j is None or not {j.left, j.right} <= seen:
                raise ValueError(f"{node.id}: requires an approved join with earlier input nodes")
            columns = []
            if len({name.upper() for name in node.columns}) != len(node.columns):
                raise ValueError("join output column names must be unique ignoring case")
            for name, value in node.columns.items():
                projection = select("SELECT " + value)
                if (
                    len(projection.expressions) != 1
                    or len(list(projection.find_all(exp.Select))) != 1
                    or projection.args.get("from_")
                    or projection.find(exp.Star)
                    or projection.find(exp.AggFunc)
                    or projection.find(exp.Window)
                ):
                    raise ValueError("join columns must be scalar projections over l and r")
                if any(c.table.lower() not in {"l", "r"} for c in projection.find_all(exp.Column)):
                    raise ValueError("qualify join projection columns with l. or r.")
                columns.append(
                    f"{projection.expressions[0].sql(dialect='snowflake')} AS {ident(name)}"
                )
            on = " AND ".join(
                f"l.{ident(left_key)} = r.{ident(right_key)}"
                for left_key, right_key in zip(j.left_keys, j.right_keys, strict=True)
            )
            sql = (
                f"SELECT {', '.join(columns)} FROM {ident(j.left)} l "
                f"{j.how.upper()} JOIN {ident(j.right)} r ON {on}"
            )
            dependencies[node.id] = {j.left, j.right}
        seen.add(node.id)
        expression_risks = {
            exp.Case: (
                "business_logic",
                "Check each CASE branch against an approved semantic rule",
            ),
            exp.Coalesce: ("null_replacement", "Verify that replacing unknown values is intended"),
            exp.Window: (
                "window",
                "Check partition grain, ordering ties and point-in-time semantics",
            ),
            exp.Cast: ("type_conversion", "Check precision, invalid values and null conversions"),
        }
        for cls, (kind, review) in expression_risks.items():
            if select(sql).find(cls):
                risks.append({"node": node.id, "kind": kind, "review": review})
        ctes.append(f"{ident(node.id)} AS ({sql})")
    if set(joins) != {n.id for n in candidate.nodes if n.kind == "join"}:
        raise ValueError("candidate must implement exactly the approved joins")
    reachable = set()

    def visit(node):
        if node not in reachable:
            reachable.add(node)
            for parent in dependencies[node]:
                visit(parent)

    visit(candidate.output)
    if seen != reachable:
        raise ValueError("all candidate nodes must contribute to the output")
    ctes.append(f'"CANDIDATE" AS (SELECT * FROM {ident(candidate.output)})')
    prefix = "WITH " + ",\n".join(ctes)
    return {"prefix": prefix, "sql": prefix + '\nSELECT * FROM "CANDIDATE"', "risks": risks}


def deployment_equivalence_queries(candidate: Candidate, contract: Contract) -> list[dict]:
    """Compare every output value, even when semantic checks target intermediates.

    The accompanying output grain checks reject duplicates; these full-row set
    comparisons establish coverage in both directions without hashing values.
    Incompatible schemas or unsupported comparisons remain unproven SQL errors.
    """
    prefix = compile_candidate(candidate, contract)["prefix"]
    target = deployment_target(contract.deployment_target)
    return [
        {
            "id": "deployment." + suffix,
            "name": "Deployed output: " + label,
            "sql": prefix + "\nSELECT COUNT(*) AS N FROM ("
            f"SELECT * FROM {left} EXCEPT SELECT * FROM {right}) GRAYSON_DIFFERENCE",
            "expectation": Expectation(kind="scalar", column="N", value=0).model_dump(mode="json"),
            "repair": "Compare the deployed schema and every output value with the approved SQL.",
        }
        for suffix, label, left, right in (
            ("missing_rows", "missing expected rows", '"CANDIDATE"', target),
            ("unexpected_rows", "unexpected rows", target, '"CANDIDATE"'),
        )
    ]


def verification_queries(candidate: Candidate, contract: Contract) -> list[dict]:
    compiled = compile_candidate(candidate, contract)
    prefix = compiled["prefix"]
    relations = {n.id for n in candidate.nodes} | {"candidate"}
    checks = []

    def add(key, name, sql, *, value=0, operator="eq", extra="", repair=""):
        checks.append(
            {
                "id": key,
                "name": name,
                "sql": prefix + extra + "\n" + sql,
                "expectation": Expectation(
                    kind="scalar", column="N", operator=operator, value=value
                ).model_dump(mode="json"),
                "repair": repair,
            }
        )

    add(
        "output.nonempty",
        "Output has the agreed minimum rows",
        'SELECT COUNT(*) AS N FROM "CANDIDATE"',
        value=contract.minimum_rows,
        operator="gte",
        repair="Inspect source availability and filters; do not lower the minimum to pass.",
    )
    for join in contract.joins:
        left, right = ident(join.left), ident(join.right)
        on = " AND ".join(
            f"l.{ident(a)} = r.{ident(b)}"
            for a, b in zip(join.left_keys, join.right_keys, strict=True)
        )
        repair = (
            f"Inspect {join.left}/{join.right} key multiplicity and business grain. "
            "Correct the key or source logic; DISTINCT or arbitrary ROW_NUMBER is not a "
            "semantic resolution. A changed relationship requires a contract review."
        )
        for side, relation, keys in (
            ("left", left, join.left_keys),
            ("right", right, join.right_keys),
        ):
            nulls = " OR ".join(f"{ident(k)} IS NULL" for k in keys)
            add(
                f"join.{join.id}.{side}_null_keys",
                f"{join.id}: {side} keys are not null",
                f"SELECT COUNT(*) AS N FROM {relation} WHERE {nulls}",
                repair=repair,
            )
            cap = join.max_matches if side == "right" else 1
            if side == "left" and join.cardinality == "many_to_one":
                continue
            keys_sql = ", ".join(ident(k) for k in keys)
            add(
                f"join.{join.id}.{side}_multiplicity",
                f"{join.id}: {side} multiplicity",
                f"SELECT COUNT(*) AS N FROM (SELECT {keys_sql} FROM {relation} "
                f"GROUP BY {keys_sql} HAVING COUNT(*) > {cap})",
                repair=repair,
            )
        add(
            f"join.{join.id}.unmatched_left",
            f"{join.id}: unmatched left rows",
            f"SELECT COUNT(*) AS N FROM {left} l WHERE NOT EXISTS "
            f"(SELECT 1 FROM {right} r WHERE {on})",
            value=join.max_unmatched_left,
            operator="lte",
            repair="Inspect missing keys and null handling; do not silently discard rows.",
        )
        if join.max_unmatched_right is not None:
            add(
                f"join.{join.id}.unmatched_right",
                f"{join.id}: unmatched right rows",
                f"SELECT COUNT(*) AS N FROM {right} r WHERE NOT EXISTS "
                f"(SELECT 1 FROM {left} l WHERE {on})",
                value=join.max_unmatched_right,
                operator="lte",
                repair=repair,
            )
    for check in contract.checks:
        if check.relation not in relations:
            raise ValueError(f"{check.id}: missing candidate relation {check.relation}")
        relation = ident(check.relation)
        repair = check.rationale + ". Diagnose exclusions, aggregation, keys and null semantics."
        if check.kind == "grain":
            keys = ", ".join(ident(k) for k in check.keys)
            nulls = " OR ".join(f"{ident(k)} IS NULL" for k in check.keys)
            add(
                check.id + ".unique",
                check.name,
                f"SELECT COUNT(*) AS N FROM (SELECT {keys} FROM {relation} "
                f"GROUP BY {keys} HAVING COUNT(*) > 1)",
                repair=repair,
            )
            add(
                check.id + ".nonnull",
                check.name + ": non-null keys",
                f"SELECT COUNT(*) AS N FROM {relation} WHERE {nulls}",
                repair=repair,
            )
        elif check.kind == "population":
            base = baseline(check.baseline_sql, set(contract.scope))
            extra = f', "GRAYSON_BASE" AS ({base})'
            equality = " AND ".join(
                f"a.{ident(k)} IS NOT DISTINCT FROM b.{ident(k)}" for k in check.keys
            )
            for suffix, a, b, cap in (
                ("missing", '"GRAYSON_BASE"', relation, check.max_missing),
                ("added", relation, '"GRAYSON_BASE"', check.max_added),
            ):
                add(
                    check.id + "." + suffix,
                    check.name + ": " + suffix,
                    f"SELECT COUNT(*) AS N FROM {a} a WHERE NOT EXISTS "
                    f"(SELECT 1 FROM {b} b WHERE {equality})",
                    value=cap,
                    operator="lte",
                    extra=extra,
                    repair=repair,
                )
        elif check.kind == "values":
            base = baseline(check.baseline_sql, set(contract.scope))
            extra = f', "GRAYSON_BASE" AS ({base})'
            keys = ", ".join(ident(k) for k in check.keys)
            add(
                check.id + ".baseline_grain",
                check.name + ": baseline grain",
                f'SELECT COUNT(*) AS N FROM (SELECT {keys} FROM "GRAYSON_BASE" '
                f"GROUP BY {keys} HAVING COUNT(*) > 1)",
                extra=extra,
                repair="Resolve the expected-value baseline grain before interpreting this test.",
            )
            equality = " AND ".join(
                f"a.{ident(k)} IS NOT DISTINCT FROM b.{ident(k)}" for k in check.keys
            )
            add(
                check.id,
                check.name,
                f"SELECT COUNT(*) AS N FROM {relation} a WHERE NOT EXISTS "
                f'(SELECT 1 FROM "GRAYSON_BASE" b WHERE {equality} AND '
                f"a.{ident(check.column)} IS NOT DISTINCT FROM b.{ident(check.column)})",
                extra=extra,
                repair=repair,
            )
            add(
                check.id + ".missing",
                check.name + ": missing expected keys",
                f'SELECT COUNT(*) AS N FROM "GRAYSON_BASE" b WHERE NOT EXISTS '
                f"(SELECT 1 FROM {relation} a WHERE {equality})",
                extra=extra,
                repair=repair,
            )
        elif check.kind == "measure":
            base = baseline(check.baseline_sql, set(contract.scope))
            group = ", ".join(ident(k) for k in check.group_by)
            col = ident(check.column)
            projection = (group + ", " if group else "") + (
                f"SUM({col}) AS GRAYSON_V, COUNT({col}) AS GRAYSON_NON_NULL, "
                f"COUNT(*) - COUNT({col}) AS GRAYSON_NULL, 1 AS GRAYSON_PRESENT"
            )
            grouping = " GROUP BY " + group if group else ""
            extra = (
                f', "GRAYSON_BASE" AS ({base}), "GRAYSON_EXPECTED" AS '
                f'(SELECT {projection} FROM "GRAYSON_BASE"{grouping}), '
                f'"GRAYSON_ACTUAL" AS (SELECT {projection} FROM {relation}{grouping})'
            )
            equality = (
                " AND ".join(
                    f"a.{ident(k)} IS NOT DISTINCT FROM b.{ident(k)}" for k in check.group_by
                )
                or "TRUE"
            )
            # Compare row counts as well as values. Collapsed rows and null -> zero
            # conversions must not pass merely because the totals happen to agree.
            mismatch = (
                "a.GRAYSON_PRESENT IS NULL OR b.GRAYSON_PRESENT IS NULL OR "
                "a.GRAYSON_NON_NULL <> b.GRAYSON_NON_NULL OR "
                "a.GRAYSON_NULL <> b.GRAYSON_NULL OR "
                "(a.GRAYSON_V IS NULL) <> (b.GRAYSON_V IS NULL) OR "
                "ABS(a.GRAYSON_V - b.GRAYSON_V) > "
                f"({check.absolute_tolerance} + ABS(a.GRAYSON_V) * "
                f"{check.relative_percent} / 100)"
            )
            add(
                check.id,
                check.name,
                'SELECT COUNT(*) AS N FROM "GRAYSON_EXPECTED" a FULL OUTER JOIN '
                f'"GRAYSON_ACTUAL" b ON {equality} WHERE {mismatch}',
                extra=extra,
                repair=repair,
            )
        else:
            query = check.sql.replace("{{relation}}", relation)
            tree = select(query)
            if check.relation.upper() not in sources(tree):
                raise ValueError(f"{check.id}: assertion must actually read its candidate relation")
            if not sources(tree) <= set(contract.scope) | {check.relation.upper()}:
                raise ValueError(f"{check.id}: assertion references unapproved relations")
            checks.append(
                {
                    "id": check.id,
                    "name": check.name,
                    "sql": prefix + "\n" + query,
                    "expectation": check.expectation.model_dump(mode="json"),
                    "repair": repair,
                }
            )
    if len({q["id"] for q in checks}) != len(checks):
        raise ValueError("generated verification IDs collide")
    return checks
