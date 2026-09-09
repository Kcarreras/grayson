"""Execute real candidate/probe SQL over planted defects, not canned verdicts."""

import copy
import sqlite3

import pytest

from grayson.config import GuardSettings
from grayson.core import engine as checkpoints
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.projects import engine
from grayson.projects.models import Candidate, Contract
from grayson.projects.sql import compile_candidate
from grayson.sandbox.executor import SandboxExecutor


def contract(kind="pipeline", approval="bounded"):
    return {
        "goal": "Enrich every order with its current customer region, preserving revenue",
        "deliverable": "Verified order-grain SQL and deployment package",
        "kind": kind,
        "deployment_target": "DB.S.ENRICHED",
        "scope": ["DB.S.ORDERS", "DB.S.CUSTOMERS"],
        "semantics": {
            "grain": "One row per order ID",
            "customer": "Only ACTIVE=1 is current",
            "amount": "Gross order amount, including refunds; never infer net revenue",
        },
        "data_window": "All seeded orders, fixed input snapshot for this test",
        "semantic_checks": {"grain": ["grain"], "customer": ["region"], "amount": ["revenue"]},
        "policy": {"kind": kind, "approval": approval, "max_queries": 150},
        "joins": [
            {
                "id": "enriched",
                "left": "orders",
                "right": "customers",
                "left_keys": ["CUSTOMER_ID"],
                "right_keys": ["CUSTOMER_ID"],
                "semantics": "An order belongs to exactly one current customer",
            }
        ],
        "checks": [
            {
                "id": "region",
                "name": "Current customer region",
                "kind": "values",
                "keys": ["ID"],
                "column": "REGION",
                "rationale": "Attach the current region, not an arbitrary version",
                "baseline_sql": "SELECT o.ID, c.REGION FROM DB.S.ORDERS o "
                "LEFT JOIN DB.S.CUSTOMERS c "
                "ON o.CUSTOMER_ID=c.CUSTOMER_ID AND c.ACTIVE=1",
            },
            {
                "id": "grain",
                "name": "Order grain",
                "kind": "grain",
                "keys": ["ID"],
                "rationale": "Do not duplicate orders",
            },
            {
                "id": "population",
                "name": "All orders survive",
                "kind": "population",
                "keys": ["ID"],
                "baseline_sql": "SELECT ID FROM DB.S.ORDERS",
                "rationale": "Never drop an order",
            },
            {
                "id": "revenue",
                "name": "Per-order revenue preserved",
                "kind": "measure",
                "column": "AMOUNT",
                "group_by": ["ID"],
                "baseline_sql": "SELECT ID, AMOUNT FROM DB.S.ORDERS",
                "rationale": "Prevent sums from hiding compensating per-order errors",
            },
        ],
    }


def candidate(fixed=False):
    return {
        "summary": "Current customer region for each order",
        "output": "enriched",
        "diagnosis": "Filter customer versions using the approved ACTIVE definition"
        if fixed
        else "Initial candidate before source multiplicity tests",
        "addressed_checks": ["join.enriched.right_multiplicity", "grain.unique", "revenue"],
        "nodes": [
            {
                "id": "orders",
                "sql": "SELECT ID, CUSTOMER_ID, AMOUNT FROM DB.S.ORDERS",
                "purpose": "Preserve the complete order population",
            },
            {
                "id": "customers",
                "sql": "SELECT CUSTOMER_ID, REGION FROM DB.S.CUSTOMERS"
                + (" WHERE ACTIVE = 1" if fixed else ""),
                "purpose": "Current customer region",
            },
            {
                "id": "enriched",
                "kind": "join",
                "purpose": "Attach region without fanout",
                "columns": {
                    "ID": "l.ID",
                    "CUSTOMER_ID": "l.CUSTOMER_ID",
                    "AMOUNT": "l.AMOUNT",
                    "REGION": "r.REGION",
                },
            },
        ],
    }


@pytest.fixture
def project(workspace, tmp_path):
    s = Session.create(
        workspace,
        workflow="pipeline-development",
        strict_scope=True,
        targets=["DB.S.ORDERS", "DB.S.CUSTOMERS"],
        guard=GuardSettings(auto_limit=10000),
        guard_profile="moderate",
    )
    checkpoints.seed_from_workflow(s)
    path = tmp_path / "warehouse.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE "DB.S.ORDERS" (ID INT, CUSTOMER_ID INT, AMOUNT REAL);
        INSERT INTO "DB.S.ORDERS" VALUES (1,10,100), (2,20,200), (3,10,-10);
        CREATE TABLE "DB.S.CUSTOMERS" (CUSTOMER_ID INT, REGION TEXT, ACTIVE INT);
        INSERT INTO "DB.S.CUSTOMERS" VALUES (10,'old',0), (10,'north',1), (20,'south',1);
    """)
    con.close()
    return s, SandboxExecutor(path)


def approve(s, spec=None):
    view = engine.draft(s, spec or contract())
    p = view["project"]
    return engine.approve(s, p["revision"], p["contract_digest"])


def submit(s, spec=None):
    return engine.submit_candidate(s, spec or candidate(), engine.state(s)["revision"])


def verify(s, executor):
    return engine.verify(s, engine.state(s)["revision"], executor)["project"]


def review_and_checkpoints(s):
    p = engine.state(s)
    evidence = [r["qid"] for r in p["verification"]["results"]]
    engine.record_review(
        s,
        {
            "summary": "Definitions and preservation checks agree",
            "answers": ["Verified using the approved definitions and cited probes"] * 4,
            "evidence": evidence,
            "issues": [],
            "limitations": ["Scheduling is not tested"],
        },
        p["revision"],
    )
    for key in ("source_understood", "candidate_verified", "interpretation_reviewed"):
        checkpoints.complete_checkpoint(s, key, evidence)


def test_detects_and_repairs_fanout(project):
    s, executor = project
    approve(s)
    submit(s)
    bad = verify(s, executor)
    failed = {r["id"] for r in bad["verification"]["results"] if r["status"] == "fail"}
    assert {"join.enriched.right_multiplicity", "grain.unique", "revenue"} <= failed
    assert bad["phase"] == "needs_revision"
    submit(s, candidate(True))
    good = verify(s, executor)
    assert good["verification"]["verdict"] == "pass", good["verification"]
    review_and_checkpoints(s)
    done = engine.finish(s, engine.state(s)["revision"])["project"]
    assert done["phase"] == "ready_for_deployment"
    assert done["completion"]["by"] == "agent"
    package = engine.deployment_package(s, done["revision"])["project"]
    p = s.proposal(package["deployment"]["pid"])
    assert p["status"] == "proposed"
    assert p["payload"]["ddl"].startswith("CREATE VIEW DB.S.ENRICHED AS")
    assert "OR REPLACE" not in p["payload"]["ddl"]


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("drop", "population.missing"),
        ("collapse", "revenue"),
        ("compensate", "revenue"),
    ],
)
def test_catches_population_and_value_loss(project, mutation, expected):
    s, executor = project
    approve(s)
    c = candidate(True)
    if mutation == "drop":
        c["nodes"][0]["sql"] += " WHERE ID <> 3"
    elif mutation == "collapse":
        c["nodes"][2]["columns"]["AMOUNT"] = "0"
    else:
        c["nodes"][2]["columns"]["AMOUNT"] = (
            "l.AMOUNT + CASE WHEN l.ID=1 THEN 10 WHEN l.ID=2 THEN -10 ELSE 0 END"
        )
    submit(s, c)
    p = verify(s, executor)
    assert next(r for r in p["verification"]["results"] if r["id"] == expected)["status"] == "fail"


def test_sql_error_is_unproven_and_repairable(project):
    s, executor = project
    approve(s)
    c = candidate(True)
    c["nodes"][2]["columns"]["AMOUNT"] = "l.MISSPELLED"
    submit(s, c)
    p = verify(s, executor)
    assert p["verification"]["verdict"] == "unproven"
    assert any("MISSPELLED" in r["details"] for r in p["verification"]["results"])
    submit(s, candidate(True))
    assert verify(s, executor)["verification"]["verdict"] == "pass"


def test_approval_revision_and_scope_boundaries(project):
    s, executor = project
    p = engine.draft(s, contract())["project"]
    assert run_statement(s, "SELECT * FROM DB.S.ORDERS", executor=executor)["status"] == "rejected"
    with pytest.raises(ValueError, match="human"):
        engine.approve(s, p["revision"], p["contract_digest"], "agent")
    with pytest.raises(ValueError, match="changed"):
        engine.approve(s, p["revision"], "old-digest")
    engine.approve(s, p["revision"], p["contract_digest"])
    with pytest.raises(ValueError, match="changed"):
        engine.submit_candidate(s, candidate(), p["revision"])
    s.widen_scope(["DB.S.OTHER"])
    assert run_statement(s, "SELECT * FROM DB.S.ORDERS", executor=executor)["status"] == "rejected"


def test_guided_and_milestone_approvals(project):
    s, executor = project
    approve(s, contract(approval="guided"))
    p = submit(s, candidate(True))["project"]
    with pytest.raises(ValueError, match="candidate approval"):
        verify(s, executor)
    engine.approve_candidate(s, p["revision"], p["candidate_digest"])
    assert verify(s, executor)["verification"]["verdict"] == "pass"
    review_and_checkpoints(s)
    with pytest.raises(ValueError, match="human"):
        engine.finish(s, engine.state(s)["revision"])
    assert (
        engine.finish(s, engine.state(s)["revision"], "user")["project"]["phase"]
        == "ready_for_deployment"
    )


def test_candidate_change_invalidates_checks_and_review(project):
    s, executor = project
    approve(s)
    submit(s, candidate(True))
    verify(s, executor)
    review_and_checkpoints(s)
    c = candidate(True)
    c["nodes"][2]["columns"]["AMOUNT"] = "l.AMOUNT * 2"
    p = submit(s, c)["project"]
    assert p["verification"] is None and p["review"] is None
    assert checkpoints.readiness(s)["open_checks"]
    with pytest.raises(ValueError, match="fresh"):
        engine.finish(s, p["revision"])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM DB.S.ORDERS o JOIN DB.S.CUSTOMERS c ON o.CUSTOMER_ID=c.CUSTOMER_ID",
        "SELECT * FROM (SELECT * FROM DB.S.ORDERS)",
        "SELECT * FROM DB.S.ORDERS, DB.S.CUSTOMERS",
        "SELECT * FROM DB.S.ORDERS LIMIT 1",
        "SELECT * FROM DB.S.ORDERS UNION ALL SELECT * FROM DB.S.ORDERS",
        "SELECT * FROM DB.S.SECRET",
        "DELETE FROM DB.S.ORDERS",
    ],
)
def test_hidden_joins_scope_and_partial_coverage_rejected(sql):
    c = candidate(True)
    c["nodes"][0]["sql"] = sql
    with pytest.raises(ValueError):
        compile_candidate(Candidate.model_validate(c), Contract.model_validate(contract()))


def test_budgets_and_pause_apply_to_common_query_path(project):
    s, executor = project
    spec = contract()
    spec["policy"]["max_queries"] = 1
    approve(s, spec)
    assert run_statement(s, "SELECT * FROM DB.S.ORDERS", executor=executor)["status"] == "executed"
    assert run_statement(s, "SELECT * FROM DB.S.ORDERS", executor=executor)["status"] == "rejected"
    engine.control(s, "pause", "inspect business meaning", engine.state(s)["revision"], "agent")
    with pytest.raises(ValueError, match="human"):
        engine.control(s, "resume", "continue", engine.state(s)["revision"], "agent")


def test_workflow_snapshot_survives_library_change(project):
    s, _ = project
    assert s.get_meta("workflow_snapshot_v1")
    assert checkpoints.workflow_for(s).project.kind == "pipeline"


def test_unknown_contract_fields_fail_closed():
    spec = copy.deepcopy(contract())
    spec["allow_warehouse_writes"] = True
    with pytest.raises(ValueError):
        Contract.model_validate(spec)


def test_wrong_semantic_value_fails_even_with_correct_totals(project):
    s, executor = project
    approve(s)
    c = candidate(True)
    c["nodes"][2]["columns"]["REGION"] = "'unknown'"
    submit(s, c)
    p = verify(s, executor)
    by_id = {r["id"]: r for r in p["verification"]["results"]}
    assert by_id["grain.unique"]["status"] == "pass"
    assert by_id["revenue"]["status"] == "pass"
    assert by_id["region"]["status"] == "fail"


@pytest.mark.parametrize("relation", ["customers", "candidate"])
def test_value_checks_reject_baseline_only_keys(project, relation, tmp_path):
    s, executor = project
    spec = contract()
    values = spec["checks"][0]
    values.update(
        relation=relation,
        keys=["CUSTOMER_ID"],
        baseline_sql="SELECT CUSTOMER_ID, REGION FROM DB.S.CUSTOMERS WHERE ACTIVE=1",
    )
    c = candidate(True)
    if relation == "customers":
        c["nodes"][1]["sql"] += " AND 1=0"
        # The approved join allows missing enrichment, but its independent
        # semantic check must still reject an empty intermediate relation.
        spec["joins"][0]["max_unmatched_left"] = 3
    else:
        # Output IDs and totals are intact; the semantic baseline uses a
        # different key and expects a customer absent from the output.
        with sqlite3.connect(tmp_path / "warehouse.db") as con:
            con.execute("INSERT INTO \"DB.S.CUSTOMERS\" VALUES (30, 'west', 1)")
    approve(s, spec)
    submit(s, c)
    p = verify(s, executor)
    by_id = {r["id"]: r for r in p["verification"]["results"]}
    assert by_id["population.missing"]["status"] == "pass"
    assert by_id["revenue"]["status"] == "pass"
    assert p["verification"]["verdict"] == "fail"
    assert by_id["region.missing"]["status"] == "fail"
    with pytest.raises(ValueError, match="passing verification"):
        review_and_checkpoints(s)


def test_unchanged_candidate_reruns_exhaust_stalled_budget(project):
    s, executor = project
    spec = contract()
    spec["policy"]["max_stalled_iterations"] = 2
    approve(s, spec)
    submit(s)
    initial = verify(s, executor)
    assert initial["verification"]["verdict"] == "fail"
    assert initial["stalled"] == 0  # Some checks passed on the first attempt.
    assert verify(s, executor)["stalled"] == 1
    final = verify(s, executor)
    assert final["stalled"] == 2
    assert final["phase"] == "blocked"
    assert "no verification progress" in final["block_reason"]


def test_deployment_checks_actual_target_and_preserves_source_scope(project):
    from grayson.core import proposals
    from grayson.projects.models import Candidate, Contract
    from grayson.projects.sql import compile_candidate

    s, executor = project
    approve(s)
    submit(s, candidate(True))
    verify(s, executor)
    review_and_checkpoints(s)
    engine.finish(s, engine.state(s)["revision"])
    package = engine.deployment_package(s, engine.state(s)["revision"])["project"]
    pid = package["deployment"]["pid"]
    with pytest.raises(ValueError, match="human"):
        proposals.decide(s, pid, True, actor="agent")
    proposals.decide(s, pid, True, actor="user")
    with pytest.raises(ValueError, match="reported applied"):
        engine.deployment_check(s, package["revision"], executor)
    # Simulate human execution, creating an intentionally wrong deployed table.
    con = sqlite3.connect(executor.db_path)
    con.executescript(
        'CREATE TABLE "DB.S.ENRICHED" '
        "(ID INT, CUSTOMER_ID INT, AMOUNT REAL, REGION TEXT);"
        "INSERT INTO \"DB.S.ENRICHED\" VALUES (1,10,999,'north');"
    )
    con.close()
    proposals.mark_applied(s, pid)
    scope = s.scope_tables.copy()
    result = engine.deployment_check(s, package["revision"], executor)["project"]
    assert result["deployed_verification"]["verdict"] == "fail"
    assert s.scope_tables == scope
    assert all(r["session_id"] != s.id for r in result["deployed_verification"]["results"])
    assert engine.status(s)["remaining"]["queries"] < 150 - s.budget_consumed_count()
    # The simulated human corrects the deployed table using the approved SELECT.
    sql = compile_candidate(
        Candidate.model_validate(candidate(True)), Contract.model_validate(contract())
    )["sql"]
    rows = executor.execute(sql).rows
    con = sqlite3.connect(executor.db_path)
    con.execute('DELETE FROM "DB.S.ENRICHED"')
    con.executemany(
        'INSERT INTO "DB.S.ENRICHED" VALUES (?,?,?,?)',
        [(r["ID"], r["CUSTOMER_ID"], r["AMOUNT"], r["REGION"]) for r in rows],
    )
    con.commit()
    con.close()
    result = engine.deployment_check(s, result["revision"], executor)["project"]
    assert result["deployed_verification"]["verdict"] == "pass"
    engine.accept_deployment(s, result["revision"])
    assert s.stage == "closed" and s.outcome == "project_verified"


def test_supervisor_repairs_and_stops_at_deployment_boundary(project):
    from grayson.projects.runner import drive

    s, executor = project
    approve(s)
    seen_phases = []

    def provider(context):
        brief = context["brief"]
        p = brief["project"]["project"]
        seen_phases.append(p["phase"])
        if p["phase"] == "building":
            return {"action": "candidate", "payload": candidate()}
        if p["phase"] == "needs_revision":
            assert any(r["status"] == "fail" for r in p["verification"]["results"])
            return {"action": "candidate", "payload": candidate(True)}
        if p["phase"] == "needs_review":
            return {
                "action": "review",
                "payload": {
                    "summary": "Current-customer selection resolves the measured fanout",
                    "answers": ["Approved definitions checked with independent evidence"] * 4,
                    "evidence": [p["verification"]["results"][0]["qid"]],
                    "limitations": ["Deployment and scheduling are not yet tested"],
                    "issues": [],
                },
            }
        if p["phase"] == "ready_for_review":
            pending = brief["readiness"]["open_checks"]
            if pending:
                return {
                    "action": "checkpoint",
                    "payload": {
                        "key": pending[0],
                        "evidence": [p["verification"]["results"][0]["qid"]],
                    },
                }
            return {"action": "finish"}
        return {"action": "deployment"}

    result = drive(s, provider, executor=executor)["project"]
    assert result["phase"] == "awaiting_deployment"
    assert "needs_revision" in seen_phases
    assert result["iterations"] == 2 and not result["runner"]
    assert s.proposal(result["deployment"]["pid"])["status"] == "proposed"


def test_pause_cancels_inflight_verification(project):
    s, executor = project
    approve(s)
    submit(s, candidate(True))

    class PausingExecutor:
        def execute(self, sql, timeout_seconds=0):
            engine.control(s, "pause", "Need to inspect source", engine.state(s)["revision"])
            return executor.execute(sql, timeout_seconds)

    result = verify(s, PausingExecutor())
    assert result["phase"] == "paused" and result["verification"] is None


def test_stale_evidence_cannot_complete(project, monkeypatch):
    s, executor = project
    approve(s)
    submit(s, candidate(True))
    verify(s, executor)
    review_and_checkpoints(s)
    now = engine.time.time()
    monkeypatch.setattr(engine.time, "time", lambda: now + 3700)
    with pytest.raises(ValueError, match="fresh"):
        engine.finish(s, engine.state(s)["revision"])


def test_semantics_must_have_checks_or_human_review():
    spec = contract()
    spec["semantic_checks"].pop("customer")
    with pytest.raises(ValueError, match="every semantic"):
        Contract.model_validate(spec)
    spec["human_semantic_review"] = ["customer"]
    assert Contract.model_validate(spec).human_semantic_review == ["customer"]


def test_project_console_and_human_approval(project):
    from fastapi.testclient import TestClient

    from grayson.ui.server import build_app

    s, _ = project
    client = TestClient(build_app(s.workspace, token="test"), base_url="http://127.0.0.1")
    p = engine.draft(s, contract())["project"]
    page = client.get(f"/session/{s.id}/project?t=test")
    assert page.status_code == 200 and "Review brief" in page.text
    assert page.url.path == f"/session/{s.id}"
    brief = client.get(f"/session/{s.id}?t=test&view=brief")
    assert "Approve this brief" in brief.text and "Current customer region" in brief.text
    assert client.get(f"/session/{s.id}/project").status_code == 403
    response = client.post(
        f"/session/{s.id}/project/approve?t=test",
        data={"revision": p["revision"], "digest": p["contract_digest"]},
    )
    assert response.status_code == 200 and engine.state(s)["phase"] == "building"
    assert (
        client.post(
            f"/session/{s.id}/project/approve?t=test",
            data={"revision": p["revision"], "digest": p["contract_digest"]},
        ).status_code
        == 400
    )
    page = client.get(f"/session/{s.id}?t=test")
    assert "Project views" in page.text and "Open project workspace" not in page.text
    assert 'id="findings"' not in page.text and 'id="fix-delivery"' not in page.text


def test_project_session_views_and_deployment_stay_in_one_workspace(project):
    from fastapi.testclient import TestClient

    from grayson.core.file_fixes import review_digest
    from grayson.ui.server import build_app

    s, executor = project
    approve(s)
    submit(s)
    verify(s, executor)
    submit(s, candidate(True))
    verify(s, executor)
    review_and_checkpoints(s)
    engine.finish(s, engine.state(s)["revision"])
    engine.deployment_package(s, engine.state(s)["revision"])
    client = TestClient(build_app(s.workspace, token="test"), base_url="http://127.0.0.1")
    for section in ("build", "checks", "brief", "queries", "history"):
        page = client.get(f"/session/{s.id}?t=test&view={section}")
        assert page.status_code == 200
        assert 'aria-label="Project views"' in page.text
        assert 'id="findings"' not in page.text
        assert "Open project workspace" not in page.text
    build = client.get(f"/session/{s.id}?t=test").text
    assert 'class="pipeline-edge"' in build and 'href="#node-customers"' in build
    assert "Approve deployment SQL" in build and "SQL to copy and run" in build
    assert "Workflow checkpoints" not in build and "Current customer region: baseline" not in build
    history = client.get(f"/session/{s.id}?t=test&view=history").text
    assert "SQL changes" in history and "right multiplicity" in history
    assert "ACTIVE" in history
    queries = client.get(f"/session/{s.id}?t=test&view=queries").text
    assert 'data-list="queries"' in queries and 'id="deployment"' not in queries
    qid = engine.state(s)["verification"]["results"][0]["qid"]
    query = client.get(f"/session/{s.id}/query/{qid}?t=test")
    assert query.status_code == 200 and f"/session/{s.id}?t=test&amp;view=queries" in query.text
    proposal = s.proposal(engine.state(s)["deployment"]["pid"])
    failed = client.post(
        f"/session/{s.id}/proposal/{proposal['pid']}/approve?t=test", data={"digest": "stale"}
    )
    assert failed.status_code == 400 and 'aria-label="Project views"' in failed.text
    assert 'id="findings"' not in failed.text
    approved = client.post(
        f"/session/{s.id}/proposal/{proposal['pid']}/approve?t=test",
        data={"digest": review_digest(proposal)},
    )
    assert approved.status_code == 200 and "I've applied this" in approved.text
    assert approved.url.path == f"/session/{s.id}"


def test_empty_project_session_uses_project_interface(project):
    from fastapi.testclient import TestClient

    from grayson.ui.server import build_app

    s, _ = project
    client = TestClient(build_app(s.workspace, token="test"), base_url="http://127.0.0.1")
    for section in ("build", "checks", "brief", "queries", "history"):
        page = client.get(f"/session/{s.id}?t=test&view={section}")
        assert page.status_code == 200 and 'aria-label="Project views"' in page.text
        assert 'id="findings"' not in page.text
    assert client.get(f"/session/{s.id}").status_code == 403


def test_mcp_surface_has_no_approval_tools(project):
    import asyncio

    from conftest import call_mcp
    from grayson.mcp.server import build_server

    s, _ = project
    server = build_server(s.workspace)
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert {"project_schema", "project_draft", "project_candidate", "project_verify"} <= names
    assert not {"project_approve", "project_approve_candidate", "project_resume"} & names
    result = call_mcp(server, "project_draft", {"session_id": s.id, "spec": contract()})
    assert result["project"]["phase"] == "awaiting_brief"


def test_revalidation_is_finite_idempotent_and_keeps_history(project):
    from grayson.projects.reuse import revalidate

    original, executor = project
    s = Session.create(
        original.workspace,
        workflow="goal-analysis",
        strict_scope=True,
        targets=original.targets,
        guard=original.guard_settings,
        guard_profile="moderate",
    )
    checkpoints.seed_from_workflow(s)
    spec = contract(kind="analysis")
    spec["policy"]["max_revalidations"] = 1
    approve(s, spec)
    submit(s, candidate(True))
    verify(s, executor)
    review_and_checkpoints(s)
    original_result = engine.finish(s, engine.state(s)["revision"])["project"]
    replay = revalidate(s, "source-change-1", original_result["revision"], executor)["project"]
    assert replay["phase"] == "complete" and replay["revalidation_of"] == s.id
    assert engine.state(s)["verification"] == original_result["verification"]
    duplicate = revalidate(s, "source-change-1", original_result["revision"], executor)["project"]
    assert duplicate["revision"] == replay["revision"]
    with pytest.raises(ValueError, match="no preapproved"):
        revalidate(s, "source-change-2", engine.state(s)["revision"], executor)


def test_recipe_and_regression_are_proposals(project):
    from grayson.projects.reuse import propose_regression, recipe

    s, executor = project
    approve(s)
    submit(s, candidate(True))
    verify(s, executor)
    draft = recipe(s, "orders-project")
    assert not draft["saved"] and "project_example" in draft["yaml"]
    proposed = propose_regression(s, "revenue", "orders_amounts_preserved")
    assert proposed["check"]["state"] == "proposed"


def test_diagnostics_show_fanout_keys_without_passing_the_gate(project):
    s, executor = project
    approve(s)
    submit(s)
    verify(s, executor)
    examples = engine.diagnose(s, "join.enriched.right_multiplicity", executor=executor)
    assert examples["status"] == "executed"
    assert examples["preview"] == [{"CUSTOMER_ID": 10}]
    assert engine.state(s)["verification"]["verdict"] == "fail"
    with pytest.raises(ValueError, match="fresh passing"):
        checkpoints.complete_checkpoint(s, "source_understood", [examples["qid"]])


def test_approval_change_preserves_evidence_and_respects_ceiling(project):
    from grayson.config_edit import set_values

    s, executor = project
    approve(s)
    submit(s, candidate(True))
    verify(s, executor)
    before = engine.state(s)
    with pytest.raises(ValueError, match="human"):
        engine.set_approval(s, "guided", before["revision"], "agent")
    view = engine.set_approval(s, "guided", before["revision"])
    assert view["project"]["verification"] == before["verification"]
    assert view["effective_policy"]["approval"] == "guided"
    set_values(s.workspace.root, {"projects.max_approval": "milestones"})
    view = engine.set_approval(s, "bounded", view["project"]["revision"])
    assert view["effective_policy"]["approval"] == "milestones"


def test_supervisor_action_budget_survives_resume(project):
    from grayson.projects.runner import drive

    s, _ = project
    spec = contract()
    spec["policy"]["max_actions"] = 2
    approve(s, spec)

    def provider(_):
        return {"action": "plan", "payload": {"steps": []}}

    drive(s, provider, max_steps=1)
    result = drive(s, provider, max_steps=10)["project"]
    assert result["phase"] == "blocked" and result["runner_steps"] == 2


def test_lowering_approval_releases_candidate_gate(project):
    s, executor = project
    approve(s, contract(approval="guided"))
    submit(s, candidate(True))
    before = engine.state(s)
    assert before["phase"] == "candidate_review"
    view = engine.set_approval(s, "bounded", before["revision"])
    assert view["project"]["phase"] == "verifying"
    assert view["project"]["candidate_digest"] == before["candidate_digest"]
    verify(s, executor)
    assert engine.state(s)["verification"]["verdict"] == "pass"


def test_human_pause_resume_releases_abandoned_runner(project):
    import time

    from grayson.projects.runner import drive

    s, _ = project
    approve(s)

    def abandoned(v):
        v["runner"] = {"token": "crashed-process", "expires": time.time() + 3600}
        return v

    engine._mutate(s, engine.state(s)["revision"], "test_claim", abandoned)
    engine.control(s, "pause", "Recover interrupted runner", engine.state(s)["revision"])
    engine.control(s, "resume", "Restart runner", engine.state(s)["revision"])

    def provider(_):
        return {"action": "plan", "payload": {"steps": []}}

    result = drive(s, provider, max_steps=1)["project"]
    assert result["runner_steps"] == 1 and not result["runner"]


def test_assertion_cannot_hide_relation_in_string_literal():
    from grayson.projects.sql import verification_queries

    spec = contract()
    spec["checks"].append(
        {
            "id": "vacuous",
            "name": "Misleading assertion",
            "kind": "assertion",
            "sql": "SELECT '{{relation}}' WHERE FALSE",
            "rationale": "An invalid evidence binding",
        }
    )
    with pytest.raises(ValueError, match="actually read"):
        verification_queries(
            Candidate.model_validate(candidate(True)), Contract.model_validate(spec)
        )


def test_brief_inherits_pinned_workflow_defaults(project):
    import json

    s, _ = project
    snapshot = json.loads(s.get_meta("workflow_snapshot_v1"))
    snapshot["project"].update(max_queries=333, max_actions=444, max_revalidations=2)
    s.set_meta("workflow_snapshot_v1", json.dumps(snapshot))
    spec = contract()
    spec["policy"] = {"approval": "bounded"}
    p = engine.draft(s, spec)["project"]["contract"]["policy"]
    assert p["approval"] == "bounded" and p["max_queries"] == 333
    assert p["max_actions"] == 444 and p["max_revalidations"] == 2
