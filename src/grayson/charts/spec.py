"""Chart specs: deterministic visuals agents build from cached artifacts.

A chart is a small validated spec — artifact id, kind, column mapping, title —
stored in the session and rendered server-side from the cached rows. Because
the artifact is an executed query (q_XXXX), every chart is traceable evidence,
and because the console re-renders on its live refresh, the user watches the
agent's analytical narrative build up in near real time. grayson draws exactly
what the cited query returned; there is no untracked data path into a picture.
"""

from __future__ import annotations

import json
import math
import re
from bisect import bisect_right
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

from grayson.cache.store import QID_RE
from grayson.util import read_json, utcnow, write_json

if TYPE_CHECKING:
    from grayson.core.session import Session

ChartKind = Literal["bar", "line", "scatter", "histogram"]
KINDS = ("bar", "line", "scatter", "histogram")
#: bar charts only — `auto` renders many categories or long names as horizontal
#: bars (one row per category, labels on the y axis where they have room) and
#: ordered scales (dates, numbers) as vertical ones; the others force it
Orientation = Literal["auto", "vertical", "horizontal"]

CHART_ID_RE = re.compile(r"^c_[0-9]{3,}$")

#: hard cap on plotted series — the palette validates all-pairs at three slots;
#: past that, agents make another chart (facet) instead of a rainbow
MAX_SERIES = 3

#: row caps per kind, so a chart of a million-row artifact stays a chart.
#: A histogram's points are its bins; the rows it bins are capped separately.
MAX_POINTS = {"bar": 60, "line": 300, "scatter": 1000, "histogram": 40}
#: rows a histogram reads from its artifact — binning is O(n) and local, so
#: the cap is a courtesy to the console's refresh, not a plotting limit
HISTOGRAM_ROW_CAP = 50_000
MAX_BINS = MAX_POINTS["histogram"]
MIN_BINS = 2


class ChartError(ValueError):
    """Invalid chart spec; message says what to fix."""


class ChartSpec(BaseModel):
    chart_id: str
    qid: str
    kind: ChartKind
    x: str
    #: measures; empty for a histogram, which bins `x` itself and counts rows
    y: list[str] = Field(default_factory=list, max_length=MAX_SERIES)
    title: str
    note: str = ""
    orientation: Orientation = "auto"
    #: histogram only: the bin count the author asked for (None = chosen from
    #: the row count); the effective count, after the edges are made round,
    #: is reported by chart_data
    bins: int | None = None
    worker: str | None = None
    created_at: str = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _y_matches_kind(self) -> ChartSpec:
        if self.kind == "histogram":
            if self.y:
                raise ValueError(
                    "a histogram takes no y column — it bins the numeric x column and counts "
                    "rows. To plot a measure you already aggregated, use bar"
                )
        elif not self.y:
            raise ValueError(f"{self.kind} charts need at least one y column (a measure)")
        if self.bins is not None and self.kind != "histogram":
            raise ValueError("bins applies to histograms only")
        return self

    @field_validator("bins")
    @classmethod
    def _bins_in_range(cls, v: int | None) -> int | None:
        if v is not None and not (MIN_BINS <= v <= MAX_BINS):
            raise ValueError(f"bins must be between {MIN_BINS} and {MAX_BINS}, got {v}")
        return v

    @field_validator("qid")
    @classmethod
    def _valid_qid(cls, v: str) -> str:
        if not QID_RE.match(v):
            raise ValueError(f"qid must be an artifact id like q_0003, got {v!r}")
        return v

    @field_validator("title")
    @classmethod
    def _has_title(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("title is required — say what the chart shows")
        return v.strip()


def _num(value: object) -> float | None:
    """Numeric coercion for plotting; None for nulls and non-numeric text."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        v = float(value)
    else:
        try:
            v = float(str(value).strip())
        except ValueError:
            return None
    return v if v == v and abs(v) != float("inf") else None  # drop NaN/inf


def _resolve_column(wanted: str, columns: list[str]) -> str:
    """Case-insensitive column match, returning the artifact's actual name."""
    for c in columns:
        if c.lower() == wanted.lower():
            return c
    raise ChartError(
        f"column {wanted!r} is not in artifact columns {columns} — "
        "check `cache show` for the artifact's shape"
    )


def _charts_dir(session: Session):
    d = session.dir / "charts"
    d.mkdir(exist_ok=True)
    return d


def add_chart(
    session: Session,
    qid: str,
    kind: str,
    x: str,
    y: list[str],
    title: str,
    note: str = "",
    worker: str | None = None,
    orientation: str = "auto",
    bins: int | None = None,
) -> dict:
    """Validate a chart against the cached artifact and persist it."""
    if kind not in KINDS:
        raise ChartError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}")
    # kind-specific shape rules (y per kind, bins) live on ChartSpec and are
    # checked first, before the artifact is read
    try:
        ChartSpec(chart_id="c_000", qid="q_0000", kind=kind, x=x, y=y, title="_", bins=bins)
    except PydanticValidationError as e:
        first = e.errors()[0]
        raise ChartError(str(first.get("ctx", {}).get("error") or first["msg"])) from e
    if kind == "bar" and len(y) > 1:
        raise ChartError(
            "bar charts take one y column — make one chart per measure "
            "(grouped bars are unreadable at query-result widths)"
        )
    if orientation not in ("auto", "vertical", "horizontal"):
        raise ChartError(f"orientation must be auto, vertical, or horizontal, got {orientation!r}")
    if orientation != "auto" and kind != "bar":
        raise ChartError("orientation applies to bar charts only")
    sidecar = session.cache.get(qid)
    if sidecar is None:
        raise ChartError(f"no cached artifact '{qid}' in this session")
    columns = sidecar.get("columns") or []
    if not columns or not sidecar.get("row_count"):
        raise ChartError(f"artifact '{qid}' has no rows to plot")
    x_col = _resolve_column(x, columns)
    y_cols = [_resolve_column(c, columns) for c in y]
    if len(set(y_cols)) != len(y_cols):
        raise ChartError("y columns must be distinct")
    if x_col in y_cols:
        raise ChartError("x and y must be different columns")

    # Sample the artifact so a mis-mapped chart fails at creation, with a clear
    # message, instead of rendering an empty picture in the console.
    sample = session.cache.preview(qid, limit=50)
    for c in y_cols:
        if not any(_num(row.get(c)) is not None for row in sample):
            raise ChartError(
                f"y column {c!r} has no numeric values in the artifact's first rows — "
                "y must be a measure"
            )
    if kind == "scatter" and not any(_num(row.get(x_col)) is not None for row in sample):
        raise ChartError("scatter needs a numeric x column — use bar or line for categories")
    if kind == "histogram" and not any(_num(row.get(x_col)) is not None for row in sample):
        raise ChartError(
            f"histogram needs a numeric x column and {x_col!r} has no numeric values in the "
            "artifact's first rows — bar charts show the counts of categories"
        )

    try:
        spec = ChartSpec(
            chart_id="c_000",  # placeholder: allocate only after the spec validates
            qid=qid,
            kind=kind,
            x=x_col,
            y=y_cols,
            title=title,
            note=note,
            orientation=orientation,
            bins=bins,
            worker=worker,
        )
    except PydanticValidationError as e:
        first = e.errors()[0]
        msg = str(first.get("ctx", {}).get("error") or first["msg"])
        raise ChartError(msg) from e
    spec = spec.model_copy(update={"chart_id": _allocate_id(session)})
    write_json(_charts_dir(session) / f"{spec.chart_id}.json", spec.model_dump())
    session.log_event(
        worker or "agent",
        "chart_added",
        {"chart_id": spec.chart_id, "qid": qid, "kind": kind, "title": title},
    )
    return spec.model_dump()


def _allocate_id(session: Session) -> str:
    d = _charts_dir(session)
    n = len(list(d.glob("c_*.json")))
    while True:
        n += 1
        candidate = d / f"c_{n:03d}.json"
        try:
            candidate.touch(exist_ok=False)  # exclusive create: safe under parallel workers
        except FileExistsError:
            continue
        return candidate.stem


def list_charts(session: Session) -> list[dict]:
    out = []
    for path in sorted((session.dir / "charts").glob("c_*.json")):
        try:
            data = read_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        if data:  # skip the empty placeholder a crashed allocation may leave
            out.append(data)
    return out


def get_chart(session: Session, chart_id: str) -> dict | None:
    if not CHART_ID_RE.match(chart_id):
        return None
    path = session.dir / "charts" / f"{chart_id}.json"
    if not path.is_file():
        return None
    try:
        return read_json(path) or None
    except (json.JSONDecodeError, OSError):
        return None


def chart_data(session: Session, spec: dict) -> dict:
    """The rows a chart plots: capped, null-y rows dropped, truncation noted.

    A histogram's points are its bins, computed here from the artifact's raw
    values; `cap` and `truncated` then describe the rows that were binned.
    """
    if spec["kind"] == "histogram":
        return _histogram_data(session, spec)
    cap = MAX_POINTS[spec["kind"]]
    columns, rows = session.cache.rows(spec["qid"], limit=cap + 1)
    truncated = len(rows) > cap
    rows = rows[:cap]
    x_col, y_cols = spec["x"], spec["y"]
    idx = {c: i for i, c in enumerate(columns)}
    points: list[dict] = []
    skipped = 0
    for row in rows:
        x_raw = row[idx[x_col]] if x_col in idx else None
        ys = [_num(row[idx[c]]) if c in idx else None for c in y_cols]
        if x_raw is None or all(v is None for v in ys):
            skipped += 1
            continue
        points.append({"x": x_raw, "y": ys})
    return {
        "x": x_col,
        "y": y_cols,
        "points": points,
        "truncated": truncated,
        "cap": cap,
        "skipped": skipped,
    }


# -- histograms -------------------------------------------------------------


def default_bins(n: int) -> int:
    """Sturges' rule, clamped: ceil(log2 n) + 1 bins for n values. Coarse for
    heavy tails and multimodal data, which is exactly when an author passes
    `bins` — the default only has to be sensible."""
    if n <= 1:
        return 1
    return max(5, min(30, math.ceil(math.log2(n)) + 1))


def _nice_width(raw: float) -> float:
    """Round a bin width up to 1, 2, 2.5, or 5 times a power of ten, so the
    edges land on numbers a reader can hold in their head."""
    if raw <= 0:
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    for mult in (1, 2, 2.5, 5, 10):
        if mag * mult >= raw - mag * 1e-9:
            return mag * mult
    return mag * 10


def bin_edges(lo: float, hi: float, bins: int) -> list[float]:
    """Bin edges of a round width covering [lo, hi]. The count comes out near
    `bins`, not exactly on it: round edges are worth more than a round count.
    A degenerate range (every value equal) gets one bin around the value.
    Edges are computed as start + i × width, never by running addition, so
    0.1 ten times is 1.0 and a value on an edge lands in the bin it opens."""
    bins = max(bins, 1)
    if hi <= lo:
        width = _nice_width(abs(lo) * 0.1 or 1.0)
        start = math.floor(lo / width) * width
        return [round(start, 10), round(start + width, 10)]
    width = _nice_width((hi - lo) / bins)
    start = math.floor(lo / width) * width
    count = max(1, math.ceil((hi - start) / width - 1e-9))
    if count > MAX_BINS:  # rounding produced more bins than asked: coarsen
        return bin_edges(lo, hi, max(1, bins // 2))
    return [round(start + i * width, 10) for i in range(count + 1)]


def _bin_label(lo: float, hi: float) -> str:
    from grayson.charts.render import _fmt

    return f"{_fmt(lo)}–{_fmt(hi)}"


def _histogram_data(session: Session, spec: dict) -> dict:
    cap = HISTOGRAM_ROW_CAP
    columns, rows = session.cache.rows(spec["qid"], limit=cap + 1)
    truncated = len(rows) > cap
    rows = rows[:cap]
    x_col = spec["x"]
    idx = columns.index(x_col) if x_col in columns else None
    values: list[float] = []
    skipped = 0
    for row in rows:
        v = _num(row[idx]) if idx is not None else None
        if v is None:
            skipped += 1
        else:
            values.append(v)
    base = {
        "x": x_col,
        "y": ["count"],
        "truncated": truncated,
        "cap": cap,
        "skipped": skipped,
        "values": len(values),
    }
    if not values:
        return {**base, "points": [], "bins": 0, "width": None, "edges": []}
    lo, hi = min(values), max(values)
    edges = bin_edges(lo, hi, spec.get("bins") or default_bins(len(values)))
    width = edges[1] - edges[0]
    counts = [0] * (len(edges) - 1)
    last = len(counts) - 1
    for v in values:
        # a bin owns its lower edge; the top edge belongs to the last bin
        counts[min(max(bisect_right(edges, round(v, 10)) - 1, 0), last)] += 1
    points = [
        {"x": _bin_label(edges[i], edges[i + 1]), "lo": edges[i], "hi": edges[i + 1], "y": [c]}
        for i, c in enumerate(counts)
    ]
    mean = sum(values) / len(values)
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        **base,
        "points": points,
        "bins": len(counts),
        "width": width,
        "edges": edges,
        "stats": {"min": lo, "max": hi, "mean": mean, "median": median},
    }
