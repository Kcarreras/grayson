"""Chart specs: deterministic visuals agents build from cached artifacts.

A chart is a small validated spec — artifact id, kind, column mapping, title —
stored in the session and rendered server-side from the cached rows. Because
the artifact is an executed query (q_XXXX), every chart is traceable evidence,
and because the console re-renders on its live refresh, the user watches the
agent's analytical narrative build up in near real time. seekql draws exactly
what the cited query returned; there is no untracked data path into a picture.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from seekql.cache.store import QID_RE
from seekql.util import read_json, utcnow, write_json

if TYPE_CHECKING:
    from seekql.core.session import Session

ChartKind = Literal["bar", "line", "scatter"]

CHART_ID_RE = re.compile(r"^c_[0-9]{3,}$")

#: hard cap on plotted series — the palette validates all-pairs at three slots;
#: past that, agents make another chart (facet) instead of a rainbow
MAX_SERIES = 3

#: row caps per kind, so a chart of a million-row artifact stays a chart
MAX_POINTS = {"bar": 60, "line": 300, "scatter": 1000}


class ChartError(ValueError):
    """Invalid chart spec; message says what to fix."""


class ChartSpec(BaseModel):
    chart_id: str
    qid: str
    kind: ChartKind
    x: str
    y: list[str] = Field(min_length=1, max_length=MAX_SERIES)
    title: str
    note: str = ""
    worker: str | None = None
    created_at: str = Field(default_factory=utcnow)

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
) -> dict:
    """Validate a chart against the cached artifact and persist it."""
    if kind not in ("bar", "line", "scatter"):
        raise ChartError(f"kind must be bar, line, or scatter, got {kind!r}")
    if kind == "bar" and len(y) > 1:
        raise ChartError(
            "bar charts take one y column — make one chart per measure "
            "(grouped bars are unreadable at query-result widths)"
        )
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

    try:
        spec = ChartSpec(
            chart_id="c_000",  # placeholder: allocate only after the spec validates
            qid=qid,
            kind=kind,
            x=x_col,
            y=y_cols,
            title=title,
            note=note,
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
    """The rows a chart plots: capped, null-y rows dropped, truncation noted."""
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
