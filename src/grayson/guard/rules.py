"""SQL statement guard.

Every statement an agent submits passes through validate_statement() before
execution; there is no unguarded path. Default-deny: only read-shaped
statements (SELECT/SHOW/DESCRIBE/EXPLAIN) survive, scoped and cost-capped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatch

import sqlglot
from sqlglot import exp

# Statement/expression node types that are never allowed anywhere in the tree.
# Built by name so the guard survives sqlglot additions/renames: a name that no
# longer exists is skipped, and exp.Command catches otherwise-unparsed verbs.
_FORBIDDEN_NAMES = [
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Create",
    "Drop",
    "Alter",
    "AlterTable",
    "AlterSession",
    "TruncateTable",
    "Truncate",
    "Grant",
    "Revoke",
    "Use",
    "Copy",
    "Call",
    "Set",
    "Pragma",
    "Transaction",
    "Commit",
    "Rollback",
    "Kill",
    "Undrop",
    "Attach",
    "Detach",
    "Export",
    "LoadData",
    "Install",
    "Comment",
    "Into",
    "Command",  # catch-all for constructs sqlglot could not parse — never allowed nested
]

FORBIDDEN_TYPES: tuple[type[exp.Expression], ...] = tuple(
    t
    for name in _FORBIDDEN_NAMES
    if isinstance(t := getattr(exp, name, None), type) and issubclass(t, exp.Expression)
)

_SET_OPERATION: type[exp.Expression] = getattr(exp, "SetOperation", exp.Union)
_TABLE_FROM_ROWS: type[exp.Expression] | None = getattr(exp, "TableFromRows", None)

ALWAYS_ALLOWED_SCHEMAS = {"INFORMATION_SCHEMA"}
ALWAYS_ALLOWED_CATALOGS = {"SNOWFLAKE"}

# Scalar/table functions denied outright. SYSTEM$* is denied by prefix because
# the family includes session/query-mutating side effects (ABORT_SESSION,
# CANCEL_QUERY, WAIT); QA never needs them. RESULT_SCAN reads arbitrary prior
# query output by id, bypassing scope — agents use grayson's own cache instead.
DENIED_FUNCTIONS = {"RESULT_SCAN", "GET_ABSOLUTE_PATH", "GET_PRESIGNED_URL", "GET_STAGE_LOCATION"}
DENIED_FUNCTION_PREFIXES = ("SYSTEM$",)

# Built-in table functions that are safe as row sources (no scope/exfil concern).
SAFE_TABLE_FUNCTIONS = {"GENERATOR", "FLATTEN", "SPLIT_TO_TABLE", "EXPLODE"}

_SHOW_RE = re.compile(
    r"^\s*SHOW\s+(TERSE\s+)?"
    r"(TABLES|VIEWS|COLUMNS|SCHEMAS|DATABASES|OBJECTS|FUNCTIONS|PROCEDURES|"
    r"STAGES|WAREHOUSES|PARAMETERS|PRIMARY\s+KEYS|IMPORTED\s+KEYS|UNIQUE\s+KEYS)\b",
    re.IGNORECASE,
)
_DESCRIBE_RE = re.compile(r'^\s*DESC(RIBE)?\s+(TABLE\s+|VIEW\s+)?[\w."$]+\s*$', re.IGNORECASE)
_EXPLAIN_RE = re.compile(r"^\s*EXPLAIN(\s+USING\s+\w+)?\s+", re.IGNORECASE)


@dataclass
class GuardContext:
    """Session-scoped inputs to validation."""

    scope_tables: set[str] = field(default_factory=set)  # upper-cased FQNs in scope
    allowed_globs: list[str] = field(default_factory=list)  # "DB.SCHEMA" fnmatch globs
    strict_scope: bool = False
    executed_count: int = 0  # statements already executed this session


@dataclass
class GuardVerdict:
    allowed: bool
    rule: str | None = None  # rule that rejected, if any
    reason: str | None = None
    suggestion: str | None = None
    warnings: list[str] = field(default_factory=list)
    executed_sql: str | None = None  # what will actually run (may differ: LIMIT injection)
    tables: list[str] = field(default_factory=list)
    injected_limit: int | None = None
    aggregate_only: bool = False

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "rule": self.rule,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "warnings": self.warnings,
            "tables": self.tables,
            "injected_limit": self.injected_limit,
            "aggregate_only": self.aggregate_only,
        }


def _reject(rule: str, reason: str, suggestion: str | None = None) -> GuardVerdict:
    return GuardVerdict(allowed=False, rule=rule, reason=reason, suggestion=suggestion)


def validate_statement(sql, settings, context: GuardContext | None = None) -> GuardVerdict:
    """Validate one agent-submitted statement. settings: config.GuardSettings."""
    context = context or GuardContext()
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        return _reject("empty", "empty statement")

    # -- parse & single-statement check (parser splits on real semicolons,
    #    not ones inside string literals or comments)
    try:
        statements = sqlglot.parse(sql, read="snowflake")
    except sqlglot.errors.ParseError as e:
        return _reject("parse_error", f"could not parse statement: {e}")
    # None and bare Semicolon entries are empty statements/trailing comments
    statements = [s for s in statements if s is not None and not isinstance(s, exp.Semicolon)]
    if len(statements) != 1:
        return _reject(
            "multi_statement",
            f"exactly one statement per call (got {len(statements)})",
            "submit statements one at a time",
        )
    tree = statements[0]

    # -- budget (checked before anything executes)
    if settings.budget_cap and context.executed_count >= settings.budget_cap:
        return _reject(
            "budget_exceeded",
            f"session query budget of {settings.budget_cap} reached",
            "ask the user to extend the budget (`grayson session budget --cap N` or the UI)",
        )

    # -- EXPLAIN: strip the prefix, validate the inner statement, run the original
    if _EXPLAIN_RE.match(sql):
        inner = _EXPLAIN_RE.sub("", sql, count=1)
        inner_settings = settings.model_copy(update={"auto_limit": 0})
        inner_verdict = validate_statement(inner, inner_settings, context)
        if not inner_verdict.allowed:
            return _reject(
                "explain_inner",
                f"EXPLAIN target rejected: {inner_verdict.reason}",
                inner_verdict.suggestion,
            )
        verdict = inner_verdict
        verdict.executed_sql = sql
        verdict.injected_limit = None
        return verdict

    # -- metadata commands that sqlglot may leave unparsed (exp.Command)
    if isinstance(tree, exp.Command):
        text = sql.upper()
        if text.startswith("SHOW"):
            if _SHOW_RE.match(sql):
                return GuardVerdict(allowed=True, executed_sql=sql)
            return _reject("show_form", "unsupported SHOW form")
        if text.startswith(("DESC", "DESCRIBE")):
            if _DESCRIBE_RE.match(sql):
                return GuardVerdict(allowed=True, executed_sql=sql)
            return _reject("describe_form", "unsupported DESCRIBE form")
        first = sql.split(None, 1)[0].upper()
        return _reject(
            "statement_type",
            f"statement type '{first}' is not allowed",
            "agents may only run SELECT, SHOW, DESCRIBE, and EXPLAIN; "
            "for new warehouse objects, propose a view for the user to execute",
        )

    # -- root statement type allowlist
    if isinstance(tree, exp.Describe | exp.Show):
        return GuardVerdict(allowed=True, executed_sql=sql)
    if not isinstance(tree, exp.Select | _SET_OPERATION):
        return _reject(
            "statement_type",
            f"statement type '{type(tree).__name__}' is not allowed",
            "agents may only run SELECT, SHOW, DESCRIBE, and EXPLAIN; "
            "for new warehouse objects, propose a view for the user to execute",
        )

    # -- forbidden constructs anywhere in the tree (default-deny in depth)
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_TYPES):
            return _reject(
                "forbidden_construct",
                f"'{type(node).__name__}' construct is not allowed inside a query",
                "read-only SELECT statements only; SELECT INTO, DML, and DDL are blocked",
            )

    # -- denied functions (side-effecting/scope-bypassing built-ins & UDTFs).
    #    Anonymous nodes carry the raw function name; typed exp.Func built-ins
    #    (COUNT, SUM, GENERATOR, ...) are analytical and always fine.
    for fn in tree.find_all(exp.Anonymous):
        fname = str(fn.this or "").upper()
        if fname in DENIED_FUNCTIONS or fname.startswith(DENIED_FUNCTION_PREFIXES):
            return _reject(
                "denied_function",
                f"function '{fname}' is not allowed",
                "this function can cause side effects or bypass scope; it is blocked "
                "for read-only QA. Use a plain SELECT over the target tables instead.",
            )

    # -- table extraction & scope check
    cte_names = {c.alias_or_name.upper() for c in tree.find_all(exp.CTE) if c.alias_or_name}
    tables: list[str] = []
    warnings: list[str] = []

    # UDTF row sources (TABLE(udtf(...))) are invisible to the table scope check
    # and could read/exfiltrate outside scope. Built-in table funcs are safe.
    for tfr in _table_function_sources(tree):
        anon = tfr.find(exp.Anonymous)
        if anon is None:
            continue  # typed built-in table function (GENERATOR, FLATTEN, ...)
        fname = str(anon.this or "").upper()
        if fname in SAFE_TABLE_FUNCTIONS:
            continue
        if context.strict_scope:
            return _reject(
                "table_function_scope",
                f"table function '{fname}' cannot be scope-checked (strict mode)",
                "table/UDTF functions can read outside the session scope; run a plain "
                "SELECT over registered tables, or ask the user to relax strict scope",
            )
        warnings.append(
            f"table function '{fname}' is not scope-checked — verify it reads only in-scope data"
        )

    for t in tree.find_all(exp.Table):
        name = (t.name or "").upper()
        if not name:
            continue  # function-backed source, handled above
        catalog = (t.catalog or "").upper()
        schema = (t.db or "").upper()
        if not catalog and not schema and name in cte_names:
            continue
        fq = ".".join(p for p in (catalog, schema, name) if p)
        tables.append(fq)
        if schema in ALWAYS_ALLOWED_SCHEMAS or catalog in ALWAYS_ALLOWED_CATALOGS:
            continue
        if fq in context.scope_tables:
            continue
        schema_key = f"{catalog}.{schema}" if catalog else schema
        if schema and any(fnmatch(schema_key, g.upper()) for g in context.allowed_globs):
            continue
        if context.strict_scope:
            # Unqualified names cannot be verified against scope; in strict mode
            # that ambiguity is itself a block (Snowflake resolves them against
            # the connection's current namespace, i.e. potentially anything).
            detail = (
                f"table '{fq}' is outside the session scope (strict mode)"
                if schema
                else f"unqualified table '{fq}' cannot be scope-verified (strict mode)"
            )
            return _reject(
                "out_of_scope",
                detail,
                "register the table at session start or ask the user to widen scope",
            )
        if not schema:
            warnings.append(f"unqualified table '{fq}' cannot be scope-checked")
        else:
            warnings.append(f"table '{fq}' is outside the session scope")

    # -- budget warn threshold
    if settings.budget_warn and context.executed_count + 1 >= settings.budget_warn:
        warnings.append(
            f"query {context.executed_count + 1} of soft budget {settings.budget_warn} — "
            "consider consolidating or asking the user to raise the budget"
        )

    # -- auto-LIMIT on unbounded raw-row output
    aggregate_only = _is_aggregate_only(tree)
    injected: int | None = None
    executed_tree = tree
    if settings.auto_limit and not aggregate_only:
        existing = _existing_limit(tree)
        if existing is None or existing > settings.auto_limit:
            executed_tree = tree.limit(settings.auto_limit)
            injected = settings.auto_limit

    return GuardVerdict(
        allowed=True,
        warnings=warnings,
        executed_sql=executed_tree.sql(dialect="snowflake"),
        tables=sorted(set(tables)),
        injected_limit=injected,
        aggregate_only=aggregate_only,
    )


def _table_function_sources(tree: exp.Expression) -> list[exp.Expression]:
    """Row sources backed by a function call: TABLE(fn(...)) and bare fn('...').

    These do not appear as normal exp.Table references, so the scope loop can
    miss them. Returns the wrapper nodes for inspection.
    """
    sources: list[exp.Expression] = []
    if _TABLE_FROM_ROWS is not None:
        sources.extend(tree.find_all(_TABLE_FROM_ROWS))
    # bare form: exp.Table with empty name but a function child (e.g. RESULT_SCAN)
    for t in tree.find_all(exp.Table):
        if not (t.name or "") and t.find(exp.Anonymous) is not None:
            sources.append(t)
    return sources


def _existing_limit(tree: exp.Expression) -> int | None:
    limit = tree.args.get("limit")
    if limit is None:
        return None
    try:
        return int(limit.expression.this)
    except (AttributeError, TypeError, ValueError):
        return 0  # unparseable limit: treat as bounded, do not override


def _is_aggregate_only(tree: exp.Expression) -> bool:
    """True when output is aggregate-shaped: GROUP BY present, or every
    projection contains an aggregate outside a window frame."""
    if not isinstance(tree, exp.Select):
        return False  # set operations treated as raw-row output
    if tree.args.get("group") is not None:
        return True
    projections = tree.expressions
    if not projections:
        return False
    return all(_has_non_window_agg(p) for p in projections)


def _has_non_window_agg(node: exp.Expression) -> bool:
    for agg in node.find_all(exp.AggFunc):
        parent = agg.parent
        inside_window = False
        while parent is not None and parent is not node:
            if isinstance(parent, exp.Window):
                inside_window = True
                break
            parent = parent.parent
        if not inside_window:
            return True
    return False
