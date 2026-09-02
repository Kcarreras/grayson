"""Charts: spec validation against artifacts, SVG rendering, CLI/MCP/UI surface."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FakeExecutor
from grayson.charts import ChartError, add_chart, chart_data, get_chart, list_charts, render_svg
from grayson.cli import app as cli_app
from grayson.config import GuardSettings
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.ui.server import build_app

runner = CliRunner()
TOKEN = "tok"

DAILY = [
    {"DAY": f"2026-08-{d:02d}", "NULL_RATE": d / 100, "ROW_COUNT": 1000 + d} for d in range(1, 11)
]


def invoke(*args):
    result = runner.invoke(cli_app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.fixture
def session(workspace) -> Session:
    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    return s


@pytest.fixture
def qid(session) -> str:
    out = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=DAILY))
    assert out["status"] == "executed"
    return out["qid"]


def test_add_and_list(session, qid):
    spec = add_chart(
        session, qid, "line", "day", ["null_rate"], "NULL rate by day", note="spike on the 9th"
    )
    assert spec["chart_id"] == "c_001"
    assert spec["x"] == "DAY" and spec["y"] == ["NULL_RATE"]  # resolved to real casing
    assert list_charts(session)[0]["title"] == "NULL rate by day"
    assert get_chart(session, "c_001")["note"] == "spike on the 9th"
    assert any(e["type"] == "chart_added" for e in session.events(10))


def test_validation_failures(session, qid):
    with pytest.raises(ChartError, match="no cached artifact"):
        add_chart(session, "q_9999", "bar", "DAY", ["NULL_RATE"], "t")
    with pytest.raises(ChartError, match="not in artifact columns"):
        add_chart(session, qid, "bar", "NOPE", ["NULL_RATE"], "t")
    with pytest.raises(ChartError, match="no numeric values"):
        add_chart(session, qid, "bar", "NULL_RATE", ["DAY"], "t")
    with pytest.raises(ChartError, match="different columns"):
        add_chart(session, qid, "line", "NULL_RATE", ["NULL_RATE"], "t")
    with pytest.raises(ChartError, match="one y column"):
        add_chart(session, qid, "bar", "DAY", ["NULL_RATE", "ROW_COUNT"], "t")
    with pytest.raises(ChartError, match="numeric x"):
        add_chart(session, qid, "scatter", "DAY", ["NULL_RATE"], "t")
    with pytest.raises(ChartError, match="title is required"):
        add_chart(session, qid, "bar", "DAY", ["NULL_RATE"], "  ")


def test_chart_data_skips_null_y_rows(session):
    rows = [{"K": "a", "V": 1}, {"K": "b", "V": None}, {"K": "c", "V": 3}]
    out = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=rows))
    spec = add_chart(session, out["qid"], "bar", "K", ["V"], "vals")
    data = chart_data(session, spec)
    assert [p["x"] for p in data["points"]] == ["a", "c"]
    assert data["skipped"] == 1


def test_render_svg_line_and_bar(session, qid):
    line = add_chart(session, qid, "line", "DAY", ["NULL_RATE", "ROW_COUNT"], "two series")
    svg = render_svg(line, chart_data(session, line))
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert svg.count('stroke-width="2"') >= 2  # one 2px path per series
    assert "--viz-s2" in svg  # second categorical slot in play
    bar = add_chart(session, qid, "bar", "DAY", ["ROW_COUNT"], "rows per day")
    bsvg = render_svg(bar, chart_data(session, bar))
    assert bsvg.count("<path") >= 10  # one rounded bar per category
    assert "<title>" in bsvg  # native tooltips carry exact values


def test_render_escapes_hostile_values(session):
    rows = [{"K": "<script>alert(1)</script>", "V": 2}, {"K": "b&b", "V": 3}]
    out = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=rows))
    spec = add_chart(session, out["qid"], "bar", "K", ["V"], "hostile labels")
    svg = render_svg(spec, chart_data(session, spec))
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg and "b&amp;b" in svg


def test_render_empty_artifact_message():
    svg = render_svg(
        {"kind": "line", "x": "X", "y": ["Y"]},
        {"points": [], "y": ["Y"], "truncated": False, "cap": 300, "skipped": 0},
    )
    assert "no plottable rows" in svg


def test_cli_chart_flow(workspace, fake_snow_env, tmp_path):
    started = invoke(
        "session", "start", "--workflow", "table-health", "--table", "DB.S.T1",
        "--guard-profile", "moderate", "--skip-snapshot",
    )  # fmt: skip
    sid = started["session"]["id"]
    s = Session(workspace, sid)
    q = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=DAILY))["qid"]
    spec = invoke(
        "chart", "add", sid, "--artifact", q, "--kind", "line",
        "-x", "day", "-y", "null_rate", "--title", "NULL rate by day",
    )  # fmt: skip
    assert spec["chart_id"] == "c_001"
    assert len(invoke("chart", "list", sid)) == 1
    shown = invoke("chart", "show", sid, "c_001")
    assert len(shown["data"]["points"]) == len(DAILY)
    out_file = tmp_path / "chart.svg"
    rendered = invoke("chart", "render", sid, "c_001", "--out", str(out_file))
    assert rendered["points"] == len(DAILY)
    assert out_file.read_text().startswith("<svg")


def test_mcp_chart_tools(workspace, fake_snow_env):
    import asyncio

    from grayson.mcp.server import build_server

    server = build_server(workspace)
    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert {"chart_add", "chart_list"} <= tools


def test_session_page_shows_charts(workspace):
    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    q = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=DAILY))["qid"]
    add_chart(
        s, q, "line", "DAY", ["NULL_RATE"], "NULL rate by day",
        note="<b>hostile</b> note",
    )  # fmt: skip
    client = TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")
    page = client.get(f"/session/{s.id}?t={TOKEN}").text
    assert "Analysis charts" in page and "NULL rate by day" in page
    assert "<svg" in page  # inline SVG made it through as markup
    assert "&lt;b&gt;hostile&lt;/b&gt;" in page  # ...but the note stays escaped
    assert "Plotted data" in page  # table view fold present (accessibility relief)


def test_render_text_bar_line_scatter(session, qid):
    from grayson.charts import render_text

    line = add_chart(session, qid, "line", "DAY", ["NULL_RATE", "ROW_COUNT"], "two series")
    txt = render_text(line, chart_data(session, line))
    assert "two series" in txt and "line" in txt and line["qid"] in txt
    assert "NULL_RATE" in txt and "ROW_COUNT" in txt
    assert any(ch in txt for ch in "▁▂▃▄▅▆▇█")  # sparkline blocks
    assert "min" in txt and "max" in txt and "last" in txt

    bar_rows = [{"K": "checkout", "V": 8123}, {"K": "search", "V": 6410}, {"K": "neg", "V": -50}]
    out = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=bar_rows))
    bar = add_chart(session, out["qid"], "bar", "K", ["V"], "events by page")
    btxt = render_text(bar, chart_data(session, bar))
    assert "checkout" in btxt and "█" in btxt
    assert "-" in btxt.split("neg")[1].splitlines()[0]  # negative value signed

    sc_rows = [{"A": i, "B": i * 2 + 1} for i in range(30)]
    out2 = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=sc_rows))
    sc = add_chart(session, out2["qid"], "scatter", "A", ["B"], "a vs b")
    stxt = render_text(sc, chart_data(session, sc))
    assert "•" in stxt and "┤" in stxt


def test_render_text_empty():
    from grayson.charts import render_text

    txt = render_text(
        {"kind": "bar", "x": "X", "y": ["Y"], "title": "t", "qid": "q_0001"},
        {"points": [], "y": ["Y"], "truncated": False, "cap": 60, "skipped": 0},
    )
    assert "no plottable rows" in txt


def test_chart_add_returns_terminal_text(workspace, fake_snow_env):
    started = invoke(
        "session", "start", "--workflow", "table-health", "--table", "DB.S.T1",
        "--guard-profile", "moderate", "--skip-snapshot",
    )  # fmt: skip
    sid = started["session"]["id"]
    s = Session(workspace, sid)
    q = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=DAILY))["qid"]
    spec = invoke(
        "chart", "add", sid, "--artifact", q, "--kind", "line",
        "-x", "day", "-y", "null_rate", "--title", "NULL rate by day",
    )  # fmt: skip
    assert "NULL rate by day" in spec["text"]
    assert "paste" in spec["hint"]
    shown = invoke("chart", "show", sid, spec["chart_id"])
    assert shown["text"] == spec["text"]


def test_mcp_chart_add_returns_text(workspace, fake_snow_env):
    import asyncio

    from grayson.mcp.server import build_server

    server = build_server(workspace)

    def call(name, args):
        result = asyncio.run(server.call_tool(name, args))
        content = getattr(result, "content", None) or []
        return json.loads(content[0].text)

    started = call("session_start", {"workflow": "table-health", "tables": ["DB.S.T1"]})
    sid = started["session"]["id"]
    s = Session(workspace, sid)
    q = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=DAILY))["qid"]
    spec = call(
        "chart_add",
        {"session_id": sid, "qid": q, "kind": "line", "x": "DAY", "y": ["NULL_RATE"],
         "title": "NULL rate by day"},
    )  # fmt: skip
    assert "▁" in spec["text"] or "█" in spec["text"]


def test_chart_tiles_collapse_beyond_newest_four(workspace):
    import re

    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    q = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=DAILY))["qid"]
    for i in range(6):
        add_chart(s, q, "line", "DAY", ["NULL_RATE"], f"chart {i}")
    client = TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")
    page = client.get(f"/session/{s.id}?t={TOKEN}").text
    tiles = re.findall(
        r'<details class="card chartcard[^"]*"\s+data-fold="c_\d+"\s*( open)?\s*>', page
    )
    assert len(tiles) == 6
    assert sum(1 for open_attr in tiles if open_attr) == 4  # newest four open
    # just-created charts carry the slide-in animation class
    assert "chart-enter" in page


def _ticks(svg: str) -> list[str]:
    import re

    return re.findall(r'text-anchor="middle"[^>]*>(?:<title>[^<]*</title>)?([^<]*)</text>', svg)


def _chart(kind: str, xs: list, detail: bool = False, values: list | None = None, **spec) -> str:
    pts = [{"x": x, "y": [values[i] if values else (i % 7) + 1]} for i, x in enumerate(xs)]
    data = {"points": pts, "y": ["N"], "truncated": False, "cap": 300, "skipped": 0}
    return render_svg({"kind": kind, "x": "T", "y": ["N"], **spec}, data, detail=detail)


def _rows(svg: str) -> list[str]:
    """Horizontal bars' category labels (the only end-anchored text there)."""
    import re

    return re.findall(r'text-anchor="end"[^>]*>(?:<title>[^<]*</title>)?([^<]*)</text>', svg)


def test_shared_affixes_and_tail_labels():
    from grayson.charts.render import _tail_label, shared_affixes

    days = [f"2026-08-{d:02d}T00:00:00" for d in range(1, 31)]
    assert shared_affixes(days) == ("2026-08-", "T00:00:00")  # ISO 'T' is a boundary
    assert shared_affixes(["checkout_completed", "checkout_started"]) == ("checkout_", "")
    assert shared_affixes(["2026-08-10", "2026-08-19"]) == ("2026-08-", "")  # never mid-number
    assert shared_affixes(["100", "200", "300"]) == ("", "")  # numbers keep their digits
    assert shared_affixes(["1.5", "2.5"]) == ("", "")
    assert shared_affixes(["same", "same"]) == ("", "")  # nothing to tell apart
    assert shared_affixes(["ab", "ac"]) == ("", "")  # too short to be worth a caption
    assert _tail_label("ANALYTICS.WEB.PAGE_EVENTS", 12) == "…PAGE_EVENTS"
    assert _tail_label("RAW.STRIPE.REFUNDS", 12) == "…REFUNDS"
    assert _tail_label("A.VERY_LONG_TABLE_NAME_X", 12) is None  # last segment alone too long
    assert _tail_label("short", 12) == "short"


def test_long_labels_never_hide_what_varies():
    days = [f"2026-08-{d:02d}T00:00:00" for d in range(1, 31)]
    # timestamps: the shared date and time parts come off and are captioned once;
    # the ticks show the day, and two-character days fit every other slot
    svg = _chart("line", days)
    assert _ticks(svg) == [f"{d:02d}" for d in range(1, 31, 2)]
    assert 'data-shared="2026-08-…T00:00:00"' in svg
    assert 'data-full="2026-08-01T00:00:00"' in svg  # a residue still reveals the whole
    # fully qualified names on a vertical axis: nothing shared by all, so they
    # shorten from the front (the table name is the distinguishing part), carry
    # the full name, and get as many characters as three slots allow
    fqns = ["ANALYTICS.WEB.PAGE_EVENTS", "ANALYTICS.WEB.SESSIONS", "RAW.STRIPE.REFUNDS"]
    svg = _chart("bar", fqns, orientation="vertical")
    assert _ticks(svg) == ["…WEB.PAGE_EVENTS", "…WEB.SESSIONS", "RAW.STRIPE.REFUNDS"]
    assert 'class="tick-cut" tabindex="0" data-full="ANALYTICS.WEB.PAGE_EVENTS"' in svg
    assert "<title>ANALYTICS.WEB.PAGE_EVENTS</title>" in svg  # the file explains itself
    # a shared schema is captioned, the residue is the bare table name
    svg = _chart("bar", fqns[:2], orientation="vertical")
    assert _ticks(svg) == ["PAGE_EVENTS", "SESSIONS"] and 'data-shared="ANALYTICS.WEB.…"' in svg
    # short labels render exactly as before: no caption, no extra markup
    svg = _chart("bar", ["a<b", 'q"uote', "plain"])
    assert _ticks(svg) == ["a&lt;b", 'q"uote', "plain"]
    assert "data-full" not in svg and "data-shared" not in svg
    # hostile long labels stay escaped in every carrier, either way round
    hostile = [f'<script>alert("{i}")</script>{i}' for i in range(3)]
    for svg in (_chart("bar", hostile, orientation="vertical"), _chart("bar", hostile)):
        assert "<script>" not in svg and "&lt;script&gt;" in svg


def test_labels_never_collide():
    """How many labels are drawn, and how long, comes from the plot width: a
    drawn label always fits its slot, in either layout, for any cardinality."""
    from grayson.charts.render import DETAIL, TILE

    shapes = [
        lambda n: [f"category_{i}" for i in range(n)],
        lambda n: [f"a very long category label number {i} of the cohort" for i in range(n)],
        lambda n: [f"2026-08-{i % 28 + 1:02d}T00:00:00" for i in range(n)],
    ]
    for layout, detail in ((TILE, False), (DETAIL, True)):
        width = layout.width - layout.margin["left"] - layout.margin["right"]
        for n in (2, 7, 13, 30, 60):
            for shape in shapes:
                svg = _chart("line", shape(n), detail=detail)
                shown = _ticks(svg)
                assert shown, (n, detail)  # something is always labelled
                assert max(len(t) for t in shown) * layout.char_px <= width / len(shown)
                assert max(len(t) for t in shown) <= layout.max_chars


def test_detail_layout_shows_more_labels():
    days = [f"2026-08-{d:02d}T00:00:00" for d in range(1, 31)]
    tile, detail = _chart("line", days), _chart("line", days, detail=True)
    assert tile.startswith('<svg viewBox="0 0 640 308"')
    assert detail.startswith('<svg viewBox="0 0 1000 440"')
    assert len(_ticks(tile)) == 15 and len(_ticks(detail)) == 30  # every day, on the wide canvas
    out = _chart("bar", ["ANALYTICS.WEB.PAGE_EVENTS", "RAW.STRIPE.REFUNDS"], detail=True,
                 orientation="vertical")  # fmt: skip
    assert _ticks(out) == ["ANALYTICS.WEB.PAGE_EVENTS", "RAW.STRIPE.REFUNDS"]  # room for all of it


def test_many_or_long_categories_go_horizontal():
    from grayson.charts.render import bar_orientation

    def auto(labels):
        return bar_orientation({"kind": "bar"}, [{"x": x, "y": [1]} for x in labels])

    # ordered scales keep vertical bars; names go horizontal past eight or twelve chars
    assert auto([f"2026-08-{d:02d}" for d in range(1, 31)]) == "vertical"
    assert auto([str(h) for h in range(24)]) == "vertical"
    assert auto(["checkout", "search", "cart", "home"]) == "vertical"
    assert auto([f"page_{i}" for i in range(9)]) == "horizontal"
    assert auto(["ANALYTICS.WEB.PAGE_EVENTS", "RAW.STRIPE.REFUNDS"]) == "horizontal"
    assert bar_orientation({"orientation": "vertical"}, [{"x": f"p{i}"} for i in range(30)]) == (
        "vertical"
    )
    # horizontal: the full names sit on the y axis, one row each, nothing shortened
    fqns = ["ANALYTICS.WEB.PAGE_EVENTS", "ANALYTICS.WEB.SESSIONS", "RAW.STRIPE.REFUNDS"]
    svg = _chart("bar", fqns)
    assert _rows(svg) == fqns and "tick-cut" not in svg
    assert svg.startswith('<svg viewBox="0 0 640 142"')  # three rows, not three planks
    assert "<title>ANALYTICS.WEB.PAGE_EVENTS: 1</title>" in svg  # value tooltips on the bars
    # sixty bars: the tile shows what fits and says so; the detail size holds them all
    many = [f"PAGE_{i:02d}_EVENTS_LONG_NAME" for i in range(60)]
    tile = _chart("bar", many)
    assert tile.startswith('<svg viewBox="0 0 640 322"')
    assert len(_rows(tile)) == 16 and 'data-rows-hidden="44"' in tile
    assert "+44 more rows — enlarge to see them all" in tile
    detail = _chart("bar", many, detail=True)
    assert len(_rows(detail)) == 60 and "rows-hidden" not in detail
    assert detail.startswith('<svg viewBox="0 0 1000 1252"')
    # negative values grow left of the zero line; a null gets its label but no bar
    values = [5, -3, 8, -1, 0, None, 2, 4, 9, 1]
    svg = _chart("bar", [f"segment_{i}_of_the_cohort" for i in range(10)], values=values)
    assert svg.count("<path") == 9 and len(_rows(svg)) == 10
    assert "<title>segment_1_of_the_cohort: -3</title>" in svg
    # a label wider than the margin allows is shortened and still carries the whole
    wide = [f"{'X' * 60}_{i}" for i in range(3)]
    svg = _chart("bar", wide)
    assert "tick-cut" in svg and f'data-full="{"X" * 60}_0"' in svg


def test_orientation_is_validated_and_stored(session, qid):
    with pytest.raises(ChartError, match="bar charts only"):
        add_chart(session, qid, "line", "DAY", ["NULL_RATE"], "t", orientation="horizontal")
    with pytest.raises(ChartError, match="orientation must be"):
        add_chart(session, qid, "bar", "DAY", ["ROW_COUNT"], "t", orientation="sideways")
    spec = add_chart(session, qid, "bar", "DAY", ["ROW_COUNT"], "t", orientation="horizontal")
    assert spec["orientation"] == "horizontal"
    assert get_chart(session, spec["chart_id"])["orientation"] == "horizontal"
    svg = render_svg(spec, chart_data(session, spec))
    assert len(_rows(svg)) == len(DAILY)  # dates would default to vertical; forced sideways
    assert add_chart(session, qid, "bar", "DAY", ["ROW_COUNT"], "t")["orientation"] == "auto"


def test_cli_and_mcp_take_orientation(workspace, fake_snow_env):
    import asyncio

    from grayson.mcp.server import build_server

    started = invoke(
        "session", "start", "--workflow", "table-health", "--table", "DB.S.T1",
        "--guard-profile", "moderate", "--skip-snapshot",
    )  # fmt: skip
    sid = started["session"]["id"]
    s = Session(workspace, sid)
    q = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=DAILY))["qid"]
    spec = invoke(
        "chart", "add", sid, "--artifact", q, "--kind", "bar", "-x", "day", "-y", "row_count",
        "--title", "rows per day", "--orientation", "horizontal",
    )  # fmt: skip
    assert spec["orientation"] == "horizontal"
    refused = runner.invoke(cli_app, [
        "chart", "add", sid, "--artifact", q, "--kind", "line", "-x", "day", "-y", "row_count",
        "--title", "t", "--orientation", "horizontal",
    ])  # fmt: skip
    assert refused.exit_code == 1 and "bar charts only" in refused.output
    server = build_server(workspace)
    result = asyncio.run(server.call_tool("chart_add", {
        "session_id": sid, "qid": q, "kind": "bar", "x": "DAY", "y": ["ROW_COUNT"],
        "title": "rows per day", "orientation": "vertical",
    }))  # fmt: skip
    assert json.loads(result.content[0].text)["orientation"] == "vertical"
