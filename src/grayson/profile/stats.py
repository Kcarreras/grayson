"""Statistics computed locally over a cached sample artifact.

Quantiles and correlation are awkward-to-impossible in portable SQL and, done
per column pair, quadratic in warehouse cost: a 30-column table is 435 pairs.
Computed here instead, in one pass over rows grayson already fetched.

**The evidence chain is weaker here, and says so.** A warehouse query is audited
end to end — statement, plan, result. A local statistic is "this cached artifact,
plus arithmetic grayson did afterwards": the sample's query id is real evidence,
the number derived from it was never verified by the warehouse, and it describes
the sample rather than the table. Every result carries `computed: "local"` and
the sample size, and `confidence_ceiling` says how far a finding resting on it
should go. This is the same trade `cache query` already makes for re-slicing;
the difference is that a correlation *looks* like a measurement of the table, so
it is labelled harder.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: below this many usable pairs, a correlation is noise wearing a number
MIN_PAIRS_FOR_CORRELATION = 30

#: |r| at or above this is worth a human's attention — collinear features, a
#: leaked label, a column that is a copy of another under a different name
NOTABLE_CORRELATION = 0.7


@dataclass(frozen=True)
class NumericSummary:
    column: str
    n: int
    mean: float
    stdev: float
    minimum: float
    maximum: float
    quantiles: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "column": self.column,
            "n": self.n,
            "mean": self.mean,
            "stdev": self.stdev,
            "min": self.minimum,
            "max": self.maximum,
            "quantiles": self.quantiles,
            "computed": "local",
        }


def _numeric(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def numeric_columns(columns: list[str], rows: list[tuple]) -> dict[str, list[float]]:
    """Columns where enough values parse as numbers to be worth summarising.

    Judged among the column's *present* values, not among all rows: a feature
    that is 70% null is still numeric, and treating sparseness as evidence of
    text would silently drop exactly the columns most worth looking at.
    """
    out: dict[str, list[float]] = {}
    for idx, name in enumerate(columns):
        present = [r[idx] for r in rows if r[idx] is not None and r[idx] != ""]
        values = [v for v in (_numeric(v) for v in present) if v is not None]
        # a mostly-unparseable column is text that happens to hold a few digits
        if values and len(values) >= max(1, len(present) // 2):
            out[name] = values
    return out


def quantiles(values: list[float]) -> dict[str, float]:
    """Linear-interpolated quantiles over a sorted copy."""
    if not values:
        return {}
    ordered = sorted(values)

    def at(q: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        pos = q * (len(ordered) - 1)
        low = math.floor(pos)
        high = math.ceil(pos)
        if low == high:
            return ordered[low]
        return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)

    return {"p01": at(0.01), "p25": at(0.25), "p50": at(0.5), "p75": at(0.75), "p99": at(0.99)}


def summarize(columns: list[str], rows: list[tuple]) -> list[dict]:
    out = []
    for name, values in numeric_columns(columns, rows).items():
        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
        out.append(
            NumericSummary(
                column=name,
                n=n,
                mean=mean,
                stdev=math.sqrt(variance),
                minimum=min(values),
                maximum=max(values),
                quantiles=quantiles(values),
            ).to_dict()
        )
    return out


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:  # a constant column correlates with nothing
        return None
    return sxy / math.sqrt(sxx * syy)


def _ranked(values: list[float]) -> list[float]:
    """Average ranks, so ties do not bias Spearman."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def correlations(columns: list[str], rows: list[tuple], method: str = "pearson") -> dict:
    """Pairwise correlation over the sample, strongest first.

    Pairs are computed on rows where BOTH values parse — listwise deletion per
    pair, not per row, so one sparse column does not shrink every other pair's
    sample. Each result carries its own n for exactly that reason.
    """
    if method not in ("pearson", "spearman"):
        raise ValueError(f"method must be pearson or spearman, got {method!r}")
    numeric = numeric_columns(columns, rows)
    pairs = []
    skipped = []
    # a duplicated name is ambiguous: `columns.index` would silently bind every
    # pair to the first occurrence, presenting a confidently wrong r — skip it
    # and say so instead
    duplicates = sorted(name for name in numeric if columns.count(name) > 1)
    for name in duplicates:
        skipped.append(
            {"columns": [name], "reason": "duplicate column name in the sample — ambiguous"}
        )
    names = sorted(name for name in numeric if name not in duplicates)
    # parse each column once, aligned to rows — re-parsing per pair is
    # O(pairs x rows) float() calls, a many-second stall on wide tables
    index = {name: columns.index(name) for name in names}
    parsed = {name: [_numeric(row[index[name]]) for row in rows] for name in names}
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            xs, ys = [], []
            for x, y in zip(parsed[a], parsed[b], strict=True):
                if x is not None and y is not None:
                    xs.append(x)
                    ys.append(y)
            if len(xs) < MIN_PAIRS_FOR_CORRELATION:
                skipped.append({"columns": [a, b], "usable_pairs": len(xs)})
                continue
            if method == "spearman":
                xs, ys = _ranked(xs), _ranked(ys)
            r = pearson(xs, ys)
            if r is None:
                skipped.append({"columns": [a, b], "reason": "a column is constant"})
                continue
            pairs.append({"columns": [a, b], "r": round(r, 4), "n": len(xs)})
    pairs.sort(key=lambda p: abs(p["r"]), reverse=True)
    return {
        "method": method,
        "columns_considered": names,
        "pairs": pairs,
        "notable": [p for p in pairs if abs(p["r"]) >= NOTABLE_CORRELATION],
        "skipped": skipped,
        "computed": "local",
        "confidence_ceiling": "medium",
        "caveat": (
            "computed locally over the cached sample, not by the warehouse. The sample's "
            "query id is real evidence; these numbers are arithmetic grayson did on it "
            "afterwards, and they describe the sample, not the table. Cite the sample "
            "qid, say the statistic was computed locally, and confirm anything decisive "
            "with a warehouse query before resting a high-confidence finding on it."
        ),
    }
