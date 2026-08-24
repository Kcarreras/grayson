"""Tiny dependency-free SQL syntax highlighter for the console.

One regex pass over the statement; every fragment is HTML-escaped before any
markup is added, so hostile SQL cannot smuggle tags into the page. Colors ride
on the console's theme tokens (see the `.sql-*` rules in base.html).
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_KEYWORDS = {
    "ALL",
    "AND",
    "ANY",
    "AS",
    "ASC",
    "AVG",
    "BETWEEN",
    "BY",
    "CASE",
    "CAST",
    "COALESCE",
    "COUNT",
    "CROSS",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "DESC",
    "DESCRIBE",
    "DISTINCT",
    "ELSE",
    "END",
    "EXISTS",
    "EXPLAIN",
    "FALSE",
    "FROM",
    "FULL",
    "GROUP",
    "HAVING",
    "ILIKE",
    "IN",
    "INNER",
    "INTERVAL",
    "IS",
    "JOIN",
    "LEFT",
    "LIKE",
    "LIMIT",
    "MAX",
    "MIN",
    "NOT",
    "NULL",
    "NULLIF",
    "OFFSET",
    "ON",
    "OR",
    "ORDER",
    "OUTER",
    "OVER",
    "PARTITION",
    "QUALIFY",
    "RANGE",
    "RIGHT",
    "ROWS",
    "SELECT",
    "SHOW",
    "SOME",
    "SUM",
    "THEN",
    "TRUE",
    "UNION",
    "USING",
    "VALUES",
    "WHEN",
    "WHERE",
    "WITH",
}

_TOKEN = re.compile(
    r"""
      (?P<comment>--[^\n]*|/\*.*?\*/)
    | (?P<string>'(?:[^']|'')*')
    | (?P<number>\b\d+(?:\.\d+)?\b)
    | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
    """,
    re.VERBOSE | re.DOTALL,
)


def highlight_sql(sql: str) -> Markup:
    out: list[str] = []
    pos = 0
    for m in _TOKEN.finditer(sql):
        out.append(str(escape(sql[pos : m.start()])))
        text = str(escape(m.group(0)))
        kind = m.lastgroup
        if kind == "comment":
            out.append(f'<span class="sql-c">{text}</span>')
        elif kind == "string":
            out.append(f'<span class="sql-s">{text}</span>')
        elif kind == "number":
            out.append(f'<span class="sql-n">{text}</span>')
        elif m.group(0).upper() in _KEYWORDS:
            out.append(f'<span class="sql-k">{text}</span>')
        else:
            out.append(text)
        pos = m.end()
    out.append(str(escape(sql[pos:])))
    return Markup("".join(out))
