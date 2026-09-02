"""Server-side SVG chart rendering — stdlib only, no JS, no chart library.

The console embeds these SVGs inline, so colors reference the console's CSS
variables with light-mode hex fallbacks; the same markup exported to a
standalone .svg file still renders (fallbacks apply). Series colors are the
three validated categorical slots (all-pairs CVD-safe on both console
surfaces); text and grid always wear text/border tokens, never series color.

Axis labels never hide what varies. Categorical x labels that would be
truncated first lose whatever every label shares (a date prefix, a schema
path, a constant time part), which is printed once under the axis; a label
that is still cut carries its full text in a `<title>` and a `data-full`
attribute, so the file explains itself and the console can show it on hover.
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
    """Canvas and label budget for one rendering size.

    The tile layout is the console's default (session tiles, exports); the
    detail layout backs the chart page and the lightbox, where the picture is
    shown large enough that more, longer labels are legible.
    """

    width: int
    height: int
    margin: dict = field(default_factory=lambda: dict(MARGIN))
    max_labels: int = 8  # categorical x labels drawn; beyond this every k-th
    max_chars: int = 12  # x label length before the ellipsis


TILE = Layout(W, H)
DETAIL = Layout(
    1000, 440, {"top": 16, "right": 20, "bottom": 36, "left": 66}, max_labels=16, max_chars=24
)


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
        self.prefix = ""
        self.suffix = ""
        self.tail = False  # shorten path-like labels from the front, keeping the end

    def sy(self, v: float) -> float:
        frac = (v - self.lo) / (self.hi - self.lo)
        return round(self.y0 - frac * (self.y0 - self.y1), 2)

    def every_kth(self, n: int) -> int:
        return max(1, math.ceil(n / self.layout.max_labels))

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

    def set_affixes(self, labels: list[str]) -> None:
        """Decide how the categorical labels shorten — only when at least one
        would otherwise be truncated, so short labels render exactly as before.

        First whatever every label shares comes off (printed once under the
        axis). Then, if the residues are paths whose ends tell them apart,
        they shorten from the front: `…PAGE_EVENTS`, not `ANALYTICS.W…`.
        """
        max_chars = self.layout.max_chars
        if not labels or max(len(lbl) for lbl in labels) <= max_chars:
            return
        self.prefix, self.suffix = shared_affixes(labels)
        residues = list(dict.fromkeys(self.category(lbl)[0] for lbl in labels))
        if any(len(r) > max_chars for r in residues):
            tails = [_tail_label(r, max_chars) for r in residues]
            heads = [_tick_label(r, max_chars) for r in residues]
            if all(t is not None for t in tails) and len(set(tails)) >= len(set(heads)):
                self.tail = True
        if self.prefix or self.suffix:
            caption = f"{self.prefix}…{self.suffix}"
            self.parts.append(
                f'<text x="{self.x0}" y="{self.layout.height - 3}" {_FONT} '
                f'font-size="10" fill="{_FAINT}" data-shared={quoteattr(caption)}>'
                "<title>shared by every label on this axis; the labels show the part "
                f"that varies</title>{escape(caption)}</text>"
            )

    def x_label(self, px: float, text: str, full: str | None = None) -> None:
        """One categorical tick. `full` is the label as the data has it; `text`
        is what the axis shows before truncation (the residue once shared
        affixes are stripped). Anything hidden rides along in the markup."""
        full = text if full is None else full
        shown = (_tail_label(text, self.layout.max_chars) if self.tail else None) or _tick_label(
            text, self.layout.max_chars
        )
        attrs = ""
        if shown != full:
            attrs = (
                f' class="tick-cut" tabindex="0" data-full={quoteattr(full)}'
                f"><title>{escape(full)}</title"
            )
        self.parts.append(
            f'<text x="{round(px, 2)}" y="{self.y0 + 16}" text-anchor="middle" {_FONT}{attrs}>'
            f"{escape(shown)}</text>"
        )

    def category(self, x: object) -> tuple[str, str]:
        """(display text, full text) for a categorical x value."""
        full = str(x)
        if (
            (self.prefix or self.suffix)
            and full.startswith(self.prefix)
            and full.endswith(self.suffix)
        ):
            return full[len(self.prefix) : len(full) - len(self.suffix)] or full, full
        return full, full

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
    k = plot.every_kth(n)
    color = SERIES_COLORS[0]
    plot.set_affixes([str(p["x"]) for p in points])
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
            plot.x_label(cx, *plot.category(p["x"]))


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

    k = plot.every_kth(n)
    if not numeric_x:
        plot.set_affixes([str(p["x"]) for p in points])
    for i in range(0, n, k):
        plot.x_label(px(i), *plot.category(points[i]["x"]))
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


def _scatter(plot: _Plot, points: list[dict], y_names: list[str]) -> None:
    pairs = [(_num(p["x"]), p) for p in points]
    pairs = [(xv, p) for xv, p in pairs if xv is not None]
    if not pairs:
        return
    xlo, xhi = min(v for v, _ in pairs), max(v for v, _ in pairs)
    if xlo == xhi:
        xlo, xhi = xlo - 1, xhi + 1
    for t in _nice_ticks(xlo, xhi, 6):
        px = plot.x0 + (t - xlo) / (xhi - xlo) * (plot.x1 - plot.x0)
        plot.x_label(px, _fmt(t))
    for si in range(len(y_names)):
        color = SERIES_COLORS[si]
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


def render_svg(spec: dict, data: dict, detail: bool = False) -> str:
    """Render a chart spec + its data (from chart_data) to an SVG string.

    `detail` uses the larger layout — the chart page and the lightbox, where
    twice the labels at twice the length are still legible.
    """
    layout = DETAIL if detail else TILE
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
    plot = _Plot(values, anchor_zero=(spec["kind"] == "bar"), layout=layout)
    plot.frame()
    if spec["kind"] == "bar":
        _bar(plot, points)
    elif spec["kind"] == "line":
        _line(plot, points, y_names)
    else:
        _scatter(plot, points, y_names)
    return plot.svg()


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
