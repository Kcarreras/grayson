"""Terminal renderings of charts — Unicode text, for agent chat and CLIs.

Harness chats (Cursor, Claude Code, Codex) render text, not images, so the
in-chat form of a chart is block characters: labeled bars, sparklines, and a
dot grid. Same deterministic data as the SVG (chart_data), second renderer —
an agent pastes this into its reply so the user sees the shape immediately,
while the console shows the full chart on its live refresh.
"""

from __future__ import annotations

_BLOCKS = "▏▎▍▌▋▊▉█"
_SPARK = "▁▂▃▄▅▆▇█"
_BAR_WIDTH = 36
_LABEL_WIDTH = 16


def _num_str(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.4g}M"
    if a >= 10_000:
        return f"{v / 1_000:.4g}k"
    if v == int(v):
        return str(int(v))
    return f"{v:.4g}"


def _clip(s: str, width: int) -> str:
    return s if len(s) <= width else s[: width - 1] + "…"


def _hbar(frac: float, width: int) -> str:
    """A horizontal bar of `frac` (0..1) of `width` cells, eighth-cell precise."""
    cells = max(0.0, min(1.0, frac)) * width
    full = int(cells)
    rem = cells - full
    bar = "█" * full
    if rem > 1 / 16 and full < width:
        bar += _BLOCKS[min(7, int(rem * 8))]
    return bar


def _spark(values: list[float | None], lo: float, hi: float) -> str:
    span = (hi - lo) or 1.0
    out = []
    for v in values:
        if v is None:
            out.append("·")
        else:
            out.append(_SPARK[min(7, int((v - lo) / span * 8))])
    return "".join(out)


def _text_bar(points: list[dict]) -> list[str]:
    values = [p["y"][0] for p in points]
    peak = max((abs(v) for v in values if v is not None), default=0) or 1.0
    lines = []
    for p in points:
        v = p["y"][0]
        label = _clip(str(p["x"]), _LABEL_WIDTH).rjust(_LABEL_WIDTH)
        if v is None:
            lines.append(f"{label} │ ·")
            continue
        bar = _hbar(abs(v) / peak, _BAR_WIDTH)
        sign = "-" if v < 0 else ""
        lines.append(f"{label} │{sign}{bar} {_num_str(v)}")
    return lines


def _text_line(points: list[dict], y_names: list[str]) -> list[str]:
    all_vals = [v for p in points for v in p["y"] if v is not None]
    lo, hi = min(all_vals), max(all_vals)
    name_w = min(max(len(n) for n in y_names), 20)
    lines = []
    for si, name in enumerate(y_names):
        series = [p["y"][si] for p in points]
        present = [v for v in series if v is not None]
        if not present:
            continue
        lines.append(
            f"{_clip(name, name_w).rjust(name_w)} {_spark(series, lo, hi)}  "
            f"min {_num_str(min(present))} · max {_num_str(max(present))} · "
            f"last {_num_str(present[-1])}"
        )
    lines.append(f"{'x:'.rjust(name_w)} {points[0]['x']} → {points[-1]['x']}")
    return lines


def _text_scatter(
    points: list[dict], y_names: list[str], rows: int = 10, cols: int = 48
) -> list[str]:
    def _numx(p: dict) -> float | None:
        try:
            v = float(str(p["x"]).strip())
        except ValueError:
            return None
        return v if v == v and abs(v) != float("inf") else None

    pairs = [
        (x, p["y"][0]) for p in points if (x := _numx(p)) is not None and p["y"][0] is not None
    ]
    if not pairs:
        return ["(no numeric points)"]
    xlo, xhi = min(x for x, _ in pairs), max(x for x, _ in pairs)
    ylo, yhi = min(y for _, y in pairs), max(y for _, y in pairs)
    xspan, yspan = (xhi - xlo) or 1.0, (yhi - ylo) or 1.0
    grid = [[" "] * cols for _ in range(rows)]
    for x, y in pairs:
        c = min(cols - 1, int((x - xlo) / xspan * cols))
        r = min(rows - 1, int((yhi - y) / yspan * rows))
        grid[r][c] = "•" if grid[r][c] == " " else "●"  # ● marks overplotting
    lines = [f"{_num_str(yhi).rjust(8)} ┤{''.join(grid[0])}"]
    lines += [f"{' ' * 8} │{''.join(row)}" for row in grid[1:-1]]
    lines.append(f"{_num_str(ylo).rjust(8)} ┤{''.join(grid[-1])}")
    pad = " " * 9
    gap = max(0, cols - len(_num_str(xlo)) - len(_num_str(xhi)))
    lines.append(f"{pad}{_num_str(xlo)}{' ' * gap}{_num_str(xhi)}")
    return lines


def render_text(spec: dict, data: dict) -> str:
    """Terminal rendering of a chart — paste-ready for an agent's chat reply."""
    points = data["points"]
    y_names = data["y"]
    header = f"{spec['title']}  [{spec['kind']} · {spec['qid']}]"
    if not points or not any(v is not None for p in points for v in p["y"]):
        return f"{header}\n(no plottable rows)"
    if spec["kind"] == "bar":
        body = _text_bar(points)
    elif spec["kind"] == "histogram":
        body = _text_bar(points)
        stats = data.get("stats") or {}
        if stats:
            body.append(
                f"{data.get('values', 0)} values · {data.get('bins', len(points))} "
                f"bin{'s' if data.get('bins', len(points)) != 1 else ''} of "
                f"{_num_str(data['width'])} · min {_num_str(stats['min'])} · "
                f"median {_num_str(stats['median'])} · mean {_num_str(stats['mean'])} · "
                f"max {_num_str(stats['max'])}"
            )
    elif spec["kind"] == "line":
        body = _text_line(points, y_names)
    else:
        body = _text_scatter(points, y_names)
    footer = []
    if data.get("truncated"):
        footer.append(f"(first {data['cap']} rows)")
    return "\n".join([header, *body, *footer])
