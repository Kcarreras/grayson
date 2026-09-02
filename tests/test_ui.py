"""Web console tests via FastAPI TestClient."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import FakeExecutor
from grayson.config import GuardSettings
from grayson.core import engine
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.interventions import build_request
from grayson.ui.server import build_app

TOKEN = "test-token"


@pytest.fixture
def client(workspace):
    # base_url sets the Host header to a loopback name the server's DNS-rebinding
    # guard accepts (the TestClient default 'testserver' is rejected by design).
    return TestClient(build_app(workspace, token=TOKEN), base_url="http://127.0.0.1")


@pytest.fixture
def session(workspace):
    s = Session.create(
        workspace,
        workflow="semantic-rule-qa",
        targets=["DB.S.URLS"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
        title="url QA",
    )
    engine.seed_from_workflow(s)
    return s


def test_setup_inputs_render_on_session_page(client, session):
    session.set_setup_inputs({"rule_statement": "every URL maps to exactly one category"})
    page = client.get(f"/session/{session.id}", params={"t": TOKEN})
    assert page.status_code == 200
    assert "Setup inputs" in page.text
    assert "every URL maps to exactly one category" in page.text


def test_token_required(client, session):
    assert client.get("/").status_code == 403
    assert client.get(f"/?t={TOKEN}").status_code == 200


def test_dashboard_lists_session(client, session):
    r = client.get(f"/?t={TOKEN}")
    assert r.status_code == 200
    assert "url QA" in r.text
    assert session.id in r.text


def test_session_detail(client, session):
    r = client.get(f"/session/{session.id}?t={TOKEN}")
    assert r.status_code == 200
    assert "Checkpoints" in r.text
    assert "sample_for_review" in r.text  # a required check key


def test_rename_session_via_ui(client, session):
    r = client.post(
        f"/session/{session.id}/title?t={TOKEN}",
        data={"title": "NULL email regression"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "NULL email regression" in r.text
    assert session.get_meta("title") == "NULL email regression"


def test_records_show_renamed_session_title(client, session):
    qid = run_statement(session, "SELECT * FROM DB.S.URLS", executor=FakeExecutor())["qid"]
    fid = engine.record_finding(
        session,
        {
            "title": "Wrong buckets",
            "severity": "low",
            "confidence": "high",
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "Some URLs land in the wrong category bucket.",
            "evidence": [qid],
            "extra": {
                "finding_kind": "rule_defect",
                "rule_location": "categorize_url()",
                "observed_behaviour": "some URLs land in the wrong bucket",
                "expected_behaviour": "match the documented taxonomy",
            },
        },
    )["fid"]
    client.post(
        f"/session/{session.id}/title?t={TOKEN}",
        data={"title": "Renamed for the archive"},
        follow_redirects=False,
    )
    assert "Renamed for the archive" in client.get(f"/records?t={TOKEN}").text
    detail = client.get(f"/records/{session.id}/finding/{fid}?t={TOKEN}")
    assert detail.status_code == 200
    assert "Renamed for the archive" in detail.text


def test_query_detail_page_highlights_sql(client, session):
    from grayson.core.run import run_statement

    run_statement(session, "SELECT a FROM DB.S.T1 WHERE b = 'x' -- why", executor=FakeExecutor())
    r = client.get(f"/session/{session.id}/query/q_0001?t={TOKEN}")
    assert r.status_code == 200
    assert '<span class="sql-k">SELECT</span>' in r.text
    assert '<span class="sql-c">-- why</span>' in r.text
    # the session page links the qid chip and carries the SQL as hover text
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert f"/session/{session.id}/query/q_0001" in page
    assert client.get(f"/session/{session.id}/query/q_9999?t={TOKEN}").status_code == 404


def test_query_detail_shows_execution_error(client, session):
    err = FakeExecutor(status="error", error="no such column: BIRTHDATE")
    run_statement(session, "SELECT nope FROM DB.S.T1", executor=err)
    r = client.get(f"/session/{session.id}/query/q_0001?t={TOKEN}")
    assert r.status_code == 200
    assert "no such column: BIRTHDATE" in r.text


def test_query_detail_escapes_hostile_sql(client, session):
    from grayson.core.run import run_statement

    run_statement(
        session,
        "SELECT '<script>alert(1)</script>' FROM DB.S.T1",
        executor=FakeExecutor(),
    )
    r = client.get(f"/session/{session.id}/query/q_0001?t={TOKEN}")
    assert "<script>alert(1)" not in r.text
    assert "&lt;script&gt;" in r.text


def test_unknown_session_404(client):
    assert client.get(f"/session/nope?t={TOKEN}").status_code == 404


def test_label_intervention_render_and_respond(client, session):
    req = build_request(
        "label_sample",
        {
            "rows": [{"url": "cnn.com"}, {"url": "shop.com"}],
            "labels": ["news", "shop", "other"],
            "instructions": "label each URL",
        },
    )
    iid = session.add_intervention("label_sample", "Label URLs", "feeds accuracy", req)

    page = client.get(f"/session/{session.id}/intervention/{iid}?t={TOKEN}")
    assert page.status_code == 200
    assert "cnn.com" in page.text and "label each URL" in page.text

    resp = client.post(
        f"/session/{session.id}/intervention/{iid}/respond?t={TOKEN}",
        data={"label_0": "news", "note_0": "", "label_1": "shop", "note_1": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    item = session.intervention(iid)
    assert item["status"] == "answered"
    assert item["response"]["labeled_count"] == 2


def test_bad_label_response_shows_error(client, session):
    req = build_request("label_sample", {"rows": [{"u": 1}], "labels": ["a", "b"]})
    iid = session.add_intervention("label_sample", "t", "", req)
    resp = client.post(
        f"/session/{session.id}/intervention/{iid}/respond?t={TOKEN}",
        data={"label_0": "zzz"},
    )
    assert resp.status_code == 400
    assert session.intervention(iid)["status"] == "open"


def test_accept_finding_via_ui(client, session):
    qid = run_statement(session, "SELECT * FROM DB.S.URLS", executor=FakeExecutor())["qid"]
    finding = engine.record_finding(
        session,
        {
            "title": "Miscategorized URLs",
            "severity": "medium",
            "confidence": "medium",
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "About 8% of URLs fall into the wrong category bucket.",
            "evidence": [qid],
            "extra": {
                "finding_kind": "rule_defect",
                "rule_location": "categorize_url()",
                "observed_behaviour": "some URLs land in the wrong bucket",
                "expected_behaviour": "match the documented taxonomy",
            },
        },
    )
    r = client.post(
        f"/session/{session.id}/finding/{finding['fid']}/accept?t={TOKEN}",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert session.finding(finding["fid"])["accepted"] is True


def test_advance_gate_blocks_in_ui(client, session):
    r = client.post(f"/session/{session.id}/advance?t={TOKEN}", data={"to": "review"})
    assert r.status_code == 400
    assert "required checkpoints still open" in r.text


def test_proposal_approve_via_ui(client, session):
    from grayson.core import proposals

    qid = run_statement(session, "SELECT * FROM DB.S.URLS", executor=FakeExecutor())["qid"]
    fid = engine.record_finding(
        session,
        {
            "title": "Bad categories",
            "severity": "high",
            "confidence": "high",
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "Categories are wrong for a meaningful fraction of URLs.",
            "evidence": [qid],
            "extra": {
                "finding_kind": "rule_defect",
                "rule_location": "categorize_url()",
                "observed_behaviour": "some URLs land in the wrong bucket",
                "expected_behaviour": "match the documented taxonomy",
            },
        },
    )["fid"]
    p = proposals.record_proposal(
        session,
        "ddl_snippet",
        "Fix rule",
        {"ddl": "CREATE OR REPLACE VIEW v AS SELECT 1", "rationale": "adds finance bucket"},
        fid,
    )
    # renders on the session page
    page = client.get(f"/session/{session.id}?t={TOKEN}")
    assert "Fix rule" in page.text and "awaiting decision" in page.text
    # approve
    r = client.post(
        f"/session/{session.id}/proposal/{p['pid']}/approve?t={TOKEN}", follow_redirects=False
    )
    assert r.status_code == 303
    assert session.proposal(p["pid"])["status"] == "approved"


# -- honest outcomes: waive and clean close in the console ----------------


def _clear_checks(session, waive: str | None = None) -> list[str]:
    out = run_statement(session, "SELECT * FROM DB.S.URLS", executor=FakeExecutor())
    keys = engine.workflow_for(session).required_check_keys()
    for key in keys:
        if key == waive:
            continue
        engine.complete_checkpoint(session, key, [out["qid"]], "done")
    return [out["qid"]]


def test_clean_close_card_appears_only_when_the_run_is_clean(client, session):
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert "Close as clean" not in page
    _clear_checks(session)
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert "This run came back clean" in page
    assert "Close as clean" in page


def test_close_clean_records_the_outcome(client, session):
    _clear_checks(session)
    r = client.post(
        f"/session/{session.id}/close-clean?t={TOKEN}",
        data={"note": "coverage and labels both sound"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert session.stage == "closed"
    assert session.outcome == "clean"
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert "closed clean" in page
    assert "coverage and labels both sound" in page


def test_close_clean_refused_with_work_outstanding(client, session):
    r = client.post(f"/session/{session.id}/close-clean?t={TOKEN}", data={"note": ""})
    assert r.status_code == 400
    assert "checkpoints still open" in r.text
    assert session.stage != "closed"


def test_waive_from_the_console(client, session):
    _clear_checks(session, waive="error_pattern_analysis")
    r = client.post(
        f"/session/{session.id}/checkpoint/error_pattern_analysis/waive?t={TOKEN}",
        data={"reason": "no misassignments to pattern-match"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert session.checkpoint("error_pattern_analysis")["status"] == "waived"
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert "waived" in page
    assert "no misassignments to pattern-match" in page


def test_waive_without_a_reason_is_refused(client, session):
    r = client.post(
        f"/session/{session.id}/checkpoint/rule_coverage/waive?t={TOKEN}", data={"reason": "  "}
    )
    assert r.status_code == 400
    assert "requires a reason" in r.text
    assert session.checkpoint("rule_coverage")["status"] == "open"


def test_suggested_checks_show_as_breadth_not_gates(client, session):
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert "None blocks a stage" in page  # the suggested-checks widget, not a gate
    # they must not count against the checkpoint gate
    assert engine.readiness(session)["required_checks"] == [c["key"] for c in session.checkpoints()]


def test_taking_up_a_suggested_check_records_it_as_a_checkpoint(client, session):
    out = run_statement(session, "SELECT * FROM DB.S.URLS", executor=FakeExecutor())
    engine.complete_checkpoint(session, "rule_drift", [out["qid"]], "accuracy is flat")
    assert session.checkpoint("rule_drift")["status"] == "complete"
    ready = engine.readiness(session)
    # closed, but it never becomes a gate
    assert "rule_drift" not in ready["required_checks"]
    assert next(c for c in ready["suggested_checks"] if c["key"] == "rule_drift")["done"]
    assert ready["checks_complete"] is False


def test_severity_scale_is_explained_where_findings_are_judged(client, session):
    """The user accepting or rejecting is the one calibration check that matters;
    they need to know what the agent's label was supposed to mean."""
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert "wrong data is already being used" in page.lower()


def test_closed_sessions_list_shows_the_outcome(client, session):
    from grayson.core.run import run_statement

    out = run_statement(session, "SELECT * FROM DB.S.URLS", executor=FakeExecutor())
    for key in engine.workflow_for(session).required_check_keys():
        engine.complete_checkpoint(session, key, [out["qid"]], "checked")
    engine.close_session(session, "user", "nothing to act on")
    page = client.get(f"/?t={TOKEN}").text
    assert "Closed sessions" in page
    assert "clean" in page


def test_session_guard_controls_update_the_snapshot(client, session):
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    # one control: the guard display IS the form while the session is open
    assert f'action="/session/{session.id}/guard' in page
    assert page.count("Guard profile") == 1 and page.count("Strict scope") == 1
    r = client.post(
        f"/session/{session.id}/guard?t={TOKEN}",
        data={"guard_profile": "strict", "strict_scope": "true"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert session.strict_scope is True
    assert session.guard_settings.auto_limit == 1000  # the strict profile, snapshotted
    ev = next(e for e in session.events(10) if e["type"] == "guard_changed")
    assert ev["actor"] == "user"
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert '<option value="true" selected>on</option>' in page  # reflects the change


def test_session_guard_controls_hidden_once_closed(client, session):
    session.set_meta("stage", "closed")
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert f'action="/session/{session.id}/guard' not in page
    assert "Guard profile" in page and "Strict scope" in page  # still displayed, read-only
    r = client.post(f"/session/{session.id}/guard?t={TOKEN}", data={"guard_profile": "strict"})
    assert r.status_code == 400  # the snapshot is part of the record now


def test_close_button_gates_still_decide(client, session):
    # unready: the close is refused and the reason shown in place
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert "Close this session" in page
    r = client.post(f"/session/{session.id}/close?t={TOKEN}", data={"note": ""})
    assert r.status_code == 400
    assert session.stage != "closed"
    # cleared checks, nothing found: the same button closes (clean outcome)
    _clear_checks(session)
    r = client.post(
        f"/session/{session.id}/close?t={TOKEN}",
        data={"note": "looked sound"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert session.stage == "closed" and session.outcome == "clean"


def test_settings_per_workflow_defaults_roundtrip(client, workspace):
    page = client.get(f"/settings?t={TOKEN}").text
    assert "Per-workflow session defaults" in page
    r = client.post(
        f"/settings/workflow/table-onboarding?t={TOKEN}",
        data={"guard_profile": "strict", "strict_scope": "true"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    workspace.reload_config()
    wd = workspace.config.workflow_defaults["table-onboarding"]
    assert wd.guard_profile == "strict" and wd.strict_scope is True
    # inherit clears back to the normal resolution
    r = client.post(
        f"/settings/workflow/table-onboarding?t={TOKEN}",
        data={"guard_profile": "", "strict_scope": ""},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    workspace.reload_config()
    assert "table-onboarding" not in workspace.config.workflow_defaults
    # an unknown workflow is refused
    r = client.post(f"/settings/workflow/nope?t={TOKEN}", data={})
    assert r.status_code == 400


def test_knowledge_page_confirm_and_add_fact(client, workspace):
    from grayson.knowledge import KnowledgeStore

    ks = KnowledgeStore(workspace.knowledge_dir)
    added = ks.add_fact("DB.S.T1", "amounts are gross", status="proposed")
    page = client.get(f"/knowledge/DB.S.T1?t={TOKEN}").text
    assert "Confirm" in page
    r = client.post(
        f"/knowledge/DB.S.T1/fact/{added['id']}/confirm?t={TOKEN}", follow_redirects=False
    )
    assert r.status_code in (302, 303)
    assert ks.fact("DB.S.T1", added["id"])["status"] == "user_confirmed"
    # a human writing a fact directly lands user-confirmed (add + confirm, one action)
    r = client.post(
        f"/knowledge/DB.S.T1/fact?t={TOKEN}",
        data={"fact": "loads land by 06:00 UTC"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    facts = ks.read("DB.S.T1")["facts"]
    written = next(f for f in facts if f["fact"] == "loads land by 06:00 UTC")
    assert written["status"] == "user_confirmed"


def test_knowledge_page_answer_open_question(client, workspace):
    from grayson.knowledge import KnowledgeStore

    ks = KnowledgeStore(workspace.knowledge_dir)
    ks.set_profile("DB.S.T1", {"open_questions": ["What is the grain?"]})
    page = client.get(f"/knowledge/DB.S.T1?t={TOKEN}").text
    assert "What is the grain?" in page and "Answer" in page
    r = client.post(
        f"/knowledge/DB.S.T1/question?t={TOKEN}",
        data={"question": "What is the grain?", "answer": "one row per order"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    doc = ks.read("DB.S.T1")
    assert doc["open_questions"] == []
    fact = doc["facts"][0]
    assert fact["fact"] == "What is the grain? — one row per order"
    assert fact["status"] == "user_confirmed"  # answered by the human directly


def test_knowledge_page_descriptor_and_column_edits(client, workspace):
    from grayson.knowledge import KnowledgeStore

    ks = KnowledgeStore(workspace.knowledge_dir)
    ks.set_profile("DB.S.T1", {"columns": [{"name": "ORDER_ID", "type": "NUMBER"}]})
    r = client.post(
        f"/knowledge/DB.S.T1/column?t={TOKEN}",
        data={"name": "ORDER_ID", "description": "primary key, one per order"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    cols = ks.read("DB.S.T1")["columns"]
    assert cols[0]["description"] == "primary key, one per order"
    assert cols[0]["type"] == "NUMBER"  # untouched
    r = client.post(
        f"/knowledge/DB.S.T1/descriptor?t={TOKEN}",
        data={"grain": "one row per order", "freshness": "hourly", "owners": "data-eng, kcg"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    doc = ks.read("DB.S.T1")
    assert doc["grain"] == "one row per order"
    assert doc["owners"] == ["data-eng", "kcg"]


def _close_with_report(session, workspace):
    qid = run_statement(session, "SELECT * FROM DB.S.URLS", executor=FakeExecutor())["qid"]
    for key in engine.workflow_for(session).required_check_keys():
        engine.complete_checkpoint(session, key, [qid], "done")
    engine.close_session(
        session, actor="user", note="looked sound", overrides_dir=workspace.workflows_dir
    )


def test_report_record_renders_locally_and_from_the_library(client, session, workspace, tmp_path):
    # Regression: a session's published report sat in the records list labelled
    # "proposal" and 500ed on click — the record page had no report branch.
    import shutil

    from fastapi.testclient import TestClient

    from grayson.library import set_library_config
    from grayson.workspace import Workspace

    _close_with_report(session, workspace)
    listing = client.get(f"/records?t={TOKEN}").text
    assert "session report" in listing and "closed clean" in listing
    page = client.get(f"/records/{session.id}/report/report?t={TOKEN}")
    assert page.status_code == 200
    assert "Session report" in page.text and "looked sound" in page.text
    assert "closed clean" in page.text

    # a teammate's workspace: same library records, no local session
    other = tmp_path / "other"
    other.mkdir()
    (other / "grayson.toml").write_text(
        (workspace.root / "grayson.toml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copytree(workspace.records_dir, lib / "records")
    set_library_config(other, lib, False)
    other_client = TestClient(build_app(Workspace(other), token=TOKEN), base_url="http://127.0.0.1")
    page = other_client.get(f"/records/{session.id}/report/report?t={TOKEN}")
    assert page.status_code == 200
    assert "from a teammate" in page.text and "Session report" in page.text


def test_record_page_shows_the_cited_queries(client, session, workspace):
    from grayson.records import get_record

    qid = run_statement(session, "SELECT * FROM DB.S.URLS", executor=FakeExecutor())["qid"]
    f = engine.record_finding(
        session,
        {
            "title": "Bad category mapping",
            "severity": "medium",
            "confidence": "high",
            "affected_objects": ["DB.S.URLS"],
            "reproduction": "re-run the cited query",
            "summary": "Several URLs map to two categories at once.",
            "evidence": [qid],
            "extra": {
                "finding_kind": "rule_defect",
                "rule_location": "categorize_url()",
                "observed_behaviour": "some URLs carry two categories",
                "expected_behaviour": "exactly one category per URL",
            },
        },
    )
    page = client.get(f"/records/{session.id}/finding/{f['fid']}?t={TOKEN}")
    assert page.status_code == 200
    assert f"/session/{session.id}/query/{qid}" in page.text  # linked, not a bare number
    assert "SELECT * FROM DB.S.URLS" in page.text  # the statement itself, one fold away
    assert get_record(workspace, session.id, "finding", f["fid"])["evidence_queries"]


def test_chart_page_and_svg_download(client, session):
    from grayson.charts import add_chart

    rows = [{"DAY": f"2026-01-0{i}", "NULL_RATE": i / 10} for i in range(1, 6)]
    qid = run_statement(
        session, "SELECT DAY, NULL_RATE FROM DB.S.URLS", executor=FakeExecutor(rows=rows)
    )["qid"]
    spec = add_chart(session, qid, "line", "DAY", ["NULL_RATE"], "NULL rate by day")
    # the session page: tiles are lightbox-able and link to the full page
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert 'id="chart-lightbox"' in page
    assert f'data-chart="{spec["chart_id"]}"' in page
    assert "http-equiv" not in page  # refresh is script-driven now (pauses while typing)
    # the full page: chart, data, and the query behind it
    detail = client.get(f"/session/{session.id}/chart/{spec['chart_id']}?t={TOKEN}")
    assert detail.status_code == 200
    assert "NULL rate by day" in detail.text and "Plotted data (5 rows)" in detail.text
    assert "DAY, NULL_RATE" in detail.text and 'class="sql-k"' in detail.text  # highlighted
    assert f"/session/{session.id}/query/{qid}" in detail.text
    # the file
    svg = client.get(f"/session/{session.id}/chart/{spec['chart_id']}.svg?t={TOKEN}")
    assert svg.status_code == 200
    assert svg.headers["content-type"].startswith("image/svg+xml")
    assert "attachment" in svg.headers["content-disposition"]
    assert svg.text.lstrip().startswith("<svg")
    assert client.get(f"/session/{session.id}/chart/c_999?t={TOKEN}").status_code == 404


def test_chart_labels_detail_and_inline_svg(client, session):
    from grayson.charts import add_chart

    rows = [{"TS": f"2026-08-{d:02d}T00:00:00", "NULL_RATE": d / 100} for d in range(1, 31)]
    qid = run_statement(
        session, "SELECT TS, NULL_RATE FROM DB.S.URLS", executor=FakeExecutor(rows=rows)
    )["qid"]
    spec = add_chart(session, qid, "line", "TS", ["NULL_RATE"], "NULL rate by day")
    # the tile: the shared date/time parts are captioned once; the console
    # carries the tooltip script for anything still shortened
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert 'data-shared="2026-08-…T00:00:00"' in page and "viz-tip" in page
    # the chart page renders the detail size, and its download matches it
    detail = client.get(f"/session/{session.id}/chart/{spec['chart_id']}?t={TOKEN}").text
    assert 'viewBox="0 0 1000 440"' in detail
    assert f"chart/{spec['chart_id']}.svg?detail=1" in detail
    dl = client.get(f"/session/{session.id}/chart/{spec['chart_id']}.svg?detail=1&t={TOKEN}")
    assert 'viewBox="0 0 1000 464"' in dl.text  # detail canvas plus the export footer
    # the lightbox's inline markup: unbranded, not a download, both sizes
    inline = client.get(f"/session/{session.id}/chart/{spec['chart_id']}/svg?detail=1&t={TOKEN}")
    assert inline.status_code == 200
    assert inline.headers["content-type"].startswith("image/svg+xml")
    assert "content-disposition" not in inline.headers
    assert 'viewBox="0 0 1000 440"' in inline.text and "gray</tspan>" not in inline.text
    tile = client.get(f"/session/{session.id}/chart/{spec['chart_id']}/svg?t={TOKEN}").text
    assert tile.startswith('<svg viewBox="0 0 640 308"')
    assert client.get(f"/session/{session.id}/chart/c_999/svg?t={TOKEN}").status_code == 404
    # a label the detail size still shortens carries its full text, and the
    # page says how to see it
    fq = [{"T": n, "N": i} for i, n in enumerate(["ANALYTICS.WEB.PAGE_EVENTS", "RAW.X.REFUNDS"])]
    q2 = run_statement(session, "SELECT T, N FROM DB.S.URLS", executor=FakeExecutor(rows=fq))
    spec2 = add_chart(session, q2["qid"], "bar", "T", ["N"], "rows by table")
    page2 = client.get(f"/session/{session.id}/chart/{spec2['chart_id']}?t={TOKEN}").text
    assert 'data-full="ANALYTICS.WEB.PAGE_EVENTS"' in page2 and "…WEB.PAGE_EVENTS" in page2
    assert "Some axis labels are shortened" in page2
    assert "Some axis labels are shortened" not in detail  # nothing shortened there


# -- endings and removal from the console -------------------------------------


def _bug_hunter_with_accepted_finding(workspace):
    """A bug-hunter session with one accepted finding — published to the
    library at acceptance, as the configured user."""
    s = Session.create(
        workspace,
        workflow="bug-hunter",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0, timeout_seconds=0, budget_warn=0, budget_cap=0),
        guard_profile="moderate",
        title="dupes",
    )
    engine.seed_from_workflow(s)
    qid = run_statement(s, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    f = engine.record_finding(
        s,
        {
            "title": "Dup rows",
            "severity": "high",
            "confidence": "high",
            "affected_objects": ["DB.S.T1"],
            "reproduction": "re-run the cited query",
            "summary": "Duplicate rows appear in the output table.",
            "evidence": [qid],
            "extra": {
                "resolution": "root_caused",
                "root_cause": "join fan-out",
                "blast_radius": "1000 rows",
                "alternatives_tested": "two ruled out",
            },
        },
    )
    s.accept_finding(f["fid"])
    return s, f["fid"]


def _link_plain_library(workspace, tmp_path, admins=None):
    """A linked library directory that is not a git repo: the author/admin
    rules apply, and removals just change files."""
    from grayson.library import init_library, set_library_config

    lib = init_library(tmp_path / "team-lib", admins or [])
    set_library_config(workspace.root, lib, False)
    workspace.reload_config()
    return lib


def test_abandon_from_the_console(client, session, workspace):
    iid = session.add_intervention("choose", "grain?", "pick", {"options": ["a", "b"]})
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert "Abandon this session" in page and "Delete this session" in page
    refused = client.post(f"/session/{session.id}/abandon?t={TOKEN}", data={"reason": " "})
    assert refused.status_code == 400 and "needs a reason" in refused.text
    done = client.post(
        f"/session/{session.id}/abandon?t={TOKEN}",
        data={"reason": "wrong table"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    page = client.get(f"/session/{session.id}?t={TOKEN}").text
    assert "abandoned" in page and "Closed without a result" in page
    assert "Abandon this session" not in page and "Delete this session" in page  # still deletable
    assert session.intervention(iid)["status"] == "cancelled"
    dash = client.get(f"/?t={TOKEN}").text
    assert 'title="wrong table">abandoned' in dash
    assert not (workspace.records_dir / session.id).exists()  # nothing published


def test_delete_from_the_console(client, session, workspace):
    r = client.post(f"/session/{session.id}/delete?t={TOKEN}", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?")
    assert client.get(f"/session/{session.id}?t={TOKEN}").status_code == 404
    assert session.id not in workspace.list_session_ids()


def test_delete_with_published_records_is_the_authors_call(client, workspace, tmp_path):
    from grayson.identity import set_user_id

    _link_plain_library(workspace, tmp_path)
    set_user_id("kcg")
    s, fid = _bug_hunter_with_accepted_finding(workspace)
    folder = workspace.records_dir / s.id
    assert (folder / f"{fid}.json").is_file()
    page = client.get(f"/session/{s.id}?t={TOKEN}").text
    assert "published <b>1</b> record" in page and "as author (kcg)" in page
    assert 'name="library" value="true" >' in page  # the box is live
    # a teammate sees why the records stay, and cannot take them
    set_user_id("bob")
    page = client.get(f"/session/{s.id}?t={TOKEN}").text
    assert "they stay: published by kcg, not you (bob)" in page
    assert 'name="library" value="true" disabled' in page
    refused = client.post(
        f"/session/{s.id}/delete?t={TOKEN}", data={"library": "true", "reason": "tidy"}
    )
    assert refused.status_code == 400 and "nothing deleted" in refused.text
    assert folder.is_dir() and s.id in workspace.list_session_ids()
    # the author takes both in one go
    set_user_id("kcg")
    done = client.post(
        f"/session/{s.id}/delete?t={TOKEN}",
        data={"library": "true", "reason": "restarted"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert not folder.exists() and s.id not in workspace.list_session_ids()


def test_record_page_offers_removal_to_author_or_admin(client, workspace, tmp_path):
    from grayson.identity import set_user_id

    _link_plain_library(workspace, tmp_path, admins=["boss"])
    set_user_id("kcg")
    s, fid = _bug_hunter_with_accepted_finding(workspace)
    url = f"/records/{s.id}/finding/{fid}?t={TOKEN}"
    page = client.get(url).text
    assert "Remove from the library" in page and "Remove records (as author (kcg))" in page
    assert "The session itself stays in this workspace" in page
    set_user_id("bob")
    page = client.get(url).text
    assert "Not yours to remove: published by kcg, not you (bob)" in page
    assert "Remove records (as" not in page
    refused = client.post(f"/records/{s.id}/delete?t={TOKEN}", data={"reason": "x"})
    assert refused.status_code == 400 and "published by kcg, not you (bob)" in refused.text
    assert (workspace.records_dir / s.id).is_dir()
    set_user_id("boss")
    page = client.get(url).text
    assert "Remove records (as library admin)" in page
    done = client.post(
        f"/records/{s.id}/delete?t={TOKEN}", data={"reason": "cleanup"}, follow_redirects=False
    )
    assert done.status_code == 303 and done.headers["location"].startswith("/records")
    assert not (workspace.records_dir / s.id).exists()
    assert s.id in workspace.list_session_ids()  # the session itself stays
    assert client.post(f"/records/bad:id/delete?t={TOKEN}", data={}).status_code == 404


def test_settings_page_shows_admins_read_only(client, workspace, tmp_path):
    assert "library admins add" not in client.get(f"/settings?t={TOKEN}").text  # solo: no team
    _link_plain_library(workspace, tmp_path)
    page = client.get(f"/settings?t={TOKEN}").text
    assert "none set" in page and "library admins add" in page
    from grayson.library import write_library_settings

    write_library_settings(tmp_path / "team-lib", {"admins": ["kcg", "bob"]})
    page = client.get(f"/settings?t={TOKEN}").text
    assert "kcg, bob" in page
