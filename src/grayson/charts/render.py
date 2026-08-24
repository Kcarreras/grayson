"""Server-side SVG chart rendering — stdlib only, no JS, no chart library.

The console embeds these SVGs inline, so colors reference the console's CSS
variables with light-mode hex fallbacks; the same markup exported to a
standalone .svg file still renders (fallbacks apply). Series colors are the
three validated categorical slots (all-pairs CVD-safe on both console
surfaces); text and grid always wear text/border tokens, never series color.
"""

from __future__ import annotations

import math
import re
from xml.sax.saxutils import escape

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
_AXIS = "var(--border, #d7dce5)"
_GRID = "var(--border-soft, #e5e9f0)"
_SURFACE = "var(--panel, #ffffff)"

_FONT = f'font-family="ui-sans-serif, system-ui, sans-serif" font-size="11" fill="{_TEXT}"'


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


class _Plot:
    """Shared frame: scales, grid, axes. Marks are appended by kind."""

    def __init__(self, y_values: list[float], anchor_zero: bool):
        self.x0 = MARGIN["left"]
        self.x1 = W - MARGIN["right"]
        self.y0 = H - MARGIN["bottom"]  # baseline (svg y grows downward)
        self.y1 = MARGIN["top"]
        self.lo, self.hi = _y_domain(y_values, anchor_zero)
        self.parts: list[str] = []

    def sy(self, v: float) -> float:
        frac = (v - self.lo) / (self.hi - self.lo)
        return round(self.y0 - frac * (self.y0 - self.y1), 2)

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

    def x_label(self, px: float, text: str, full: str | None = None) -> None:
        self.parts.append(
            f'<text x="{round(px, 2)}" y="{self.y0 + 16}" text-anchor="middle" {_FONT}>'
            f"{escape(_tick_label(text))}</text>"
        )

    def svg(self) -> str:
        return (
            f'<svg viewBox="0 0 {W} {H}" role="img" '
            'style="width:100%;height:auto;display:block" '
            'xmlns="http://www.w3.org/2000/svg">' + "".join(self.parts) + "</svg>"
        )


def _every_kth(n: int, max_labels: int = 8) -> int:
    return max(1, math.ceil(n / max_labels))


def _bar(plot: _Plot, points: list[dict]) -> None:
    n = len(points)
    slot = (plot.x1 - plot.x0) / n
    w = max(2.0, min(slot * 0.72, 48.0))
    base = plot.sy(0.0) if plot.lo <= 0 <= plot.hi else plot.y0
    k = _every_kth(n)
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
            plot.x_label(cx, str(p["x"]))


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

    k = _every_kth(n)
    for i in range(0, n, k):
        plot.x_label(px(i), str(points[i]["x"]))
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


def render_svg(spec: dict, data: dict) -> str:
    """Render a chart spec + its data (from chart_data) to an SVG string."""
    points = data["points"]
    y_names = data["y"]
    values = [v for p in points for v in p["y"] if v is not None]
    if not points or not values:
        return (
            f'<svg viewBox="0 0 {W} 80" role="img" '
            'style="width:100%;height:auto;display:block" '
            'xmlns="http://www.w3.org/2000/svg">'
            f'<text x="{W / 2}" y="45" text-anchor="middle" {_FONT}>'
            "no plottable rows in this artifact</text></svg>"
        )
    plot = _Plot(values, anchor_zero=(spec["kind"] == "bar"))
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
