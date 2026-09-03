"""Workflow-mandated charts: a checkpoint whose content is a shape closes with
the picture — declared on the template, enforced at the gate, visible in every
surface that shows checkpoints."""

from __future__ import annotations

import asyncio
import json

import pytest
import yaml

from conftest import CHART_ROWS, FakeExecutor, close_checkpoint
from grayson.charts import add_chart
from grayson.config import GuardSettings
from grayson.core import engine
from grayson.core.engine import EnforcementError
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.workflows import get_workflow, list_workflows
from grayson.workflows.authoring import render_preview
from grayson.workflows.lint import lint_template
from grayson.workflows.models import ChartRequirement, WorkflowTemplate

NEEDS_LINE = {
    "name": "needs-line",
    "title": "Needs a line",
    "description": "a workflow with a required chart",
    "required_checks": [
        {
            "key": "trend",
            "title": "Show the trend",
            "description": "the measure over time",
            "charts": [{"kinds": ["line", "bar"], "description": "the measure over time"}],
        },
        {"key": "plain", "title": "No picture needed", "description": "rows suffice"},
    ],
    "suggested_checks": [
        {
            "key": "spread",
            "title": "Spread",
            "description": "how values are distributed",
            "charts": [{"kinds": ["histogram"], "description": "the distribution"}],
        }
    ],
}


@pytest.fixture
def session(workspace) -> Session:
    (workspace.workflows_dir).mkdir(parents=True, exist_ok=True)
    (workspace.workflows_dir / "needs-line.yaml").write_text(yaml.safe_dump(NEEDS_LINE))
    s = Session.create(
        workspace,
        workflow="needs-line",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s, workspace.workflows_dir)
    return s


@pytest.fixture
def qid(session) -> str:
    return run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=CHART_ROWS))[
        "qid"
    ]


def test_requirement_model_and_label():
    req = ChartRequirement(kinds=["line", "bar"], description="the measure  over\n time")
    assert req.allows("bar") and not req.allows("scatter")
    assert req.label() == "line|bar: the measure over time"
    assert ChartRequirement().allows("correlation") and ChartRequirement().label() == "any kind"
    with pytest.raises(ValueError, match="unknown chart kind"):
        ChartRequirement(kinds=["pie"])


def test_lint_flags_a_requirement_with_no_intent():
    tpl = WorkflowTemplate.model_validate(NEEDS_LINE)
    assert lint_template(tpl) == []
    bare = WorkflowTemplate.model_validate(
        {**NEEDS_LINE, "required_checks": [{**NEEDS_LINE["required_checks"][0], "charts": [{}]}]}
    )
    assert any("without saying what it should show" in w for w in lint_template(bare))


def test_gate_refuses_to_close_without_the_required_chart(session, qid, workspace):
    wd = workspace.workflows_dir
    with pytest.raises(
        EnforcementError, match="requires 1 chart.*line\\|bar: the measure over time"
    ):
        engine.complete_checkpoint(session, "trend", [qid], "done", overrides_dir=wd)
    # the wrong kind does not count
    hist = add_chart(session, qid, "histogram", "V", [], "spread")["chart_id"]
    with pytest.raises(EnforcementError, match=f"Cited: {hist} \\(histogram\\)"):
        engine.complete_checkpoint(session, "trend", [qid], "done", overrides_dir=wd, charts=[hist])
    # a chart whose query is not cited does not count either
    other = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=CHART_ROWS))
    line = add_chart(session, other["qid"], "line", "K", ["V"], "trend")["chart_id"]
    with pytest.raises(EnforcementError, match=f"built from {other['qid']}, which is not cited"):
        engine.complete_checkpoint(session, "trend", [qid], "done", overrides_dir=wd, charts=[line])
    with pytest.raises(EnforcementError, match="not a chart of this session"):
        engine.complete_checkpoint(session, "trend", [qid], "", overrides_dir=wd, charts=["c_999"])
    with pytest.raises(EnforcementError, match="twice"):
        engine.complete_checkpoint(
            session, "trend", [qid, other["qid"]], "", overrides_dir=wd, charts=[line, line]
        )
    assert session.checkpoint("trend")["status"] == "open"

    cp = engine.complete_checkpoint(
        session, "trend", [qid, other["qid"]], "done", overrides_dir=wd, charts=[line]
    )
    assert cp["status"] == "complete" and cp["charts"] == [line]
    assert session.events(5)[0]["payload"]["charts"] == [line]
    # a checkpoint with no requirement closes as before, and may cite charts freely
    plain = engine.complete_checkpoint(session, "plain", [qid], "ok", overrides_dir=wd)
    assert plain["charts"] == []


def test_suggested_check_carries_its_requirement_when_taken_up(session, qid, workspace):
    wd = workspace.workflows_dir
    with pytest.raises(EnforcementError, match="histogram: the distribution"):
        engine.complete_checkpoint(session, "spread", [qid], "", overrides_dir=wd)
    cp = close_checkpoint(session, "spread", [qid], "seen", overrides_dir=wd)
    assert cp["status"] == "complete" and len(cp["charts"]) == 1


def test_requirements_travel_with_checkpoint_listings(session, qid, workspace):
    wd = workspace.workflows_dir
    view = {c["key"]: c for c in engine.checkpoints_view(session, wd)}
    assert view["trend"]["requires_charts"] == ["line|bar: the measure over time"]
    assert view["plain"]["requires_charts"] == []

    from grayson.core.brief import build_brief, render_brief

    brief = build_brief(session, wd)
    assert brief["checkpoints"][0]["requires_charts"] == ["line|bar: the measure over time"]
    assert "requires chart(s): line|bar: the measure over time" in render_brief(brief)
    close_checkpoint(session, "trend", [qid], "done", overrides_dir=wd)
    text = render_brief(build_brief(session, wd))
    assert "charts: c_001" in text and "requires chart" not in text

    from grayson.report import build_report, render_markdown

    assert "charts: c_001" in render_markdown(build_report(session, wd))


def test_preview_and_workflow_page_show_the_requirement(workspace, session):
    tpl = get_workflow("needs-line", workspace.workflows_dir)
    text = render_preview(tpl)
    assert "requires chart — line|bar: the measure over time" in text
    assert "spread — Spread  [chart: histogram: the distribution]" in text

    from fastapi.testclient import TestClient

    from grayson.ui.server import build_app

    client = TestClient(build_app(workspace, token="tok"), base_url="http://127.0.0.1")
    page = client.get("/workflows/needs-line?t=tok").text
    assert "line|bar: the measure over time" in page
    session_page = client.get(f"/session/{session.id}?t=tok").text
    assert "needs chart: line|bar: the measure over time" in session_page


def test_mcp_checkpoint_complete_takes_charts(workspace, fake_snow_env, session):
    from grayson.mcp.server import build_server

    server = build_server(workspace)

    def call(name, args):
        result = asyncio.run(server.call_tool(name, args))
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        return json.loads(result.content[0].text)

    qid = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=CHART_ROWS))[
        "qid"
    ]
    listed = {c["key"]: c for c in call("checkpoint_list", {"session_id": session.id})}
    assert listed["trend"]["requires_charts"] == ["line|bar: the measure over time"]
    refused = call(
        "checkpoint_complete", {"session_id": session.id, "key": "trend", "evidence": [qid]}
    )
    assert "requires 1 chart" in refused["error"]
    chart = call(
        "chart_add",
        {"session_id": session.id, "qid": qid, "kind": "bar", "x": "K", "y": ["V"], "title": "t"},
    )
    done = call(
        "checkpoint_complete",
        {
            "session_id": session.id,
            "key": "trend",
            "evidence": [qid],
            "charts": [chart["chart_id"]],
        },
    )
    assert done["status"] == "complete" and done["charts"] == [chart["chart_id"]]


def test_an_unknown_kind_makes_the_file_unloadable(workspace):
    bad = {**NEEDS_LINE, "name": "bad-kind"}
    bad["required_checks"] = [{**NEEDS_LINE["required_checks"][0], "charts": [{"kinds": ["pie"]}]}]
    workspace.workflows_dir.mkdir(parents=True, exist_ok=True)
    (workspace.workflows_dir / "bad-kind.yaml").write_text(yaml.safe_dump(bad))
    from grayson.workflows import override_problems

    problems = override_problems(workspace.workflows_dir)
    assert any("unknown chart kind" in p["problem"] for p in problems)


def test_round_trip_keeps_requirements(workspace):
    from grayson.workflows.authoring import save_workflow_yaml

    saved = save_workflow_yaml(
        workspace.workflows_dir, "needs-line", yaml.safe_dump(NEEDS_LINE), user_id="kane"
    )
    assert saved.check("trend").charts[0].kinds == ["line", "bar"]
    reloaded = get_workflow("needs-line", workspace.workflows_dir)
    assert reloaded.check("spread").charts[0].description == "the distribution"


# -- the core set ----------------------------------------------------------

#: where a core checkpoint's content is a shape on every target, it requires
#: the picture; everything else is left to the agent's judgment
CORE_REQUIREMENTS = {
    "bug-hunter": {"scope_blast_radius": ["line", "bar"]},
    "table-health": {
        "null_completeness": ["bar"],
        "distributions": ["histogram", "bar"],
        "freshness": ["line", "bar"],
    },
    "pipeline-qa": {"rowcount_reconciliation": ["bar"], "measure_conservation": ["bar"]},
    "migration-parity": {
        "rowcount_parity": ["bar"],
        "value_parity": ["bar"],
        "aggregate_parity": ["bar"],
    },
    "feature-readiness": {
        "label_profiled": ["bar", "histogram"],
        "feature_profiled": ["bar"],
        "missingness_characterized": ["bar", "line"],
        "redundancy_assessed": ["correlation", "scatter"],
    },
    "semantic-rule-qa": {"rule_coverage": ["bar"], "accuracy_estimate": ["bar"]},
    "table-onboarding": {"structure_profiled": ["bar"]},
}


def test_core_templates_require_charts_where_the_content_is_a_shape():
    for tpl in list_workflows(None):
        wanted = CORE_REQUIREMENTS[tpl.name]
        for check in tpl.required_checks:
            kinds = [r.kinds for r in check.charts]
            if check.key in wanted:
                assert kinds == [wanted[check.key]], f"{tpl.name}:{check.key}"
            else:
                assert kinds == [], f"{tpl.name}:{check.key} should leave charting open"
        for check in tpl.required_checks + tpl.suggested_checks:
            for req in check.charts:
                assert req.kinds and req.description.strip(), f"{tpl.name}:{check.key}"


def test_core_requirements_are_satisfiable_and_gate(workspace):
    """Every required chart of every core workflow can be built from ordinary
    rows and closes its checkpoint; a bare completion is refused."""
    for tpl in list_workflows(None):
        s = Session.create(
            workspace,
            workflow=tpl.name,
            targets=["DB.S.T1"],
            guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
            guard_profile="moderate",
        )
        qid = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor(rows=CHART_ROWS))[
            "qid"
        ]
        closed = set()
        for check in tpl.required_checks + tpl.suggested_checks:
            if any(d not in closed for d in check.depends_on):
                continue
            if check.charts:
                with pytest.raises(EnforcementError, match="requires"):
                    engine.complete_checkpoint(s, check.key, [qid], "bare")
            cp = close_checkpoint(s, check.key, [qid], "ok")
            assert len(cp["charts"]) == len(check.charts), f"{tpl.name}:{check.key}"
            closed.add(check.key)
