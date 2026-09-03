"""Relationship entries: one canonical shape, and the shorthands it absorbs.

A relationship on a table doc says "this table joins to that one, on these
columns, with this cardinality". Agents record it through `knowledge set`, and
in practice they write the join key in whatever shape the moment suggests:
``ORDER_ID``, ``PROMO_CODE = CODE``, ``ORDERS.CUSTOMER_ID = CUSTOMERS.ID``, a
list, a from/to mapping. The schema map used to print that text verbatim, so a
reader could not tell which column belonged to which table, and two sides
describing one join in two spellings drew as two edges.

This module is the single place that understands the shapes. Everything
downstream (store, graph, templates, lint) sees the canonical form:

    {"to": "DB.SCHEMA.TABLE",          # fully qualified, upper-cased
     "on": "PROMO_CODE = CODE",        # declaring column = target column;
                                       #   "ORDER_ID" when both share the name;
                                       #   ", " between the parts of a composite key
     "keys": [{"from": "PROMO_CODE", "to": "CODE"}],   # derived from `on`, never written
     "cardinality": "many-to-one",     # declaring side first; one of CARDINALITIES
     "note": "..."}                    # free text, optional

`on` stays a string on disk: the doc remains readable with zero grayson. A join
that is not a column equality (a fuzzy match, a date-range join) stays as the
text it was written in, with no `keys`, and the map draws it as free text
rather than pretending it parsed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: canonical cardinalities, declaring side first: "many-to-one" from ORDERS to
#: CUSTOMERS means many orders share one customer.
CARDINALITIES = ("one-to-one", "one-to-many", "many-to-one", "many-to-many")

#: keys an entry may use for its target table; the first is canonical
_TARGET_KEYS = ("to", "table", "target", "related_table", "references")
#: keys an entry may use for its join key; the first is canonical
_JOIN_KEYS = ("on", "join", "join_key", "join_keys", "key", "keys", "columns", "using", "join_on")
#: keys an entry may use for its cardinality; the first is canonical
_CARDINALITY_KEYS = ("cardinality", "relation", "relationship_type", "kind")
#: a column pair given as a mapping: which key names the declaring table's
#: column and which the target's
_PAIR_FROM_KEYS = (
    "from",
    "left",
    "source",
    "local",
    "this",
    "column",
    "from_column",
    "source_column",
)
_PAIR_TO_KEYS = ("to", "right", "target", "foreign", "other", "to_column", "target_column", "ref")

#: derived fields that live on the in-memory entry but never on disk
DERIVED_KEYS = ("keys",)

_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_COLUMN_REF = re.compile(rf"^{_IDENT}(?:\.{_IDENT}){{0,3}}$")
_TERM_SPLIT = re.compile(r"\s+(?:AND|&&)\s+|\s*[,;]\s*|\s+&\s+", re.IGNORECASE)
_SIDE_SPLIT = re.compile(r"\s*(?:<->|↔|->|→|=>|==|=)\s*")
_WS = re.compile(r"\s+")
_CARD = re.compile(
    r"^(one|1|many|n|m|\*)\s*(?:-?\s*to\s*-?|:|->|→|/|-)\s*(one|1|many|n|m|\*)$",
    re.IGNORECASE,
)
_MANY = {"many", "n", "m", "*"}


def _leaf(fqn: str) -> str:
    return fqn.split(".")[-1]


def _clean_ident(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].strip()
    return text.upper()


def _split_ref(ref: str) -> tuple[str | None, str]:
    """'ORDERS.CUSTOMER_ID' -> ('ORDERS', 'CUSTOMER_ID'); 'CUSTOMER_ID' -> (None, ...)."""
    parts = re.findall(rf"{_IDENT}", ref)
    if not parts:
        return None, _clean_ident(ref)
    col = _clean_ident(parts[-1])
    table = _clean_ident(parts[-2]) if len(parts) > 1 else None
    return table, col


def normalize_cardinality(value: object) -> str:
    """Canonical cardinality, or the original text (stripped) when it is not one
    of the shapes we know. Accepts '1:N', 'N:1', 'one_to_many', 'many to one',
    '1 -> *', 'M:N' and the canonical forms themselves."""
    text = _WS.sub(" ", str(value or "")).strip()
    if not text:
        return ""
    m = _CARD.match(text.replace("_", "-"))
    if not m:
        return text
    a, b = (("many" if t.lower() in _MANY else "one") for t in m.groups())
    return f"{a}-to-{b}"


def flip_cardinality(card: str) -> str:
    """The same relationship seen from the other table."""
    if card == "one-to-many":
        return "many-to-one"
    if card == "many-to-one":
        return "one-to-many"
    return card


def cardinality_ends(card: str) -> tuple[str, str]:
    """('one'|'many'|'', same) for the declaring and target ends of an edge."""
    if card not in CARDINALITIES:
        return "", ""
    a, _, b = card.partition("-to-")
    return a, b


def _pair(from_col: str, to_col: str) -> dict[str, str]:
    return {"from": from_col, "to": to_col}


def _pair_from_mapping(value: Mapping[str, Any]) -> dict[str, str] | None:
    from_col = next((value[k] for k in _PAIR_FROM_KEYS if value.get(k)), None)
    to_col = next((value[k] for k in _PAIR_TO_KEYS if value.get(k)), None)
    if from_col is None and to_col is None:
        return None
    if from_col is None or to_col is None:
        col = from_col if from_col is not None else to_col
        return _pair(_clean_ident(str(col)), _clean_ident(str(col)))
    return _pair(_clean_ident(str(from_col)), _clean_ident(str(to_col)))


def _parse_term(term: str, source: str, target: str) -> dict[str, str] | None:
    """One equality: 'A', 'A = B', 'T1.A = T2.B'. A qualifier decides which side a
    column belongs to when it names the declaring or target table; otherwise
    the left column is the declaring table's."""
    sides = [s for s in _SIDE_SPLIT.split(term.strip()) if s.strip()]
    if not sides or len(sides) > 2 or not all(_COLUMN_REF.match(s.strip()) for s in sides):
        return None
    if len(sides) == 1:
        _, col = _split_ref(sides[0])
        return _pair(col, col)
    (t_left, c_left), (t_right, c_right) = _split_ref(sides[0]), _split_ref(sides[1])
    src, tgt = _leaf(source).upper(), _leaf(target).upper()
    if (t_left == tgt and t_left != src) or (t_right == src and t_right != tgt):
        c_left, c_right = c_right, c_left  # written from the target's point of view
    return _pair(c_left, c_right)


def parse_join(on: object, source: str, target: str) -> list[dict[str, str]] | None:
    """Column pairs for a join key in any of the accepted shapes, or None when it
    is not a column equality we can read (the entry then keeps its text)."""
    if on is None or on == "" or on == []:
        return []
    if isinstance(on, str):
        terms = [t for t in _TERM_SPLIT.split(on.strip()) if t.strip()]
        pairs = [_parse_term(t, source, target) for t in terms]
        if not pairs or any(p is None for p in pairs):
            return None
        return [p for p in pairs if p is not None]
    if isinstance(on, Mapping):
        single = _pair_from_mapping(on)
        if single is not None:
            return [single]
        if on and all(isinstance(k, str) and isinstance(v, str) for k, v in on.items()):
            return [_pair(_clean_ident(k), _clean_ident(v)) for k, v in on.items()]
        return None
    if isinstance(on, (list, tuple)):
        pairs: list[dict[str, str]] = []
        for item in on:
            if isinstance(item, str):
                sub = parse_join(item, source, target)
                if sub is None:
                    return None
                pairs.extend(sub)
            elif isinstance(item, (list, tuple)) and 1 <= len(item) <= 2:
                if not all(isinstance(x, str) and x.strip() for x in item):
                    return None
                a = _clean_ident(item[0])
                pairs.append(_pair(a, _clean_ident(item[1]) if len(item) == 2 else a))
            elif isinstance(item, Mapping):
                p = _pair_from_mapping(item)
                if p is None:
                    return None
                pairs.append(p)
            else:
                return None
        return pairs
    return None


def join_text(keys: list[dict[str, str]]) -> str:
    """The canonical `on` string for parsed pairs."""
    return ", ".join(
        k["from"] if k["from"] == k["to"] else f"{k['from']} = {k['to']}" for k in keys
    )


def join_label(keys: list[dict[str, str]], source: str = "", target: str = "") -> str:
    """The form the canvas draws on an edge: one pair per line. A shared name
    stands alone; differing names are qualified with their table, because the
    line itself does not say which end declared it."""
    s, t = _leaf(source), _leaf(target)
    return "\n".join(
        k["from"]
        if k["from"] == k["to"]
        else (f"{s}.{k['from']} = {t}.{k['to']}" if s and t else f"{k['from']} → {k['to']}")
        for k in keys
    )


def flip_keys(keys: list[dict[str, str]]) -> list[dict[str, str]]:
    return [_pair(k["to"], k["from"]) for k in keys]


def qualify_table(name: object, relative_to: str | None) -> str:
    """Upper-cased DB.SCHEMA.TABLE. A one- or two-part name is completed from
    the declaring table, the way SQL would resolve it."""
    text = _WS.sub("", str(name or "")).upper().strip(".")
    if not text or relative_to is None:
        return text
    parts = text.split(".")
    base = relative_to.upper().split(".")
    if len(parts) == 1 and len(base) == 3:
        return ".".join(base[:2] + parts)
    if len(parts) == 2 and len(base) == 3:
        return ".".join(base[:1] + parts)
    return text


def _take(entry: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Pop the first populated alias; drop the rest so they do not round-trip."""
    found = next((entry[k] for k in keys if entry.get(k) not in (None, "", [])), None)
    for k in keys:
        entry.pop(k, None)
    return found


def normalize_relationship(rel: object, table: str | None = None) -> tuple[dict | None, list[str]]:
    """One entry in canonical form plus warnings about what had to be guessed.
    None when the entry names no target at all."""
    warnings: list[str] = []
    if isinstance(rel, str):
        if not rel.strip():
            return None, warnings
        rel = {"to": rel}
    if not isinstance(rel, Mapping):
        return None, [f"relationship entry {rel!r} is not an object or a table name; dropped"]
    entry: dict[str, Any] = dict(rel)
    # `on` is a YAML 1.1 boolean: a hand-written `on: ORDER_ID` loads as the key
    # True and the join key silently vanishes. The store quotes it on write;
    # read the unquoted form back too.
    if True in entry:
        entry.setdefault("on", entry.pop(True))

    raw_target = _take(entry, _TARGET_KEYS)
    if raw_target is None or not str(raw_target).strip():
        return None, ["relationship entry has no target table ('to'); dropped"]
    target = qualify_table(raw_target, table)
    if target != _WS.sub("", str(raw_target)).upper().strip("."):
        warnings.append(f"target '{raw_target}' is not fully qualified; recorded as {target}")
    if len(target.split(".")) != 3:
        warnings.append(f"target '{target}' is not a DB.SCHEMA.TABLE name")

    raw_on = _take(entry, _JOIN_KEYS)
    from_col = _take(entry, _PAIR_FROM_KEYS)
    to_col = _take(entry, _PAIR_TO_KEYS)
    if raw_on is None and (from_col is not None or to_col is not None):
        # {"from_column": "A", "to_column": "B"} spelled out at the entry level
        f = from_col if isinstance(from_col, list) else [from_col] if from_col else []
        t = to_col if isinstance(to_col, list) else [to_col] if to_col else []
        raw_on = [list(p) for p in zip(f or t, t or f, strict=False)]

    keys = parse_join(raw_on, table or "", target)
    if keys is None:
        text = (raw_on if isinstance(raw_on, str) else str(raw_on)).strip()
        on = text
        keys = []
        warnings.append(
            f"join key {text!r} to {target} is not a column equality; kept as free text "
            "(write 'COL' or 'THIS_COL = THAT_COL', comma-separated for a composite key)"
        )
    else:
        on = join_text(keys)
        if not keys:
            warnings.append(f"relationship to {target} has no join key ('on')")

    card = normalize_cardinality(_take(entry, _CARDINALITY_KEYS))
    if card and card not in CARDINALITIES:
        warnings.append(
            f"cardinality {card!r} to {target} is not one of {', '.join(CARDINALITIES)}; kept "
            "as written (declaring side first: 'many-to-one' means many rows of this table "
            "per one row of the target)"
        )

    out: dict[str, Any] = {"to": target, "on": on, "keys": keys}
    if card:
        out["cardinality"] = card
    note = entry.pop("note", None)
    if note not in (None, ""):
        out["note"] = str(note).strip()
    out.update(entry)  # anything else round-trips untouched
    return out, warnings


def normalize_relationships(
    value: object, table: str | None = None
) -> tuple[list[dict], list[str]]:
    """Canonical entries for a whole `relationships` list, plus warnings.
    Anything that is not a list yields nothing rather than crashing a reader."""
    out: list[dict] = []
    warnings: list[str] = []
    for rel in value if isinstance(value, list) else []:
        entry, w = normalize_relationship(rel, table)
        warnings.extend(w)
        if entry is not None:
            out.append(entry)
    return out, warnings


def for_disk(rels: list[dict]) -> list[dict]:
    """Entries without their derived fields: what the doc file carries."""
    return [{k: v for k, v in r.items() if k not in DERIVED_KEYS} for r in rels]


def relationship_issues(doc: Mapping[str, Any]) -> list[str]:
    """What is worth a look about a doc's recorded relationships: a join key
    that did not parse, a cardinality nobody recognises, a key column the doc's
    own column list does not have. Used by lint and echoed to agents on write."""
    issues: list[str] = []
    declared = {
        str(c.get("name", "")).upper() for c in doc.get("columns") or [] if isinstance(c, Mapping)
    }
    for rel in doc.get("relationships") or []:
        if not isinstance(rel, Mapping):
            continue
        target = rel.get("to", "?")
        keys = rel.get("keys")
        if keys is None:
            keys = parse_join(rel.get("on"), str(doc.get("table", "")), str(target))
        if rel.get("on") and not keys:
            issues.append(
                f"join key {rel.get('on')!r} to {target} is free text, not column pairs — "
                "the map cannot say which column is whose"
            )
        elif not rel.get("on"):
            issues.append(f"relationship to {target} records no join key")
        card = str(rel.get("cardinality") or "")
        if card and card not in CARDINALITIES:
            issues.append(
                f"cardinality {card!r} to {target} is not one of {', '.join(CARDINALITIES)}"
            )
        if declared and keys:
            unknown = sorted({k["from"] for k in keys} - declared)
            if unknown:
                issues.append(
                    f"join key column(s) {', '.join(unknown)} to {target} are not in this "
                    "table's recorded columns — the wrong side's column, or a stale column list?"
                )
    return issues
