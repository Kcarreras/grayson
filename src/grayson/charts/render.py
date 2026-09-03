"""Server-side SVG chart rendering — stdlib only, no JS, no chart library.

The console embeds these SVGs inline, so colors reference the console's CSS
variables with light-mode hex fallbacks; the same markup exported to a
standalone .svg file still renders (fallbacks apply). Series colors are the
three validated categorical slots (all-pairs CVD-safe on both console
surfaces); text and grid always wear text/border tokens, never series color.

Axis labels never collide and never hide what varies. How many category
labels are drawn, and how long, is computed from the plot width (labels that
would not fit are skipped, not overlapped); labels that would be truncated
first lose whatever every label shares (a date prefix, a schema path, a
constant time part), which is printed once as a caption; a label that is
still cut carries its full text in a `<title>` and a `data-full` attribute,
so the file explains itself and the console can show it on hover. Bar charts
with many categories or long names render horizontally, one row per
category, where the labels have room.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

W, H = 640, 308
MARGIN = {"top": 14, "right": 16, "bottom": 32, "left": 58}

#: validated categorical slots (dataviz reference palette 1-3); var() picks up
#: the console's per-theme values, the fallback hex is the light-mode step
SERIES_COLORS = [
    "var(--viz-s1, #2a78d6)",
    "var(--viz-s2, #eb6834)",
    "var(--viz-s3, #1baf7a)",
]
_TEXT = "var(--muted, #5d6675)"
_FAINT = "var(--faint, #8a93a3)"
_AXIS = "var(--border, #d7dce5)"
_GRID = "var(--border-soft, #e5e9f0)"
_SURFACE = "var(--panel, #ffffff)"

_FONT = f'font-family="ui-sans-serif, system-ui, sans-serif" font-size="11" fill="{_TEXT}"'

#: the shortest shared prefix/suffix worth taking off the labels — below this
#: the caption costs more attention than the characters it saves
_MIN_AFFIX = 3


@dataclass(frozen=True)
class Layout:
    """Canvas and label geometry for one rendering size.

    The tile layout is the console's default (session tiles, exports); the
    detail layout backs the chart page and the lightbox, where the picture is
    shown large enough that more, longer labels are legible. Labels are never
    drawn where they cannot fit: how many and how long is computed from the
    plot width and an estimated glyph advance, not fixed in advance.
    """

    width: int
    height: int
    margin: dict = field(default_factory=lambda: dict(MARGIN))
    max_chars: int = 20  # hard cap on a category label, however much room there is
    min_chars: int = 8  # below this many characters, draw fewer labels instead
    char_px: float = 6.4  # glyph advance assumed at the 11px axis font (generous)
    row_px: int = 16  # horizontal bars: row height once rows are packed
    grow: bool = False  # horizontal bars: grow the canvas to fit every row


TILE = Layout(W, H)
DETAIL = Layout(
    1000,
    440,
    {"top": 16, "right": 20, "bottom": 36, "left": 66},
    max_chars=32,
    min_chars=10,
    row_px=20,
    grow=True,
)

#: bars this thick at most (dataviz: never fill the slot; let the band be air)
_BAR_MAX = 24.0
#: horizontal rows this tall at most, so a three-row chart is not three planks
_ROW_MAX = 32.0
#: category axes that read as an ordered scale keep vertical bars
_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?")


def _fmt(v: float) -> str:
    if v != v:  # NaN guard; upstream filters, belt and braces
        return ""
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.4g}M"
    if a >= 10_000:
        return f"{v / 1_000:.4g}k"
    if v == int(v):
        return str(int(v))
    return f"{v:.4g}"


def _nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / max(n, 1)))
    for mult in (1, 2, 2.5, 5, 10):
        if span / (step * mult) <= n:
            step *= mult
            break
    start = math.ceil(lo / step) * step
    ticks = []
    t = start
    while t <= hi + step * 1e-9:
        ticks.append(round(t, 10))
        t += step
    return ticks


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        v = float(str(value).strip()) if not isinstance(value, int | float) else float(value)
    except ValueError:
        return None
    return v if v == v and abs(v) != float("inf") else None


def _y_domain(values: list[float], anchor_zero: bool) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    if anchor_zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    if lo == hi:
        pad = abs(lo) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    return lo, hi


def _tick_label(x: object, max_chars: int = 12) -> str:
    s = str(x)
    return s if len(s) <= max_chars else s[: max_chars - 1] + "…"


_PATH_SEP = re.compile(r"[./]")


def _tail_label(s: str, max_chars: int) -> str | None:
    """`…` plus as many trailing path segments as fit, for dotted or slashed
    identifiers whose distinguishing part is the end (`DB.SCHEMA.TABLE`).
    None when even the last segment does not fit, or there is no path."""
    if len(s) <= max_chars or not _PATH_SEP.search(s):
        return s if len(s) <= max_chars else None
    segments = _PATH_SEP.split(s)
    seps = _PATH_SEP.findall(s)
    tail = segments[-1]
    if len(tail) + 1 > max_chars:
        return None
    for i in range(len(segments) - 2, -1, -1):
        candidate = segments[i] + seps[i] + tail
        if len(candidate) + 1 > max_chars:
            break
        tail = candidate
    return "…" + tail


# -- shared affixes ---------------------------------------------------------


def _is_boundary(s: str, i: int) -> bool:
    """Whether cutting `s` between s[i-1] and s[i] splits at a separator rather
    than inside a word or a number. ISO 8601's `T` between digits counts as one,
    so `2026-08-10T00:00:00` splits into a date and a time."""
    if i <= 0 or i >= len(s):
        return True
    a, b = s[i - 1], s[i]
    if not a.isalnum() or not b.isalnum():
        return True
    return b == "T" and a.isdigit() and i + 1 < len(s) and s[i + 1].isdigit()


def _common_prefix_len(labels: list[str]) -> int:
    first = labels[0]
    n = min(len(lbl) for lbl in labels)
    i = 0
    while i < n and all(lbl[i] == first[i] for lbl in labels):
        i += 1
    return i


def shared_affixes(labels: list[str]) -> tuple[str, str]:
    """The prefix and suffix every label shares, cut at separator boundaries.

    Empty strings when there is nothing worth taking off: fewer than two
    distinct labels, all-numeric labels (`100`/`200` share `00`, and stripping
    it would lie), an affix shorter than `_MIN_AFFIX`, or a residue that is
    empty or no longer tells the labels apart.
    """
    distinct = list(dict.fromkeys(labels))
    if len(distinct) < 2 or all(_num(lbl) is not None for lbl in distinct):
        return "", ""
    # prefix: the longest common prefix, walked back to a boundary shared by all
    p = _common_prefix_len(distinct)
    while p > 0 and not all(_is_boundary(lbl, p) for lbl in distinct):
        p -= 1
    if p < _MIN_AFFIX:
        p = 0
    prefix = distinct[0][:p]
    # suffix: the same on the reversed strings, then walked inward
    rest = [lbl[p:] for lbl in distinct]
    s = _common_prefix_len([r[::-1] for r in rest])
    s = min(s, min(len(r) for r in rest) - 1)  # every residue keeps at least one char
    while s > 0 and not all(_is_boundary(r, len(r) - s) for r in rest):
        s -= 1
    if s < _MIN_AFFIX:
        s = 0
    suffix = rest[0][len(rest[0]) - s :] if s else ""
    if not prefix and not suffix:
        return "", ""
    residues = [lbl[p : len(lbl) - s] for lbl in distinct]
    if any(not r for r in residues) or len(set(residues)) != len(distinct):
        return "", ""
    return prefix, suffix


@dataclass
class _Categories:
    """How one axis shows its category labels: what every label shares (shown
    once as a caption), whether path-like labels shorten from the front, and
    the character cap. `display(x)` gives (shown, full) for a value."""

    prefix: str = ""
    suffix: str = ""
    tail: bool = False
    chars: int = 20

    @classmethod
    def plan(cls, labels: list[str], fit: int, cap: int) -> _Categories:
        """Decide for `labels` given `fit` characters of room per label (and
        `cap`, the layout's ceiling). Short labels come back untouched, so a
        chart whose labels always fitted renders exactly as before."""
        chars = max(1, min(fit, cap))
        me = cls(chars=chars)
        if not labels or max(len(lbl) for lbl in labels) <= chars:
            return me
        me.prefix, me.suffix = shared_affixes(labels)
        residues = list(dict.fromkeys(me.residue(lbl) for lbl in labels))
        if any(len(r) > chars for r in residues):
            tails = [_tail_label(r, chars) for r in residues]
            heads = [_tick_label(r, chars) for r in residues]
            if all(t is not None for t in tails) and len(set(tails)) >= len(set(heads)):
                me.tail = True
        return me

    @property
    def caption(self) -> str:
        return f"{self.prefix}…{self.suffix}" if (self.prefix or self.suffix) else ""

    def residue(self, x: object) -> str:
        full = str(x)
        if (
            (self.prefix or self.suffix)
            and full.startswith(self.prefix)
            and full.endswith(self.suffix)
        ):
            return full[len(self.prefix) : len(full) - len(self.suffix)] or full
        return full

    def display(self, x: object) -> tuple[str, str]:
        """(shown, full): the residue, shortened to the cap — from the front
        for paths when that keeps them apart, else with a trailing ellipsis."""
        full, text = str(x), self.residue(x)
        shown = (_tail_label(text, self.chars) if self.tail else None) or _tick_label(
            text, self.chars
        )
        return shown, full


def _label_text(px: float, py: float, anchor: str, shown: str, full: str) -> str:
    """One axis label. Anything hidden rides along: a `<title>` (the file
    explains itself) and data-full (the console's hover tip), plus focusability."""
    attrs = ""
    if shown != full:
        attrs = (
            f' class="tick-cut" tabindex="0" data-full={quoteattr(full)}'
            f"><title>{escape(full)}</title"
        )
    return (
        f'<text x="{round(px, 2)}" y="{round(py, 2)}" text-anchor="{anchor}" {_FONT}{attrs}>'
        f"{escape(shown)}</text>"
    )


def _caption_text(px: float, py: float, caption: str) -> str:
    return (
        f'<text x="{round(px, 2)}" y="{round(py, 2)}" {_FONT} font-size="10" fill="{_FAINT}" '
        f"data-shared={quoteattr(caption)}>"
        "<title>shared by every label on this axis; the labels show the part "
        f"that varies</title>{escape(caption)}</text>"
    )


class _Plot:
    """Shared frame: scales, grid, axes. Marks are appended by kind."""

    def __init__(self, y_values: list[float], anchor_zero: bool, layout: Layout = TILE):
        self.layout = layout
        m = layout.margin
        self.x0 = m["left"]
        self.x1 = layout.width - m["right"]
        self.y0 = layout.height - m["bottom"]  # baseline (svg y grows downward)
        self.y1 = m["top"]
        self.lo, self.hi = _y_domain(y_values, anchor_zero)
        self.parts: list[str] = []
        self.cats = _Categories(chars=layout.max_chars)

    def sy(self, v: float) -> float:
        frac = (v - self.lo) / (self.hi - self.lo)
        return round(self.y0 - frac * (self.y0 - self.y1), 2)

    def fit_chars(self, shown: int) -> int:
        """Characters that fit in one label slot when `shown` labels share the
        axis, less one for breathing room."""
        return int((self.x1 - self.x0) / max(shown, 1) / self.layout.char_px) - 1

    def plan_x(self, labels: list[str]) -> int:
        """Decide the x labels: what they share, how they shorten, and every
        k-th one drawn — the smallest k at which the drawn labels cannot
        collide while still saying something (min_chars, or the whole label
        when it is shorter). Returns k."""
        n = len(labels)
        if not n:
            return 1
        self.cats = _Categories.plan(labels, self.fit_chars(n), self.layout.max_chars)
        residues = [self.cats.residue(lbl) for lbl in labels]
        want = min(max(len(r) for r in residues), self.layout.min_chars)
        for k in range(1, n + 1):
            chars = self.fit_chars(math.ceil(n / k))
            if chars >= want:
                self.cats.chars = max(1, min(chars, self.layout.max_chars))
                return k
        return n

    def frame(self) -> None:
        for t in _nice_ticks(self.lo, self.hi):
            y = self.sy(t)
            self.parts.append(
                f'<line x1="{self.x0}" y1="{y}" x2="{self.x1}" y2="{y}" '
                f'stroke="{_GRID}" stroke-width="1"/>'
            )
            self.parts.append(
                f'<text x="{self.x0 - 8}" y="{y + 3.5}" text-anchor="end" {_FONT}>{_fmt(t)}</text>'
            )
        base = self.sy(0.0) if self.lo <= 0 <= self.hi else self.y0
        self.parts.append(
            f'<line x1="{self.x0}" y1="{base}" x2="{self.x1}" y2="{base}" '
            f'stroke="{_AXIS}" stroke-width="1"/>'
        )

    def caption(self) -> None:
        """What every x label shares, once, under the axis."""
        if self.cats.caption:
            self.parts.append(_caption_text(self.x0, self.layout.height - 3, self.cats.caption))

    def x_label(self, px: float, x: object) -> None:
        shown, full = self.cats.display(x)
        self.parts.append(_label_text(px, self.y0 + 16, "middle", shown, full))

    def svg(self) -> str:
        return (
            f'<svg viewBox="0 0 {self.layout.width} {self.layout.height}" role="img" '
            'style="width:100%;height:auto;display:block" '
            'xmlns="http://www.w3.org/2000/svg">' + "".join(self.parts) + "</svg>"
        )


def _bar(plot: _Plot, points: list[dict]) -> None:
    n = len(points)
    slot = (plot.x1 - plot.x0) / n
    w = max(2.0, min(slot * 0.72, 48.0))
    base = plot.sy(0.0) if plot.lo <= 0 <= plot.hi else plot.y0
    k = plot.plan_x([str(p["x"]) for p in points])
    plot.caption()
    color = SERIES_COLORS[0]
    for i, p in enumerate(points):
        v = p["y"][0]
        cx = plot.x0 + slot * (i + 0.5)
        if v is not None:
            x = round(cx - w / 2, 2)
            y_val = plot.sy(v)
            r = min(4.0, w / 2)
            if y_val <= base:  # positive bar: round the top end
                top = min(y_val, base - 0.01)
                d = (
                    f"M{x},{base} L{x},{round(top + r, 2)} "
                    f"Q{x},{round(top, 2)} {round(x + r, 2)},{round(top, 2)} "
                    f"L{round(x + w - r, 2)},{round(top, 2)} "
                    f"Q{round(x + w, 2)},{round(top, 2)} {round(x + w, 2)},{round(top + r, 2)} "
                    f"L{round(x + w, 2)},{base} Z"
                )
            else:  # negative bar: round the bottom end
                bot = max(y_val, base + 0.01)
                d = (
                    f"M{x},{base} L{x},{round(bot - r, 2)} "
                    f"Q{x},{round(bot, 2)} {round(x + r, 2)},{round(bot, 2)} "
                    f"L{round(x + w - r, 2)},{round(bot, 2)} "
                    f"Q{round(x + w, 2)},{round(bot, 2)} {round(x + w, 2)},{round(bot - r, 2)} "
                    f"L{round(x + w, 2)},{base} Z"
                )
            plot.parts.append(
                f'<path d="{d}" fill="{color}">'
                f"<title>{escape(str(p['x']))}: {_fmt(v)}</title></path>"
            )
        if i % k == 0:
            plot.x_label(cx, p["x"])


def _histogram(plot: _Plot, points: list[dict], edges: list[float]) -> None:
    """Contiguous bars over numeric bins, labelled at the edges rather than
    the bins: a histogram's x axis is a scale, and a reader wants to know
    where a bar starts and ends, not its midpoint."""
    n = len(points)
    lo, hi = edges[0], edges[-1]
    span = (hi - lo) or 1.0

    def sx(v: float) -> float:
        return round(plot.x0 + (v - lo) / span * (plot.x1 - plot.x0), 2)

    base = plot.sy(0.0) if plot.lo <= 0 <= plot.hi else plot.y0
    color = SERIES_COLORS[0]
    for p in points:
        count = p["y"][0]
        if count is None:
            continue
        x_start, x_end = sx(p["lo"]), sx(p["hi"])
        top = plot.sy(count)
        h = max(0.0, base - top)
        plot.parts.append(
            f'<rect x="{x_start}" y="{round(top, 2)}" width="{round(x_end - x_start, 2)}" '
            f'height="{round(h, 2)}" fill="{color}" stroke="{_SURFACE}" stroke-width="1">'
            f"<title>{escape(str(p['x']))}: {_fmt(count)}</title></rect>"
        )
    # edge labels: every k-th edge, k chosen so the drawn labels cannot collide;
    # the last edge is always wanted, unless it would sit on the previous label
    labels = [_fmt(e) for e in edges]
    slot = (max(len(lbl) for lbl in labels) + 1) * plot.layout.char_px
    fit = max(1, int((plot.x1 - plot.x0) / slot))
    k = max(1, math.ceil(len(labels) / fit))
    last_drawn: float | None = None
    for i, e in enumerate(edges):
        if i % k and i != n:
            continue
        px = sx(e)
        if last_drawn is not None and px - last_drawn < slot:
            continue
        plot.parts.append(
            f'<text x="{px}" y="{plot.y0 + 16}" text-anchor="middle" {_FONT}>'
            f"{escape(labels[i])}</text>"
        )
        last_drawn = px


def _line(plot: _Plot, points: list[dict], y_names: list[str]) -> None:
    n = len(points)
    xs_num = [_num(p["x"]) for p in points]
    numeric_x = all(v is not None for v in xs_num)
    if numeric_x:
        xlo, xhi = min(xs_num), max(xs_num)
        if xlo == xhi:
            xlo, xhi = xlo - 1, xhi + 1

        def px(i: int) -> float:
            return plot.x0 + (xs_num[i] - xlo) / (xhi - xlo) * (plot.x1 - plot.x0)
    else:

        def px(i: int) -> float:
            return plot.x0 + (plot.x1 - plot.x0) * ((i + 0.5) / n if n > 1 else 0.5)

    k = plot.plan_x([str(p["x"]) for p in points])
    plot.caption()
    for i in range(0, n, k):
        plot.x_label(px(i), points[i]["x"])
    show_markers = n <= 40
    for si in range(len(y_names)):
        color = SERIES_COLORS[si]
        segments: list[list[tuple[float, float]]] = [[]]
        for i, p in enumerate(points):
            v = p["y"][si]
            if v is None:
                if segments[-1]:
                    segments.append([])  # null breaks the line rather than lying across it
                continue
            segments[-1].append((round(px(i), 2), plot.sy(v)))
        for seg in segments:
            if len(seg) >= 2:
                d = "M" + " L".join(f"{x},{y}" for x, y in seg)
                plot.parts.append(
                    f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
                    'stroke-linejoin="round" stroke-linecap="round"/>'
                )
        if show_markers or all(len(seg) < 2 for seg in segments):
            for i, p in enumerate(points):
                v = p["y"][si]
                if v is None:
                    continue
                plot.parts.append(
                    f'<circle cx="{round(px(i), 2)}" cy="{plot.sy(v)}" r="3.5" '
                    f'fill="{color}" stroke="{_SURFACE}" stroke-width="1.5">'
                    f"<title>{escape(str(p['x']))} · {escape(y_names[si])}: {_fmt(v)}"
                    "</title></circle>"
                )


def _scatter(
    plot: _Plot, points: list[dict], y_names: list[str], fits: list[dict | None] | None = None
) -> None:
    pairs = [(_num(p["x"]), p) for p in points]
    pairs = [(xv, p) for xv, p in pairs if xv is not None]
    if not pairs:
        return
    xlo, xhi = min(v for v, _ in pairs), max(v for v, _ in pairs)
    if xlo == xhi:
        xlo, xhi = xlo - 1, xhi + 1
    for t in _nice_ticks(xlo, xhi, 6):
        px = plot.x0 + (t - xlo) / (xhi - xlo) * (plot.x1 - plot.x0)
        plot.x_label(px, _fmt(t))  # numeric ticks: short, never shortened
    fits = fits or []
    for si in range(len(y_names)):
        color = SERIES_COLORS[si]
        fit = fits[si] if si < len(fits) else None
        if fit:
            # the least-squares line, clipped to the plot's y range, and the
            # coefficient it summarises: dashed, so it reads as a summary of
            # the dots rather than as data
            _fit_line(plot, fit, xlo, xhi, color)
            plot.parts.append(
                f'<text x="{plot.x1}" y="{plot.y1 + 12 + 13 * si}" text-anchor="end" {_FONT} '
                f'fill="{color}"><title>Pearson r over {fit["n"]} rows, computed locally '
                f"from the cached artifact — the query id is evidence, this number is "
                f"arithmetic on it</title>r = {fit['r']:.2f} · n = {fit['n']}</text>"
            )
        for xv, p in pairs:
            v = p["y"][si]
            if v is None:
                continue
            px = plot.x0 + (xv - xlo) / (xhi - xlo) * (plot.x1 - plot.x0)
            plot.parts.append(
                f'<circle cx="{round(px, 2)}" cy="{plot.sy(v)}" r="4" fill="{color}" '
                f'fill-opacity="0.85" stroke="{_SURFACE}" stroke-width="1.5">'
                f"<title>{_fmt(xv)} · {escape(y_names[si])}: {_fmt(v)}</title></circle>"
            )


def _fit_line(plot: _Plot, fit: dict, xlo: float, xhi: float, color: str) -> None:
    """The least-squares line over the plotted x range, clipped to the y domain.
    The visible part is one interval of x (the line is monotone), so clipping
    is an intersection of intervals, not a segment walk."""
    slope, intercept = fit["slope"], fit["intercept"]
    lo, hi = plot.lo, plot.hi
    if slope == 0:
        if not lo <= intercept <= hi:
            return
        xa, xb = xlo, xhi
    else:
        x1, x2 = sorted(((lo - intercept) / slope, (hi - intercept) / slope))
        xa, xb = max(xlo, x1), min(xhi, x2)
        if xa >= xb:
            return

    def px(xv: float) -> float:
        return round(plot.x0 + (xv - xlo) / (xhi - xlo) * (plot.x1 - plot.x0), 2)

    ya, yb = intercept + slope * xa, intercept + slope * xb
    plot.parts.append(
        f'<line x1="{px(xa)}" y1="{plot.sy(ya)}" x2="{px(xb)}" y2="{plot.sy(yb)}" '
        f'stroke="{color}" stroke-width="1.5" stroke-dasharray="5 4" stroke-opacity="0.8"/>'
    )


def _bar_path(x0: float, x1: float, y: float, h: float, r: float) -> str:
    """A horizontal bar from x0 (the baseline end, square) to x1 (the data
    end, rounded), r the corner radius; works for bars growing either way."""
    y2 = round(y + h, 2)
    if x1 >= x0:
        xe = max(x1, x0 + 0.01)
        return (
            f"M{round(x0, 2)},{round(y, 2)} L{round(xe - r, 2)},{round(y, 2)} "
            f"Q{round(xe, 2)},{round(y, 2)} {round(xe, 2)},{round(y + r, 2)} "
            f"L{round(xe, 2)},{round(y2 - r, 2)} "
            f"Q{round(xe, 2)},{y2} {round(xe - r, 2)},{y2} L{round(x0, 2)},{y2} Z"
        )
    xe = min(x1, x0 - 0.01)
    return (
        f"M{round(x0, 2)},{round(y, 2)} L{round(xe + r, 2)},{round(y, 2)} "
        f"Q{round(xe, 2)},{round(y, 2)} {round(xe, 2)},{round(y + r, 2)} "
        f"L{round(xe, 2)},{round(y2 - r, 2)} "
        f"Q{round(xe, 2)},{y2} {round(xe + r, 2)},{y2} L{round(x0, 2)},{y2} Z"
    )


def _hbar(points: list[dict], layout: Layout) -> str:
    """Horizontal bars: one row per category, the label margin sized to the
    longest name. The right form for many categories or long names — the
    labels sit on the y axis where there is room, so nothing collides.

    The tile shows the rows that fit its height and says how many more there
    are; the detail layout grows to hold every row.
    """
    n = len(points)
    top, bottom, right = layout.margin["top"], layout.margin["bottom"], layout.margin["right"]
    avail = layout.height - top - bottom
    fits = layout.grow or n * layout.row_px <= avail
    rows = n if fits else max(1, int(avail // layout.row_px))
    row_h = min(_ROW_MAX, max(float(layout.row_px), avail / rows))
    footer = 14 if rows < n else 0
    height = round(top + rows * row_h + bottom + footer)

    # the label margin: as wide as the longest shown label, up to 42% of the canvas
    labels = [str(p["x"]) for p in points[:rows]]
    room = int((0.42 * layout.width - 12) / layout.char_px)
    cats = _Categories.plan(labels, room, max(layout.max_chars, room))
    shown = [cats.display(lbl)[0] for lbl in labels]
    label_px = max(len(t) for t in shown) * layout.char_px if shown else 0
    x0 = round(min(0.42 * layout.width, label_px + 12), 2)
    x1 = layout.width - right

    values = [p["y"][0] for p in points[:rows] if p["y"][0] is not None]
    lo, hi = _y_domain(values or [0.0], anchor_zero=True)

    def sx(v: float) -> float:
        return round(x0 + (v - lo) / (hi - lo) * (x1 - x0), 2)

    parts: list[str] = []
    plot_top, plot_bottom = top, round(top + rows * row_h, 2)
    for t in _nice_ticks(lo, hi, 6 if layout.grow else 5):
        x = sx(t)
        parts.append(
            f'<line x1="{x}" y1="{plot_top}" x2="{x}" y2="{plot_bottom}" '
            f'stroke="{_GRID}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x}" y="{round(plot_bottom + 16, 2)}" text-anchor="middle" {_FONT}>'
            f"{_fmt(t)}</text>"
        )
    zero = sx(0.0)
    parts.append(
        f'<line x1="{zero}" y1="{plot_top}" x2="{zero}" y2="{plot_bottom}" '
        f'stroke="{_AXIS}" stroke-width="1"/>'
    )
    if cats.caption:
        parts.append(_caption_text(x0, 10, cats.caption))
    bar_h = max(2.0, min(_BAR_MAX, row_h * 0.72))
    r = min(4.0, bar_h / 2)
    color = SERIES_COLORS[0]
    for i, p in enumerate(points[:rows]):
        cy = top + row_h * (i + 0.5)
        shown_lbl, full = cats.display(p["x"])
        parts.append(_label_text(x0 - 8, cy + 3.5, "end", shown_lbl, full))
        v = p["y"][0]
        if v is None:
            continue
        d = _bar_path(zero, sx(v), cy - bar_h / 2, bar_h, r)
        parts.append(
            f'<path d="{d}" fill="{color}"><title>{escape(full)}: {_fmt(v)}</title></path>'
        )
    if footer:
        parts.append(
            f'<text x="{x0}" y="{height - 4}" {_FONT} font-size="10" fill="{_FAINT}" '
            f'data-rows-hidden="{n - rows}">+{n - rows} more '
            f"row{'s' if n - rows != 1 else ''} — enlarge to see them all</text>"
        )
    return (
        f'<svg viewBox="0 0 {layout.width} {height}" role="img" '
        'style="width:100%;height:auto;display:block" '
        'xmlns="http://www.w3.org/2000/svg">' + "".join(parts) + "</svg>"
    )


def bar_orientation(spec: dict, points: list[dict]) -> str:
    """Vertical or horizontal bars. An explicit `orientation` in the spec
    wins; otherwise categories that read as an ordered scale (dates,
    numbers) stay vertical, and many categories or long names go
    horizontal, where the labels have room."""
    wanted = spec.get("orientation") or "auto"
    if wanted in ("vertical", "horizontal"):
        return wanted
    labels = [str(p["x"]) for p in points]
    if all(_DATE_RE.match(lbl) or _num(lbl) is not None for lbl in labels):
        return "vertical"
    if len(labels) > 8 or max(len(lbl) for lbl in labels) > 12:
        return "horizontal"
    return "vertical"


def render_svg(spec: dict, data: dict, detail: bool = False) -> str:
    """Render a chart spec + its data (from chart_data) to an SVG string.

    `detail` uses the larger layout — the chart page and the lightbox, where
    twice the labels at twice the length are still legible.
    """
    layout = DETAIL if detail else TILE
    if spec["kind"] == "correlation":
        return _correlation_svg(data, layout)
    points = data["points"]
    y_names = data["y"]
    values = [v for p in points for v in p["y"] if v is not None]
    if not points or not values:
        return (
            f'<svg viewBox="0 0 {layout.width} 80" role="img" '
            'style="width:100%;height:auto;display:block" '
            'xmlns="http://www.w3.org/2000/svg">'
            f'<text x="{layout.width / 2}" y="45" text-anchor="middle" {_FONT}>'
            "no plottable rows in this artifact</text></svg>"
        )
    if spec["kind"] == "bar" and bar_orientation(spec, points) == "horizontal":
        return _hbar(points, layout)
    plot = _Plot(values, anchor_zero=(spec["kind"] in ("bar", "histogram")), layout=layout)
    plot.frame()
    if spec["kind"] == "bar":
        _bar(plot, points)
    elif spec["kind"] == "histogram":
        edges = data.get("edges") or [p["lo"] for p in points] + [points[-1]["hi"]]
        _histogram(plot, points, edges)
    elif spec["kind"] == "line":
        _line(plot, points, y_names)
    else:
        _scatter(plot, points, y_names, data.get("fit"))
    return plot.svg()


#: correlation cells: positive in the first series slot, negative in the
#: second — the two are validated apart on both console surfaces, and the
#: saturation carries |r|
_CORR_POS = SERIES_COLORS[0]
_CORR_NEG = SERIES_COLORS[1]
_CORR_LABEL_CHARS = 14


def _correlation_svg(data: dict, layout: Layout) -> str:
    """An N×N heatmap of the pairwise coefficients: hue is the sign, saturation
    is |r|, and every cell prints its number, since a color alone cannot be
    read to two decimals. Empty cells are pairs with too few usable rows."""
    names = data.get("columns") or []
    matrix = data.get("matrix") or []
    if len(names) < 2 or not data.get("points"):  # every pair below the usable-row floor
        return (
            f'<svg viewBox="0 0 {layout.width} 80" role="img" '
            'style="width:100%;height:auto;display:block" '
            'xmlns="http://www.w3.org/2000/svg">'
            f'<text x="{layout.width / 2}" y="45" text-anchor="middle" {_FONT}>'
            "too few usable rows to correlate</text></svg>"
        )
    n = len(names)
    chars = min(layout.max_chars, _CORR_LABEL_CHARS)
    label_w = int(chars * layout.char_px) + 10
    top = 16 + int(chars * layout.char_px * 0.72)  # rotated labels lean into the top margin
    avail = min(layout.width - label_w - 16, layout.height - top - 8)
    cell = max(18.0, min(56.0, avail / n)) if layout.grow else max(14.0, avail / n)
    width = max(layout.width, int(label_w + cell * n + 16))
    height = int(top + cell * n + 20)  # a footer line for the method and row count
    parts = []
    x0, y0 = label_w, top
    shown = {name: (_tail_label(name, chars) or name[:chars]) for name in names}
    for i, name in enumerate(names):
        cx = x0 + cell * i + cell / 2
        cy = y0 + cell * i + cell / 2
        parts.append(_label_text(x0 - 6, cy + 3.5, "end", shown[name], name))
        parts.append(
            f'<text transform="translate({round(cx + 3, 2)},{y0 - 6}) rotate(-45)" '
            f"{_FONT} data-full={quoteattr(name)}><title>{escape(name)}</title>"
            f"{escape(shown[name])}</text>"
        )
    counts = data.get("counts") or []
    for i in range(n):
        for j in range(n):
            r = matrix[i][j]
            x, y = round(x0 + cell * j, 2), round(y0 + cell * i, 2)
            if i == j:
                parts.append(
                    f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" fill="{_GRID}" '
                    f'stroke="{_SURFACE}" stroke-width="1"/>'
                )
                continue
            if r is None:
                usable = counts[i][j] if counts else 0
                parts.append(
                    f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" fill="none" '
                    f'stroke="{_AXIS}" stroke-width="1" stroke-dasharray="3 2">'
                    f"<title>{escape(names[i])} × {escape(names[j])}: not computed "
                    f"({usable} usable rows)</title></rect>"
                )
                continue
            color = _CORR_POS if r >= 0 else _CORR_NEG
            opacity = round(0.08 + 0.87 * min(1.0, abs(r)), 3)
            parts.append(
                f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" fill="{color}" '
                f'fill-opacity="{opacity}" stroke="{_SURFACE}" stroke-width="1">'
                f"<title>{escape(names[i])} × {escape(names[j])}: r = {r:.3f} "
                f"(n = {counts[i][j] if counts else '?'})</title></rect>"
            )
            if cell >= 26:
                ink = "#ffffff" if abs(r) >= 0.6 else _TEXT
                parts.append(
                    f'<text x="{round(x + cell / 2, 2)}" y="{round(y + cell / 2 + 3.5, 2)}" '
                    f'text-anchor="middle" font-family="ui-sans-serif, system-ui, sans-serif" '
                    f'font-size="10" fill="{ink}" pointer-events="none">{r:.2f}</text>'
                )
    method = data.get("method") or "pearson"
    rows = data.get("rows") or 0
    parts.append(
        f'<text x="{x0}" y="{height - 5}" {_FONT} font-size="10" fill="{_FAINT}">'
        "<title>the coefficients are grayson's arithmetic over the cached artifact, not a "
        f"warehouse result</title>{method} · {rows} rows · computed locally</text>"
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        'style="width:100%;height:auto;display:block" '
        'xmlns="http://www.w3.org/2000/svg">' + "".join(parts) + "</svg>"
    )


def brand_export(svg: str) -> str:
    """Stamp the grayson wordmark onto a standalone export.

    Console-embedded charts stay clean; only files that leave the console
    (`chart render --out`) carry the mark. The canvas grows by a footer strip
    so the mark never overlaps plotted data.
    """
    m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    if m is None:
        return svg
    w, h = int(m.group(1)), int(m.group(2))
    grown = h + 24
    svg = svg.replace(m.group(0), f'viewBox="0 0 {w} {grown}"', 1)
    baseline = grown - 8
    wing_x, wing_y = w - 16 - 46 - 22, baseline - 12
    mark = (
        f'<g transform="translate({wing_x},{wing_y}) scale(0.5)" '
        'fill="var(--brand-accent, #23b8c8)">'
        '<polygon points="2,8 15,20 15,25 4,13"/>'
        '<polygon points="30,8 17,20 17,25 28,13"/></g>'
        f'<text x="{w - 16}" y="{baseline}" text-anchor="end" '
        'font-family="ui-monospace, Menlo, Consolas, monospace" font-size="11" '
        'font-weight="600">'
        '<tspan fill="var(--muted, #8b939b)">gray</tspan>'
        '<tspan fill="var(--ink, #57606a)">son</tspan></text>'
    )
    return svg.replace("</svg>", mark + "</svg>")
