import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FakeExecutor, call_mcp
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


def source_session(s, table="DB.S.T2", connection=None):
    other = Session.create(
        s.workspace,
        workflow="table-health",
        title="Earlier customer investigation",
        targets=[table],
        guard=GuardSettings(),
        guard_profile="moderate",
        connection=connection,
    )
    run_statement(
        other,
        f"SELECT COUNT(*) N FROM {table}",
        label="Customer baseline",
        executor=FakeExecutor(rows=[{"N": 200}]),
    )
    return other


def external_spec(other, **changes):
    return {
        "format": 1,
        "criteria": [
            {
                "name": "Customer count preserved",
                "source_qid": "q_0001",
                "source_session": other.id,
                "expectation": {"kind": "scalar", "column": "N", "operator": "eq", "value": 200},
                **changes,
            }
        ],
    }


def test_generated_ids_preserve_explicit_ids_and_are_unique(s):
    pid, _ = draft(s)
    item = {
        "name": "Duplicate IDs",
        "source_qid": "q_0001",
        "expectation": {"kind": "scalar", "column": "N", "value": 0},
    }
    items = [
        item,
        dict(item),
        {**item, "id": "duplicate_ids"},
        {**item, "name": "123 preserved"},
        {**item, "name": "é"},
    ]
    result = criteria.set_criteria(s, pid, {"criteria": items})
    assert [c["id"] for c in result["criteria"]] == [
        "duplicate_ids_2",
        "duplicate_ids_3",
        "duplicate_ids",
        "criterion_123_preserved",
        "criterion_",
    ]
    assert "id" not in item
    with pytest.raises(ValueError, match="unique"):
        criteria.set_criteria(s, pid, {"criteria": [{**item, "id": "x"}] * 2})
    with pytest.raises(ValueError):
        criteria.set_criteria(s, pid, {"criteria": [{**item, "id": ["invalid"]}]})


def test_cross_session_scope_requires_human_and_preserves_evidence_identity(s):
    pid, original = draft(s)
    other = source_session(s)
    spec = criteria.set_criteria(s, pid, external_spec(other, relative_percent="0.1"))
    assert spec["criteria"][0]["baseline"]["observed"] == "200"  # not current q_0001 = 100
    assert spec["criteria"][0]["expectation"]["value"] == "199.8"
    assert spec["scope_required"] == ["DB.S.T2"]
    assert s.scope_tables == {"DB.S.T1"}
    assert s.proposal(pid)["status"] == "proposed"
    assert (
        criteria.set_criteria(s, pid, external_spec(other))["scope_request"]["iid"]
        == spec["scope_request"]["iid"]
    )
    spec = criteria.contract(s, pid)
    with pytest.raises(ValueError, match="scope expansion"):
        proposals.decide(s, pid, True, digest=spec["digest"])
    client = TestClient(build_app(s.workspace), base_url="http://localhost")
    assert "Scope approval needed" in client.get(f"/session/{s.id}/criteria/{pid}").text
    iid = spec["scope_request"]["iid"]
    assert client.post(f"/session/{s.id}/intervention/{iid}/respond", data={}).status_code == 200
    assert criteria.contract(s, pid)["scope_required"] == ["DB.S.T2"]  # declined
    with pytest.raises(ValueError, match="scope expansion"):
        proposals.decide(s, pid, True, digest=spec["digest"])
    iid = criteria.request_scope(s, pid)["iid"]
    assert (
        client.post(
            f"/session/{s.id}/intervention/{iid}/respond", data={"granted": ["DB.S.T2"]}
        ).status_code
        == 200
    )
    assert criteria.contract(s, pid)["scope_required"] == []
    assert s.proposal(pid)["status"] == "proposed"  # grant is not fix approval
    with pytest.raises(ValueError, match="changed since review"):
        proposals.decide(s, pid, True, digest=original["digest"])
    proposals.decide(s, pid, True, digest=spec["digest"])
    proposals.mark_applied(s, pid)
    result = criteria.run_verification(s, pid, executor=FakeExecutor(rows=[{"N": 200}]))
    assert result["verdict"] == "pass"
    assert result["evidence_refs"] == [
        {"session_id": other.id, "qid": "q_0001"},
        {"session_id": s.id, "qid": "q_0002"},
    ]
    from grayson.records import get_library_record, get_record

    for record in [
        get_record(s.workspace, s.id, "proposal", pid),
        get_library_record(s.workspace.records_dir, s.id, pid),
    ]:
        assert [(q["session_id"], q["qid"]) for q in record["evidence_queries"]] == [
            (other.id, "q_0001"),
            (s.id, "q_0002"),
        ]
    assert f"/session/{other.id}/query/q_0001" in client.get(f"/records/{s.id}/proposal/{pid}").text


def test_scope_only_baseline_keeps_finding_target_requirement(s):
    pid, _ = draft(s)
    other = source_session(s)
    # A query already executed out of scope in this session also needs approval.
    qid = run_statement(
        s, "SELECT COUNT(*) N FROM DB.S.T2", executor=FakeExecutor(rows=[{"N": 200}])
    )["qid"]
    definition = external_spec(s)
    definition["criteria"][0]["source_qid"] = qid
    assert criteria.set_criteria(s, pid, definition)["scope_required"] == ["DB.S.T2"]
    s.widen_scope(["DB.S.T2"])
    assert not criteria.contract(s, pid)["scope_required"]
    with pytest.raises(ValueError, match="table under investigation"):
        criteria.prepare(s, definition)
    with pytest.raises(ValueError, match="current session"):
        criteria.prepare(s, external_spec(other))


def test_partial_scope_grant_leaves_remaining_tables_blocked(s):
    pid, _ = draft(s)
    other = source_session(s)
    run_statement(other, "SELECT COUNT(*) N FROM DB.S.T3", executor=FakeExecutor(rows=[{"N": 200}]))
    definition = external_spec(other)
    definition["criteria"].append({**definition["criteria"][0], "source_qid": "q_0002"})
    spec = criteria.set_criteria(s, pid, definition)
    assert spec["scope_required"] == ["DB.S.T2", "DB.S.T3"]
    client = TestClient(build_app(s.workspace), base_url="http://localhost")
    response = client.post(
        f"/session/{s.id}/intervention/{spec['scope_request']['iid']}/respond",
        data={"granted": ["DB.S.T2"]},
    )
    assert response.url.path == f"/session/{s.id}/criteria/{pid}"
    assert criteria.contract(s, pid)["scope_required"] == ["DB.S.T3"]
    with pytest.raises(ValueError, match="scope expansion"):
        proposals.decide(s, pid, True, digest=spec["digest"])
    assert criteria.request_scope(s, pid)["request"]["tables"] == ["DB.S.T3"]


def test_form_accepts_cross_session_query_and_hides_unused_rule_values(s):
    pid, _ = draft(s)
    other = source_session(s)
    client = TestClient(build_app(s.workspace), base_url="http://localhost")
    data = {
        "criterion_id": "",
        "name": "Customer count",
        "source_qid": f"{other.id}::q_0001",
        "kind": "scalar",
        "column": "N",
        "operator": "between",
        "value": "ignored",
        "upper": "",
        "relative_percent": "0",
    }
    response = client.post(f"/session/{s.id}/criteria/{pid}/set", data=data)
    assert response.status_code == 200
    spec = criteria.contract(s, pid)
    assert spec["criteria"][0]["source_session"] == other.id
    assert spec["criteria"][0]["expectation"]["value"] == "200"
    assert spec["criteria"][0]["expectation"]["upper"] == "200"
    assert spec["scope_required"] == ["DB.S.T2"]
    data["kind"] = "no_rows"
    assert client.post(f"/session/{s.id}/criteria/{pid}/set", data=data).status_code == 200
    assert criteria.contract(s, pid)["criteria"][0]["relative_percent"] is None


def test_replacing_scope_criteria_cancels_obsolete_request_and_rejects_bad_sources(s):
    pid, _ = draft(s)
    other = source_session(s)
    spec = criteria.set_criteria(s, pid, external_spec(other))
    criteria.set_criteria(s, pid, external_spec(s))
    assert s.intervention(spec["scope_request"]["iid"])["status"] == "cancelled"
    assert not s.interventions("open")
    different = source_session(s, connection="different")
    with pytest.raises(ValueError, match="same connection"):
        criteria.set_criteria(s, pid, external_spec(different))
    with pytest.raises(ValueError, match="invalid session"):
        criteria.set_criteria(s, pid, external_spec(other, source_session="../escape"))
    with pytest.raises(ValueError, match="successfully executed"):
        criteria.set_criteria(s, pid, external_spec(other, source_qid="q_9999"))
    assert not s.interventions("open")


def test_query_picker_prioritizes_current_and_searches_pages_without_running_sql(s):
    draft(s)
    other = source_session(s)
    incompatible = source_session(s, connection="another")
    sources = criteria.query_sessions(s)
    assert sources[0]["id"] == s.id
    assert next(x for x in sources if x["id"] == incompatible.id)["compatible"] is False
    first = criteria.query_choices(s, "all", limit=1)
    assert first["queries"][0]["session_id"] == s.id
    second = criteria.query_choices(s, "all", offset=first["next_offset"], limit=1)
    assert second["queries"][0]["session_id"] == other.id
    assert second["next_offset"] is None
    assert (
        criteria.query_choices(s, "all", search="customer")["queries"][0]["session_id"] == other.id
    )
    assert criteria.query_choices(s, "all", search="no match")["queries"] == []
    client = TestClient(build_app(s.workspace, token="secret"), base_url="http://localhost")
    assert client.get(f"/session/{s.id}/criteria-queries").status_code == 403
    result = client.get(
        f"/session/{s.id}/criteria-queries?t=secret",
        params={
            "source_session": other.id,
            "search": "db.s.t2",
            "t": "secret",
        },
    )
    assert result.status_code == 200
    assert result.json()["queries"][0]["scope_required"] == ["DB.S.T2"]
    assert client.get(f"/session/{s.id}/criteria-queries?t=secret&offset=-1").status_code == 400
    assert s.query_stats()["total"] == 1
    assert not s.interventions()
    assert s.scope_tables == {"DB.S.T1"}


def test_mcp_drafts_file_and_generated_criteria_for_ui_review(s):
    draft(s)

    path = s.workspace.root / "model.sql"
    path.write_text("before", encoding="utf-8")
    mcp = build_server(s.workspace)
    result = call_mcp(
        mcp,
        "proposal_draft_file",
        {
            "session_id": s.id,
            "target_file": "model.sql",
            "new_content": "after",
            "title": "Fix duplicate IDs",
            "success_criteria": external_spec(s),
        },
    )
    assert result["status"] == "proposed"
    assert result["payload"]["success_criteria"]["criteria"][0]["id"] == "customer_count_preserved"
    assert path.read_text(encoding="utf-8") == "before"
    client = TestClient(build_app(s.workspace), base_url="http://localhost")
    page = client.get(f"/session/{s.id}/criteria/{result['pid']}")
    assert page.status_code == 200
    assert 'value="customer_count_preserved"' in page.text
    assert "Customer count preserved" in page.text and "Approve fix and these criteria" in page.text
    blank = proposals.record_proposal(s, "ddl_snippet", "New fix", {"ddl": "SQL"}, None)
    page = client.get(f"/session/{s.id}/criteria/{blank['pid']}").text
    assert 'value="criterion_1"' in page
    assert page.count('class="help"') >= 10
    assert "Current session" in page
    choices = call_mcp(mcp, "criteria_queries", {"session_id": s.id})
    assert choices["sessions"][0]["current"]
