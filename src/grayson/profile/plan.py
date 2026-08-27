"""Planning the profile battery: a handful of wide queries, not forty narrow ones.

The naive shape of a column profile is one query per column per statistic, which
on a 40-column table is 160 warehouse round-trips and a blown query budget. But
aggregates compose: `COUNT`, `COUNT(DISTINCT)`, `MIN`, `MAX` over every column
fit in a single SELECT, and per-value frequencies for every low-cardinality
column fit in one UNION ALL. A normal table profiles in three or four statements.

Everything emitted here is plain aggregate SQL, so it survives the guard
unchanged and transpiles to the sandbox's SQLite as readily as it runs on
Snowflake. Richer statistics that portable SQL cannot express — quantiles,
correlation — are computed locally over a cached sample instead (see stats.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: identifier parts we are willing to interpolate into generated SQL
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

#: how many columns' aggregates go in one statement. Wide is the point, but a
#: 500-column table would otherwise generate a single statement no warehouse
#: enjoys parsing.
COLUMNS_PER_BATCH = 40

#: a column with more distinct values than this gets no frequency breakdown —
#: it is an identifier or a free-text field, and its top values say nothing
MAX_DISTINCT_FOR_FREQUENCIES = 50

#: rows pulled for local statistics (quantiles, correlation)
DEFAULT_SAMPLE_ROWS = 5000

NUMERIC_TYPES = (
    "NUMBER",
    "DECIMAL",
    "NUMERIC",
    "INT",
    "INTEGER",
    "BIGINT",
    "SMALLINT",
    "TINYINT",
    "BYTEINT",
    "FLOAT",
    "DOUBLE",
    "REAL",
)
TEMPORAL_TYPES = ("DATE", "TIME", "TIMESTAMP", "DATETIME")
TEXT_TYPES = ("VARCHAR", "CHAR", "STRING", "TEXT")
BOOLEAN_TYPES = ("BOOLEAN", "BOOL")
#: semi-structured and binary: countable, but MIN/MAX over them means nothing
OPAQUE_TYPES = ("VARIANT", "OBJECT", "ARRAY", "BINARY", "GEOGRAPHY", "GEOMETRY")


@dataclass(frozen=True)
class Column:
    name: str
    raw_type: str

    @property
    def kind(self) -> str:
        t = self.raw_type.upper()
        for family, names in (
            ("numeric", NUMERIC_TYPES),
            ("temporal", TEMPORAL_TYPES),
            ("boolean", BOOLEAN_TYPES),
            ("text", TEXT_TYPES),
            ("opaque", OPAQUE_TYPES),
        ):
            if any(t.startswith(n) for n in names):
                return family
        return "other"

    @property
    def ordered(self) -> bool:
        """Whether MIN/MAX over this column carries meaning."""
        return self.kind in ("numeric", "temporal")


class ProfilePlanError(ValueError):
    pass


def quote(name: str) -> str:
    """Double-quote an identifier, refusing anything that isn't a plain one.

    Profile SQL is generated rather than agent-supplied, but it still embeds
    names read from the warehouse — so they are validated, not trusted.
    """
    if not _IDENT_RE.match(name):
        raise ProfilePlanError(
            f"cannot profile column {name!r}: only plain identifiers are interpolated "
            "into generated SQL"
        )
    return f'"{name}"'


def qualify(table: str) -> str:
    parts = table.split(".")
    if not 1 <= len(parts) <= 3 or not all(_IDENT_RE.match(p) for p in parts):
        raise ProfilePlanError(f"not a usable table name: {table!r}")
    return ".".join(quote(p) for p in parts)


def parse_describe(rows: list[dict]) -> list[Column]:
    """Turn DESCRIBE TABLE output into columns. Snowflake and the sandbox both
    return `name`/`type`; casing varies by driver."""
    out = []
    for row in rows:
        upper = {str(k).upper(): v for k, v in row.items()}
        name = str(upper.get("NAME") or upper.get("COLUMN_NAME") or "").strip()
        if not name:
            continue
        out.append(Column(name=name, raw_type=str(upper.get("TYPE") or "").strip()))
    return out


def batches(columns: list[Column], size: int = COLUMNS_PER_BATCH) -> list[list[Column]]:
    return [columns[i : i + size] for i in range(0, len(columns), size)] or [[]]


def aggregate_sql(table: str, columns: list[Column]) -> tuple[str, dict[str, tuple[str, str]]]:
    """One wide SELECT covering every column's core statistics.

    Returns the SQL and a map from output alias to (column name, statistic), so
    the flat result row can be folded back into per-column facts. Aliases are
    positional (`C0_NULLS`) rather than derived from column names: names can
    collide once quoted, exceed identifier limits, or differ in case across
    drivers, and none of that is worth risking for readability of a machine-read
    result set.
    """
    selects = ["COUNT(*) AS ROW_TOTAL"]
    alias_map: dict[str, tuple[str, str]] = {}

    def add(alias: str, expr: str, column: str, stat: str) -> None:
        selects.append(f"{expr} AS {alias}")
        alias_map[alias] = (column, stat)

    for i, col in enumerate(columns):
        q = quote(col.name)
        add(f"C{i}_NONNULL", f"COUNT({q})", col.name, "non_null")
        if col.kind != "opaque":
            add(f"C{i}_DISTINCT", f"COUNT(DISTINCT {q})", col.name, "distinct")
        if col.ordered:
            add(f"C{i}_MIN", f"CAST(MIN({q}) AS VARCHAR)", col.name, "min")
            add(f"C{i}_MAX", f"CAST(MAX({q}) AS VARCHAR)", col.name, "max")
        if col.kind == "numeric":
            add(f"C{i}_AVG", f"AVG({q})", col.name, "avg")
            # a column whose values never change is dead weight downstream, and
            # a constant that used to vary is usually an upstream break
            add(f"C{i}_ZEROES", f"SUM(CASE WHEN {q} = 0 THEN 1 ELSE 0 END)", col.name, "zeroes")
        if col.kind == "text":
            # value range as well as length range: dates, codes and versions are
            # routinely stored as text, and a lexicographic max is exactly how a
            # 2031 birthdate in a VARCHAR column announces itself
            add(f"C{i}_MIN", f"CAST(MIN({q}) AS VARCHAR)", col.name, "min")
            add(f"C{i}_MAX", f"CAST(MAX({q}) AS VARCHAR)", col.name, "max")
            add(f"C{i}_MINLEN", f"MIN(LENGTH({q}))", col.name, "min_length")
            add(f"C{i}_MAXLEN", f"MAX(LENGTH({q}))", col.name, "max_length")
            # empty string is not NULL, and the difference routinely hides a bug
            add(f"C{i}_BLANK", f"SUM(CASE WHEN {q} = '' THEN 1 ELSE 0 END)", col.name, "blank")
    sql = "SELECT " + ", ".join(selects) + f" FROM {qualify(table)}"
    return sql, alias_map


def frequency_sql(table: str, columns: list[Column]) -> str | None:
    """One UNION ALL giving every low-cardinality column's value breakdown.

    Bounded by construction: only columns already shown to have few distinct
    values take part, so the result is a few hundred rows however wide the table.
    """
    parts = []
    for col in columns:
        q = quote(col.name)
        parts.append(
            f"SELECT '{col.name}' AS COLUMN_NAME, CAST({q} AS VARCHAR) AS VALUE, "
            f"COUNT(*) AS FREQUENCY FROM {qualify(table)} GROUP BY {q}"
        )
    if not parts:
        return None
    return "\nUNION ALL\n".join(parts)


def sample_sql(table: str, columns: list[Column], rows: int = DEFAULT_SAMPLE_ROWS) -> str:
    """A sample to compute locally over. Opaque columns are dropped — they bloat
    the artifact and nothing downstream can do arithmetic on them."""
    usable = [c for c in columns if c.kind != "opaque"] or columns
    cols = ", ".join(quote(c.name) for c in usable)
    return f"SELECT {cols} FROM {qualify(table)} LIMIT {int(rows)}"
