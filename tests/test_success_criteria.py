import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FakeExecutor
from grayson.checks.regression import RegressionStore
from grayson.cli import app
from grayson.config import GuardSettings
from grayson.core import criteria, engine, proposals
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.mcp.server import build_server
from grayson.ui.server import build_app


@pytest.fixture
def s(workspace):
    s = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(),
        guard_profile="moderate",
    )
    engine.seed_from_workflow(s)
    return s


def draft(s, relative=None):
    qid = run_statement(
        s, "SELECT COUNT(*) N FROM DB.S.T1", executor=FakeExecutor(rows=[{"N": 100}])
    )["qid"]
    p = proposals.record_proposal(s, "ddl_snippet", "Fix import", {"ddl": "human SQL"}, None)
    c = {
        "id": "revenue",
        "name": "Revenue stays within tolerance",
        "source_qid": qid,
        "expectation": {"kind": "scalar", "column": "N", "value": 0},
    }
    if relative is not None:
        c["relative_percent"] = relative
    spec = criteria.set_criteria(s, p["pid"], {"format": 1, "criteria": [c]})
    return p["pid"], spec


def test_approved_relative_rule_fresh_replay_and_exact_promotion(s):
    pid, spec = draft(s, "0.1")
    assert spec["criteria"][0]["expectation"]["value"] == "99.9"
    with pytest.raises(ValueError, match="user action"):
        proposals.decide(s, pid, True, actor="agent", digest=spec["digest"])
    with pytest.raises(ValueError, match="changed since review"):
        proposals.decide(s, pid, True)
    proposals.decide(s, pid, True, digest=spec["digest"])
    with pytest.raises(ValueError, match="applied"):
        criteria.run_verification(s, pid)
    proposals.mark_applied(s, pid)
    with pytest.raises(ValueError, match="computed verdict"):
        proposals.verify(s, pid, "q_0001", "q_0002", "pass")
    result = criteria.run_verification(s, pid, executor=FakeExecutor(rows=[{"N": "100.1"}]))
    assert result["verdict"] == "pass"
    assert result["before_qid"] != result["after_qid"]
    criteria.promote(s, pid, "revenue", "revenue_stable")
    check = RegressionStore(s.workspace.checks_dir).read("revenue_stable")
    assert check.expectation.model_dump(mode="json") == spec["criteria"][0]["expectation"]
    assert check.state == "proposed"
    assert (
        criteria.run_verification(s, pid, executor=FakeExecutor(rows=[{"N": "100.11"}]))["verdict"]
        == "fail"
    )
    assert (
        criteria.run_verification(s, pid, executor=FakeExecutor(status="error", error="offline"))[
            "verdict"
        ]
        == "unproven"
    )
    assert len(s.events(20, event_type="criteria_evaluated")) == 3


def test_review_binding_cannot_be_changed_after_approval(s):
    pid, spec = draft(s)
    proposals.decide(s, pid, True, digest=spec["digest"])
    con = s._con()
    p = s.proposal(pid)
    p["payload"]["ddl"] = "changed fix"
    con.execute("UPDATE proposals SET payload=? WHERE pid=?", (json.dumps(p["payload"]), pid))
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="changed since approval"):
        proposals.mark_applied(s, pid)


def test_criteria_surfaces_and_legacy_proposal(s, monkeypatch):
    pid, spec = draft(s)
    client = TestClient(build_app(s.workspace, token="secret"), base_url="http://localhost")
    assert client.get(f"/session/{s.id}/criteria/{pid}").status_code == 403
    page = client.get(f"/session/{s.id}/criteria/{pid}?t=secret")
    assert page.status_code == 200 and "Approve fix and these criteria" in page.text
    assert client.post(f"/session/{s.id}/proposal/{pid}/approve?t=secret").status_code == 400
    assert (
        client.post(
            f"/session/{s.id}/proposal/{pid}/approve?t=secret", data={"digest": spec["digest"]}
        ).status_code
        == 200
    )
    runner = CliRunner()
    result = runner.invoke(app, ["criteria", "show", s.id, pid])
    assert result.exit_code == 0 and json.loads(result.stdout)["review_current"]
    tools = asyncio.run(build_server(s.workspace).list_tools())
    names = {t.name for t in tools}
    assert {"criteria_set", "criteria_run", "criteria_show", "criteria_promote"} <= names
    assert "criteria_approve" not in names
    p = proposals.record_proposal(s, "ddl_snippet", "Legacy fix", {"ddl": "SQL"}, None)
    proposals.decide(s, p["pid"], True)
    assert "success_criteria" not in s.proposal(p["pid"])["payload"]


def test_sampled_query_and_unsupported_version_cannot_certify(s):
    qid = run_statement(s, "SELECT N FROM DB.S.T1 LIMIT 0", executor=FakeExecutor(rows=[]))["qid"]
    spec = {
        "format": 1,
        "criteria": [
            {
                "id": "x",
                "name": "No missing customers",
                "source_qid": qid,
                "expectation": {"kind": "no_rows"},
            }
        ],
    }
    with pytest.raises(ValueError, match="limited or sampled"):
        criteria.prepare(s, spec)
    spec["format"] = 2
    with pytest.raises(ValueError):
        criteria.prepare(s, spec)


def test_finding_assertions_are_evaluated_not_agent_verdicts(s):
    qid = run_statement(
        s, "SELECT COUNT(*) N FROM DB.S.T1", executor=FakeExecutor(rows=[{"N": 3}])
    )["qid"]
    payload = {
        "title": "Three duplicate IDs",
        "severity": "low",
        "confidence": "medium",
        "summary": "The duplicate count query found three duplicate IDs.",
        "evidence": [qid],
        "machine_claims": {
            "format": 1,
            "criteria": [
                {
                    "id": "duplicates",
                    "name": "Duplicate IDs exist",
                    "source_qid": qid,
                    "expectation": {"kind": "scalar", "column": "N", "operator": "gt", "value": 0},
                }
            ],
        },
    }
    finding = engine.record_finding(s, payload)
    stored = s.finding(finding["fid"])
    assert stored["payload"]["machine_claims"]["criteria"][0]["baseline"]["status"] == "pass"
    payload["machine_claims"]["criteria"][0]["expectation"]["value"] = 9
    with pytest.raises(ValueError, match="does not pass"):
        engine.record_finding(s, payload)
    assert len(s.findings()) == 1


def test_criteria_form_preserves_input_on_error_and_multi_query_report(s, monkeypatch):
    pid, spec = draft(s)
    client = TestClient(build_app(s.workspace), base_url="http://localhost")
    data = {
        "criterion_id": ["duplicate_ids", "revenue"],
        "name": ["Duplicates", "Revenue"],
        "source_qid": ["q_0001", "q_0001"],
        "kind": ["scalar", "scalar"],
        "column": ["N", "N"],
        "operator": ["eq", "eq"],
        "value": ["0", "bad"],
        "upper": ["", ""],
        "relative_percent": ["", ""],
    }
    response = client.post(f"/session/{s.id}/criteria/{pid}/set", data=data)
    assert response.status_code == 400 and 'value="bad"' in response.text
    data["value"] = ["0", "0"]
    assert client.post(f"/session/{s.id}/criteria/{pid}/set", data=data).status_code == 200
    spec = criteria.contract(s, pid)
    proposals.decide(s, pid, True, digest=spec["digest"])
    proposals.mark_applied(s, pid)
    report = criteria.run_verification(s, pid, executor=FakeExecutor(rows=[{"N": 0}]))
    from grayson.records import get_record

    record = get_record(s.workspace, s.id, "proposal", pid)
    assert len(record["evidence_queries"]) == 3
    assert client.get(f"/records/{s.id}/proposal/{pid}").status_code == 200
    assert report["counts"]["pass"] == 2
