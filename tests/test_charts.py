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
