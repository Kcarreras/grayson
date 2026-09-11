"""Execute real candidate/probe SQL over planted defects, not canned verdicts."""

import copy
import re
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


@pytest.mark.parametrize("group_by", [[], ["CUSTOMER_ID"]])
def test_measure_detects_collapsed_intermediate_rows(project, group_by):
    s, executor = project
    spec = contract()
    spec.update(
        goal="Total gross revenue per customer, retaining each order until the final rollup",
        semantics={"grain": "One row per customer", "amount": "Retain orders until final rollup"},
        semantic_checks={"grain": ["grain"], "amount": ["revenue", "source_revenue"]},
        joins=[],
    )
    spec["checks"] = [check for check in spec["checks"] if check["id"] != "region"]
    for check in spec["checks"]:
        if check["kind"] in {"grain", "population"}:
            check["keys"] = ["CUSTOMER_ID"]
        if check["kind"] == "population":
            check["baseline_sql"] = "SELECT DISTINCT CUSTOMER_ID FROM DB.S.ORDERS"
        if check["kind"] == "measure":
            check["group_by"] = ["CUSTOMER_ID"]
            check["baseline_sql"] = (
                "SELECT CUSTOMER_ID, SUM(AMOUNT) AS AMOUNT FROM DB.S.ORDERS GROUP BY CUSTOMER_ID"
            )
    spec["checks"].append(
        {
            "id": "source_revenue",
            "name": "Orders survive until final rollup",
            "kind": "measure",
            "relation": "orders",
            "column": "AMOUNT",
            "group_by": group_by,
            "baseline_sql": "SELECT CUSTOMER_ID, AMOUNT FROM DB.S.ORDERS",
            "rationale": "Equal totals must not conceal collapsed order rows",
        }
    )
    approve(s, spec)
    c = {
        "summary": "Customer revenue rollup",
        "diagnosis": "Initial rollup before preservation checks",
        "output": "totals",
        "nodes": [
            {
                "id": "orders",
                "sql": "SELECT CUSTOMER_ID, SUM(AMOUNT) AS AMOUNT "
                "FROM DB.S.ORDERS GROUP BY CUSTOMER_ID",
                "purpose": "Retain order amounts before the final rollup",
            },
            {
                "id": "totals",
                "sql": "SELECT CUSTOMER_ID, SUM(AMOUNT) AS AMOUNT FROM orders GROUP BY CUSTOMER_ID",
                "purpose": "Final customer totals",
            },
        ],
    }
    submit(s, c)
    bad = verify(s, executor)
    assert bad["verification"]["verdict"] == "fail"
    assert {r["id"] for r in bad["verification"]["results"] if r["status"] != "pass"} == {
        "source_revenue"
    }
    assert bad["phase"] == "needs_revision"
    c["nodes"][0]["sql"] = "SELECT CUSTOMER_ID, AMOUNT FROM DB.S.ORDERS"
    c["diagnosis"] = "Preserve individual order rows until the approved final rollup"
    c["addressed_checks"] = ["source_revenue"]
    submit(s, c)
    assert verify(s, executor)["verification"]["verdict"] == "pass"


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


@pytest.mark.parametrize("prior_status", ["complete", "waived"])
def test_revised_candidate_requires_current_checkpoint_dependencies(project, prior_status):
    s, executor = project
    approve(s)
    submit(s, candidate(True))
    verify(s, executor)
    review_and_checkpoints(s)
    if prior_status == "waived":
        s.reopen_checkpoint("source_understood")
        checkpoints.waive_checkpoint(s, "source_understood", "Prior candidate exception")
    revised = candidate(True)
    revised["nodes"][2]["columns"]["AMOUNT"] = "l.AMOUNT + 0"
    submit(s, revised)
    current = verify(s, executor)
    assert current["verification"]["verdict"] == "pass"
    evidence = [r["qid"] for r in current["verification"]["results"]]
    with pytest.raises(checkpoints.EnforcementError, match="source_understood"):
        checkpoints.complete_checkpoint(s, "candidate_verified", evidence)
    checkpoints.complete_checkpoint(s, "source_understood", evidence)
    with pytest.raises(checkpoints.EnforcementError, match="candidate_verified"):
        checkpoints.complete_checkpoint(s, "interpretation_reviewed", evidence)
    checkpoints.complete_checkpoint(s, "candidate_verified", evidence)
    checkpoints.complete_checkpoint(s, "interpretation_reviewed", evidence)
    assert not checkpoints.readiness(s)["open_checks"]


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


@pytest.mark.parametrize(
    "target",
    ['DB.S."DailySales"', '"SalesDb".S.DAILY', 'DB."Sales".DAILY', "DAILY", "S.DAILY", "DB..DAILY"],
)
def test_project_rejects_unsupported_deployment_targets_before_approval(project, target):
    s, _ = project
    spec = contract()
    spec["deployment_target"] = target
    with pytest.raises(ValueError, match="unquoted"):
        engine.draft(s, spec)
    assert engine.state(s) is None
    assert not s.proposals()


@pytest.mark.parametrize("source", ['DB.S."DailySales"', '"SalesDb".S.ORDERS', 'DB."Sales".ORDERS'])
def test_project_rejects_quoted_sources_before_drafting(project, source):
    s, _ = project
    spec = contract()
    spec["scope"] = [source, "DB.S.CUSTOMERS"]
    s = Session.create(
        s.workspace,
        workflow="pipeline-development",
        strict_scope=True,
        targets=spec["scope"],
        guard=GuardSettings(),
        guard_profile="moderate",
    )
    with pytest.raises(ValueError, match="source tables must use unquoted"):
        engine.draft(s, spec)
    assert engine.state(s) is None


@pytest.mark.parametrize("location", ["baseline", "candidate"])
@pytest.mark.parametrize("source", ['DB.S."orders"', "DB.S.ORDERſ"])
def test_unsupported_source_cannot_alias_an_unquoted_scope_entry(project, location, source):
    s, _ = project
    if location == "baseline":
        spec = contract()
        spec["checks"][-1]["baseline_sql"] = "SELECT ID, AMOUNT FROM " + source
        with pytest.raises(ValueError, match="source tables must use unquoted"):
            engine.draft(s, spec)
        assert engine.state(s) is None
    else:
        approve(s)
        c = candidate(True)
        c["nodes"][0]["sql"] = "SELECT ID, CUSTOMER_ID, AMOUNT FROM " + source
        with pytest.raises(ValueError, match="source tables must use unquoted"):
            submit(s, c)
        assert engine.state(s)["candidate"] is None


@pytest.mark.parametrize("source", ["ORDERS", "S.ORDERS", "DB..ORDERS", "DB.S.ORDERS.EXTRA"])
def test_project_rejects_sources_without_three_identifier_parts(project, source):
    original, _ = project
    spec = contract()
    spec["scope"] = [source, "DB.S.CUSTOMERS"]
    for check in spec["checks"]:
        if check.get("baseline_sql"):
            check["baseline_sql"] = check["baseline_sql"].replace("DB.S.ORDERS", source)
    s = Session.create(
        original.workspace,
        workflow="pipeline-development",
        strict_scope=True,
        targets=spec["scope"],
        guard=GuardSettings(),
        guard_profile="moderate",
    )
    with pytest.raises(ValueError, match="source tables must use unquoted DB.SCHEMA.OBJECT"):
        engine.draft(s, spec)
    assert engine.state(s) is None


@pytest.mark.parametrize(
    "name", ["DB.S.CAFÉ", "DÉB.S.ORDERS", "DB.SCÉMA.ORDERS", "DB.S." + "A" * 256]
)
def test_project_object_names_reject_non_ascii_and_overlength_parts(name):
    from grayson.projects.sql import deployment_target

    spec = contract()
    spec["scope"] = [name]
    with pytest.raises(ValueError, match="source tables must use unquoted"):
        Contract.model_validate(spec)
    with pytest.raises(ValueError, match="deployment target must use unquoted"):
        deployment_target(name)


def test_project_object_names_allow_ascii_grammar_and_maximum_length():
    from grayson.projects.sql import deployment_target

    name = "_Db1.S$2." + "a" * 255
    spec = contract()
    spec["scope"] = [name]
    assert Contract.model_validate(spec).scope == [name.upper()]
    assert deployment_target(name) == name.upper()


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


@pytest.mark.parametrize("setup_failure", [None, "create", "metadata"])
def test_deployment_checks_actual_target_and_preserves_source_scope(
    project, monkeypatch, setup_failure
):
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
    if setup_failure:
        before_sessions = set(s.workspace.list_session_ids())
        with monkeypatch.context() as patch:
            if setup_failure == "create":

                def fail_create(*args, **kwargs):
                    raise OSError("temporary session storage failure")

                patch.setattr(Session, "create", fail_create)
            else:
                log_event = Session.log_event

                def fail_after_metadata(child, actor, event_type, payload):
                    if event_type == "session_created" and child.workflow == "table-health":
                        raise OSError("temporary initialization failure")
                    return log_event(child, actor, event_type, payload)

                patch.setattr(Session, "log_event", fail_after_metadata)
            with pytest.raises(OSError):
                engine.deployment_check(s, package["revision"], executor)
        recovered = engine.state(s)
        assert recovered["phase"] == package["phase"]
        assert not recovered["lease"]
        for audit_id in set(s.workspace.list_session_ids()) - before_sessions:
            assert Session(s.workspace, audit_id).get_meta("project_verification_parent") == s.id
    result = engine.deployment_check(s, engine.state(s)["revision"], executor)["project"]
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
    # The runner must keep the supplied executor through deployment verification.
    from grayson.projects.runner import dispatch

    def unexpected_connection(*args, **kwargs):
        raise AssertionError("deployment switched away from the supplied executor")

    monkeypatch.setattr("grayson.core.run.get_executor", unexpected_connection)
    result = dispatch(
        s, {"action": "deployment_check"}, executor=executor, revision=result["revision"]
    )["project"]
    assert result["deployed_verification"]["verdict"] == "pass"
    engine.accept_deployment(s, result["revision"])
    assert s.stage == "closed" and s.outcome == "project_verified"


@pytest.mark.parametrize("deployed_region", ["corrupt", None])
def test_deployment_compares_output_with_intermediate_semantic_checks(project, deployed_region):
    from grayson.core import proposals

    s, executor = project
    spec = contract()
    spec["checks"][0].update(
        relation="customers",
        keys=["CUSTOMER_ID"],
        baseline_sql="SELECT CUSTOMER_ID, REGION FROM DB.S.CUSTOMERS WHERE ACTIVE=1",
    )
    approve(s, spec)
    submit(s, candidate(True))
    assert verify(s, executor)["verification"]["verdict"] == "pass"
    review_and_checkpoints(s)
    engine.finish(s, engine.state(s)["revision"])
    package = engine.deployment_package(s, engine.state(s)["revision"])["project"]
    pid = package["deployment"]["pid"]
    proposals.decide(s, pid, True, actor="user")
    rows = executor.execute(package["candidate_sql"]).rows
    with sqlite3.connect(executor.db_path) as con:
        con.execute(
            'CREATE TABLE "DB.S.ENRICHED" (ID INT, CUSTOMER_ID INT, AMOUNT REAL, REGION TEXT)'
        )
        con.executemany(
            'INSERT INTO "DB.S.ENRICHED" VALUES (?,?,?,?)',
            [(r["ID"], r["CUSTOMER_ID"], r["AMOUNT"], deployed_region) for r in rows],
        )
    proposals.mark_applied(s, pid)
    result = engine.deployment_check(s, package["revision"], executor)["project"]
    by_id = {r["id"]: r for r in result["deployed_verification"]["results"]}
    assert by_id["region"]["status"] == "pass"  # Source intermediate remains correct.
    assert by_id["population.missing"]["status"] == "pass"
    assert by_id["revenue"]["status"] == "pass"
    assert result["deployed_verification"]["verdict"] == "fail"
    assert by_id["deployment.missing_rows"]["status"] == "fail"
    assert by_id["deployment.unexpected_rows"]["status"] == "fail"
    with pytest.raises(ValueError, match="passing deployment checks"):
        engine.accept_deployment(s, result["revision"])
    # Correcting just the semantic values restores deployment acceptance.
    with sqlite3.connect(executor.db_path) as con:
        con.executemany(
            'UPDATE "DB.S.ENRICHED" SET REGION=? WHERE ID=?',
            [(r["REGION"], r["ID"]) for r in rows],
        )
    result = engine.deployment_check(s, result["revision"], executor)["project"]
    assert result["deployed_verification"]["verdict"] == "pass"
    engine.accept_deployment(s, result["revision"])
    assert s.outcome == "project_verified"


@pytest.mark.parametrize("mode", ["drive", "watch"])
def test_supervisor_repairs_and_stops_at_deployment_boundary(project, monkeypatch, mode):
    from grayson.projects.runner import drive, watch

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
        if p["phase"] == "deployment_review":
            return {"action": "accept_deployment"}
        return {"action": "deployment"}

    if mode == "watch":
        from grayson.core import proposals

        def unexpected_connection(*args, **kwargs):
            raise AssertionError("watch switched away from the supplied executor")

        monkeypatch.setattr("grayson.core.run.get_executor", unexpected_connection)
        sleeps = []

        def no_sleep(seconds):
            sleeps.append(seconds)
            assert len(sleeps) < 10, "watcher did not complete the verified deployment"

        monkeypatch.setattr("grayson.projects.runner.time.sleep", no_sleep)

        def human_deploy(change):
            assert change["phase"] not in {"blocked", "deployment_failed"}
            if change["phase"] == "awaiting_deployment":
                current = engine.state(s)
                pid = current["deployment"]["pid"]
                # Simulate the separate human approval/execution boundary.
                assert s.proposal(pid)["status"] == "proposed"
                proposals.decide(s, pid, True, actor="user")
                rows = executor.execute(current["candidate_sql"]).rows
                with sqlite3.connect(executor.db_path) as con:
                    con.execute(
                        'CREATE TABLE "DB.S.ENRICHED" '
                        "(ID INT, CUSTOMER_ID INT, AMOUNT REAL, REGION TEXT)"
                    )
                    con.executemany(
                        'INSERT INTO "DB.S.ENRICHED" VALUES (?,?,?,?)',
                        [(r["ID"], r["CUSTOMER_ID"], r["AMOUNT"], r["REGION"]) for r in rows],
                    )
                proposals.mark_applied(s, pid)

        result = watch(s, provider, executor=executor, on_change=human_deploy)["project"]
    else:
        result = drive(s, provider, executor=executor)["project"]
    assert result["phase"] == ("complete" if mode == "watch" else "awaiting_deployment")
    assert "needs_revision" in seen_phases
    assert result["iterations"] == 2 and not result["runner"]
    assert s.proposal(result["deployment"]["pid"])["status"] == (
        "applied" if mode == "watch" else "proposed"
    )


@pytest.fixture(params=["candidate_review", "ready_for_review", "awaiting_deployment"])
def project_at_review_boundary(project, request):
    s, executor = project
    approve(s, contract(approval="guided" if request.param == "candidate_review" else "bounded"))
    submit(s, candidate(True))
    if request.param != "candidate_review":
        verify(s, executor)
        review_and_checkpoints(s)
    if request.param == "awaiting_deployment":
        engine.finish(s, engine.state(s)["revision"])
        engine.deployment_package(s, engine.state(s)["revision"])
    assert engine.state(s)["phase"] == request.param
    return s


def test_narrative_only_candidate_edits_preserve_progress(project_at_review_boundary):
    s = project_at_review_boundary
    before = engine.state(s)
    changed = copy.deepcopy(before["candidate"])
    changed["summary"] = "Clarified explanation"
    changed["diagnosis"] = "No SQL changes"
    for node in changed["nodes"]:
        node["purpose"] += " (clarified wording)"
    with pytest.raises(ValueError, match="SQL is unchanged"):
        submit(s, changed)
    assert engine.state(s) == before


def test_pause_resume_restores_review_boundary(project_at_review_boundary):
    s = project_at_review_boundary
    before = engine.state(s)
    engine.control(s, "pause", "Review later", before["revision"])
    engine.control(s, "pause", "Still reviewing", engine.state(s)["revision"])
    resumed = engine.control(s, "resume", "Continue", engine.state(s)["revision"])["project"]
    assert resumed["phase"] == before["phase"]
    for key in ("candidate_digest", "candidate_approved", "verification", "review", "deployment"):
        assert resumed.get(key) == before.get(key)
    assert not resumed["lease"] and not resumed["runner"]
    engine.control(s, "block", "A real issue needs investigation", resumed["revision"])
    resumed = engine.control(s, "resume", "Investigate", engine.state(s)["revision"])["project"]
    assert resumed["phase"] == "needs_revision"


def test_runner_cannot_claim_an_active_verifier(project):
    from grayson.projects.runner import drive

    s, executor = project
    approve(s)
    submit(s, candidate(True))
    attempted = []

    def unexpected_provider(context):
        pytest.fail("competing runner must not reach its provider")

    class CompetingExecutor:
        def execute(self, sql, timeout_seconds=0):
            if not attempted:
                attempted.append(True)
                before = engine.state(s)
                with pytest.raises(ValueError, match="verification is in progress"):
                    drive(s, unexpected_provider, executor=executor)
                assert engine.state(s) == before
            return executor.execute(sql, timeout_seconds)

    result = verify(s, CompetingExecutor())
    assert result["verification"]["verdict"] == "pass"
    assert attempted and not result.get("runner")


def test_runner_does_not_block_a_verifier_started_during_reasoning(project):
    from grayson.projects.runner import drive

    s, executor = project
    spec = contract()
    spec["policy"]["max_stalled_iterations"] = 1
    approve(s, spec)

    def provider(context):
        submit(s, candidate(True))

        def another_verifier(state):
            state["lease"] = {"token": "concurrent", "expires": engine.time.time() + 60}
            return state

        engine._mutate(s, engine.state(s)["revision"], "test_verifier_started", another_verifier)
        return {"action": "query", "payload": {"sql": "SELECT * FROM DB.S.ORDERS"}}

    result = drive(s, provider, executor=executor)["project"]
    assert result["phase"] == "verifying"
    assert result["lease"]["token"] == "concurrent"
    assert not result["runner"]
    with pytest.raises(ValueError, match="verification is in progress"):
        engine.control(s, "block", "Stale action", result["revision"], actor="agent")
    assert engine.state(s)["lease"] == result["lease"]


def test_provider_plan_schema_matches_dispatch_payload(project):
    from grayson.projects.runner import drive

    s, executor = project
    approve(s)

    def provider(context):
        schema = context["actions"]["plan"]
        assert schema["type"] == "object"
        assert "steps" in schema["required"]
        assert schema["properties"]["steps"]["type"] == "array"
        return {
            "action": "plan",
            "payload": {
                "steps": [
                    {"id": "inspect", "task": "Inspect source grain"},
                    {"id": "build", "task": "Build the candidate", "depends_on": ["inspect"]},
                ]
            },
        }

    result = drive(s, provider, max_steps=1, executor=executor)["project"]
    assert [step["id"] for step in result["plan"]] == ["inspect", "build"]
    assert result["phase"] == "building"


@pytest.mark.parametrize("malformed", ["structured intervention request", None, [], 5])
def test_runner_self_corrects_nonobject_intervention_payload(project, malformed):
    from grayson.projects.runner import drive

    s, executor = project
    approve(s)
    turns = []

    def provider(context):
        turns.append(context)
        if len(turns) == 2:
            assert "error" in context["previous_result"]
        return {
            "action": "intervention",
            "payload": {
                "kind": "free_response",
                "title": "Confirm the data window",
                "payload": malformed if len(turns) == 1 else {"question": "Which date window?"},
            },
        }

    result = drive(s, provider, max_steps=2, executor=executor)["project"]
    assert len(turns) == 2 and result["phase"] == "building"
    assert len(s.interventions("open")) == 1
    assert s.interventions("open")[0]["request"]["question"] == "Which date window?"


def test_generic_abandonment_cancels_project_state(project):
    s, _ = project
    approve(s)
    s.add_intervention("free_response", "Pending decision", "", {"question": "When?"})

    def active_work(state):
        state["lease"] = {"token": "verifier", "expires": engine.time.time() + 60}
        state["runner"] = {"token": "runner", "expires": engine.time.time() + 60}
        return state

    engine._mutate(s, engine.state(s)["revision"], "test_active_work", active_work)
    with pytest.raises(checkpoints.EnforcementError, match="user action"):
        checkpoints.abandon_session(s, actor="agent", reason="Stopped")
    checkpoints.abandon_session(s, reason="No longer needed")
    result = engine.state(s)
    assert result["phase"] == "cancelled"
    assert not result["lease"] and not result["runner"]
    assert s.stage == "closed" and s.outcome == "abandoned"
    assert not s.interventions("open")


def test_project_cancel_cancels_only_open_interventions(project):
    s, _ = project
    approve(s)
    first = s.add_intervention("free_response", "Open question", "", {"question": "When?"})
    answered = s.add_intervention("free_response", "Answered question", "", {"question": "Who?"})
    s.respond_intervention(answered, {"text": "Confirmed owner"})
    revision = engine.state(s)["revision"]
    with pytest.raises(ValueError, match="human"):
        engine.control(s, "cancel", "No longer needed", revision, actor="agent")
    assert s.intervention(first)["status"] == "open"
    engine.control(s, "cancel", "No longer needed", revision, actor="user")
    assert s.stage == "closed" and not s.interventions("open")
    assert s.intervention(first)["status"] == "cancelled"
    assert s.intervention(answered)["response"] == {"text": "Confirmed owner"}
    events = s.events(event_type="intervention_cancelled")
    assert [event["payload"]["iid"] for event in events] == [first]
    assert events[0]["actor"] == "user"


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
    resumed = engine.control(s, "resume", "Restart interrupted checks", result["revision"])[
        "project"
    ]
    assert resumed["phase"] == "needs_revision" and resumed["verification"] is None


def test_long_verification_does_not_refresh_early_evidence(project, monkeypatch):
    s, executor = project
    spec = contract()
    spec["policy"]["evidence_minutes"] = 1
    approve(s, spec)
    submit(s, candidate(True))
    now = [engine.time.time()]
    monkeypatch.setattr(engine.time, "time", lambda: now[0])

    class SlowExecutor:
        def execute(self, sql, timeout_seconds=0):
            result = executor.execute(sql, timeout_seconds)
            now[0] += 10
            return result

    result = verify(s, SlowExecutor())
    report = result["verification"]
    assert report["verdict"] == "pass" and report["finished"] == now[0]
    assert report["finished"] - report["started"] > 60
    assert not engine.status(s)["evidence_current"]
    with pytest.raises(ValueError, match="fresh"):
        review_and_checkpoints(s)


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
    assert 'id="checkpoints"' in build and 'id="verification"' in build
    assert 'data-list="queries"' in build
    assert "Current customer region: baseline" in build
    history = client.get(f"/session/{s.id}?t=test&view=history").text
    assert "SQL changes" in history and "right multiplicity" in history
    assert "ACTIVE" in history
    queries = client.get(f"/session/{s.id}?t=test&view=queries").text
    assert 'data-list="queries"' in queries and 'id="deployment"' in queries
    qid = engine.state(s)["verification"]["results"][0]["qid"]
    query = client.get(f"/session/{s.id}/query/{qid}?t=test")
    assert query.status_code == 200 and f"/session/{s.id}?t=test#queries" in query.text
    proposal = s.proposal(engine.state(s)["deployment"]["pid"])
    failed = client.post(
        f"/session/{s.id}/proposal/{proposal['pid']}/approve?t=test", data={"digest": "stale"}
    )
    assert failed.status_code == 400 and 'aria-label="Project views"' in failed.text
    assert 'id="findings"' not in failed.text
    alerts = re.findall(r'<div class="banner" role="alert">(.*?)</div>', failed.text, re.S)
    assert len(alerts) == 1 and "changed" in alerts[0]
    assert "data-live" not in failed.text
    approved = client.post(
        f"/session/{s.id}/proposal/{proposal['pid']}/approve?t=test",
        data={"digest": review_digest(proposal)},
    )
    assert approved.status_code == 200 and "I've applied this" in approved.text
    assert approved.url.path == f"/session/{s.id}"


def test_project_stale_revision_shows_one_shared_error_alert(project):
    from fastapi.testclient import TestClient

    from grayson.ui.server import build_app

    s, _ = project
    p = engine.draft(s, contract(), 0)["project"]
    client = TestClient(build_app(s.workspace, token="test"), base_url="http://127.0.0.1")
    failed = client.post(
        f"/session/{s.id}/project/approve?t=test",
        data={"revision": p["revision"] + 1, "digest": p["contract_digest"]},
    )
    assert failed.status_code == 400 and 'aria-label="Project views"' in failed.text
    alerts = re.findall(r'<div class="banner" role="alert">(.*?)</div>', failed.text, re.S)
    assert alerts == ["project changed; read project_status and retry against its revision"]
    assert "data-live" not in failed.text
    assert engine.state(s)["phase"] == "awaiting_brief"


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


@pytest.mark.parametrize("phase", ["legacy_before_brief", "awaiting_brief", "building"])
def test_project_charts_and_evidence_share_the_workspace(project, phase):
    from fastapi.testclient import TestClient

    from grayson.charts import add_chart
    from grayson.ui.server import build_app

    s, executor = project
    sql = "SELECT ID, AMOUNT FROM DB.S.ORDERS"
    if phase == "legacy_before_brief":
        # Existing artifacts from older releases must remain inspectable.
        qid = s.allocate_qid(None, sql, "Inspect order amounts")
        rows = executor.execute(sql).rows
        s.cache.save(qid, rows, sql=sql, source_tables=s.targets, truncated=False)
        s.update_query(qid, status="executed", row_count=len(rows))
    else:
        approve(s)
        qid = run_statement(s, sql, executor=executor, label="Inspect order amounts")["qid"]
    chart = add_chart(s, qid, "bar", "ID", ["AMOUNT"], "Order amounts", note="Source evidence")
    if phase == "awaiting_brief":
        engine.draft(s, contract(), engine.state(s)["revision"])
    elif phase == "building":
        submit(s, candidate(True))
        verify(s, executor)
    client = TestClient(build_app(s.workspace, token="test"), base_url="http://127.0.0.1")
    page = client.get(f"/session/{s.id}?t=test").text
    for anchor in ("analysis", "proposal", "verification", "checkpoints", "queries"):
        assert f'id="{anchor}"' in page
        assert f'href="#{anchor}"' in page
    assert f'data-chart="{chart["chart_id"]}"' in page
    assert (
        f'data-svg-url="/session/{s.id}/chart/{chart["chart_id"]}/svg?detail=1&amp;t=test"' in page
    )
    assert "Plotted data (3 rows)" in page and "Source evidence" in page
    assert 'id="chart-lightbox"' in page and 'src="/static/charts.js"' in page
    assert f"/session/{s.id}/query/{qid}?t=test" in page
    assert "Inspect order amounts" in page
    assert 'aria-label="Current project status"' in page
    assert 'data-list="queries"' in page
    if phase == "building":
        assert 'class="pipeline-edge"' in page and "Order grain" in page
    else:
        assert 'class="pipeline-edge"' not in page
    # Bookmarked sections now retain the proposal and other evidence around them.
    for old_section, anchor in (("checks", "verification"), ("queries", "queries")):
        response = client.get(f"/session/{s.id}?t=test&view={old_section}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/session/{s.id}?t=test#{anchor}"
    # Rejected actions use the same chart context, lightbox, and evidence components.
    failure = client.post(
        f"/session/{s.id}/project/approve?t=test", data={"revision": 999, "digest": "stale"}
    )
    assert failure.status_code == 400
    assert f'data-chart="{chart["chart_id"]}"' in failure.text
    assert 'id="chart-lightbox"' in failure.text
    assert 'id="checkpoints"' in failure.text and 'data-list="queries"' in failure.text
    assert "data-live" not in failure.text


def test_project_workspace_marks_earlier_checkpoint_evidence_as_stale(project):
    from fastapi.testclient import TestClient

    from grayson.ui.server import build_app

    s, executor = project
    approve(s)
    submit(s, candidate(True))
    verify(s, executor)
    review_and_checkpoints(s)
    assert all(c["status"] == "complete" for c in s.checkpoints())
    submit(s, candidate(False))
    client = TestClient(build_app(s.workspace, token="test"), base_url="http://127.0.0.1")
    page = client.get(f"/session/{s.id}?t=test").text
    assert "0/3 checkpoints complete" in page
    assert page.count("Earlier proposal: this checkpoint needs fresh evidence.") == 3
    assert 'id="settled-checkpoints"' not in page
    # The UI labels historical evidence without rewriting the underlying record.
    assert all(c["status"] == "complete" for c in s.checkpoints())


@pytest.mark.parametrize("phase", ["no_brief", "awaiting_brief", "revised_brief"])
@pytest.mark.parametrize(
    "workflow,kind", [("pipeline-development", "pipeline"), ("goal-analysis", "analysis")]
)
def test_brief_approval_gates_all_execution_and_fixes(project, phase, workflow, kind):
    from conftest import FakeExecutor
    from grayson.cache.local import query_session_artifacts
    from grayson.core import file_fixes, proposals
    from grayson.core.run import cache_find, snapshot_metadata

    original, _ = project
    s = Session.create(
        original.workspace,
        workflow=workflow,
        strict_scope=True,
        targets=original.targets,
        guard=original.guard_settings,
        guard_profile="moderate",
    )
    spec = contract(kind=kind)
    s.set_setup_inputs({"goal": "Build a pipeline", "approval": "approved"}, actor="user")
    if phase == "revised_brief":
        approve(s, spec)
    if phase != "no_brief":
        engine.draft(s, spec, (engine.state(s) or {}).get("revision", 0))
    # Seed historical cache/proposals to prove reuse cannot bypass fresh approval.
    s.cache.save(
        "q_0001",
        [{"ID": 1}],
        sql="SELECT ID FROM DB.S.ORDERS",
        source_tables=s.targets,
        truncated=False,
    )
    pid = s.add_proposal("ddl_snippet", "Earlier fix", {"ddl": "SELECT 1"}, None, None)
    s.decide_proposal(pid, "approved", "user")
    executor = FakeExecutor()
    assert "approval" in engine.status(s)["query_blocker"]
    for sql in (
        "SELECT * FROM DB.S.ORDERS",
        "SHOW TABLES",
        "DESCRIBE TABLE DB.S.ORDERS",
        "EXPLAIN SELECT * FROM DB.S.ORDERS",
    ):
        result = run_statement(s, sql, executor=executor)
        assert result["status"] == "rejected" and result["rule"] == "project"
        assert "approval" in result["reason"]
    assert snapshot_metadata(s, executor)["status"] == "skipped"
    with pytest.raises(ValueError, match="approval"):
        cache_find(s, check_freshness=True, executor=executor)
    assert cache_find(s)  # Existing evidence remains readable for human review.
    with pytest.raises(ValueError, match="approval"):
        query_session_artifacts(s, "SELECT * FROM q_0001")
    with pytest.raises(ValueError, match="approval"):
        proposals.record_proposal(s, "ddl_snippet", "New fix", {"ddl": "SELECT 2"}, None)
    with pytest.raises(ValueError, match="approval"):
        file_fixes.draft(s, "new.sql", "SELECT 1", "New file")
    with pytest.raises(ValueError, match="approval"):
        file_fixes.apply(s, pid)
    with pytest.raises(ValueError, match="approval"):
        proposals.mark_applied(s, pid)
    assert not executor.calls
    assert s.stage == "setup" and s.executed_count() == 0
    assert not (s.workspace.root / "new.sql").exists()
    if phase == "no_brief":
        engine.draft(s, spec)
    p = engine.state(s)
    engine.approve(s, p["revision"], p["contract_digest"])
    assert run_statement(s, "SELECT * FROM DB.S.ORDERS", executor=executor)["status"] == "executed"
    assert query_session_artifacts(s, "SELECT * FROM q_0001")[1]
    assert snapshot_metadata(s, executor)["status"] == "ok"
    assert file_fixes.draft(s, "new.sql", "SELECT 1", "New file")["status"] == "proposed"


def test_project_start_skips_warehouse_work_in_cli_and_mcp(project, monkeypatch):
    from typer.testing import CliRunner

    from conftest import call_mcp
    from grayson.cli import app
    from grayson.mcp.server import build_server

    s, _ = project

    def no_executor(*args, **kwargs):
        pytest.fail("project startup must not connect to the warehouse before brief approval")

    monkeypatch.setattr("grayson.core.run.get_executor", no_executor)
    result = call_mcp(
        build_server(s.workspace),
        "session_start",
        {
            "workflow": "goal-analysis",
            "tables": s.targets,
            "new": True,
        },
    )
    assert result["metadata_snapshot"]["status"] == "skipped"
    assert "approval" in result["metadata_snapshot"]["reason"]
    cli = CliRunner().invoke(
        app,
        [
            "session",
            "start",
            "--workflow",
            "pipeline-development",
            "--table",
            "DB.S.ORDERS",
            "--new",
        ],
    )
    assert cli.exit_code == 0, cli.output
    assert '"status": "skipped"' in cli.output and "approval" in cli.output


def test_project_workspace_shows_and_applies_ordinary_proposals(project):
    from fastapi.testclient import TestClient

    from grayson.core import file_fixes, proposals
    from grayson.ui.server import build_app

    s, executor = project
    approve(s)
    sql = proposals.record_proposal(
        s,
        "ddl_snippet",
        "Copy the staging SQL",
        {
            "ddl": "CREATE VIEW DB.S.STAGING AS SELECT * FROM DB.S.ORDERS",
            "rationale": "Keep the staged orders available for review",
            "run_target": "Snowflake worksheet",
        },
        None,
    )
    source = s.workspace.root / "pipeline.sql"
    source.write_text("SELECT 1\n", encoding="utf-8")
    fix = file_fixes.draft(s, "pipeline.sql", "SELECT 2\n", "Update the pipeline source")
    submit(s, candidate(True))
    verify(s, executor)
    review_and_checkpoints(s)
    engine.finish(s, engine.state(s)["revision"])
    p = engine.deployment_package(s, engine.state(s)["revision"])["project"]
    deployment = s.proposal(p["deployment"]["pid"])
    client = TestClient(build_app(s.workspace, token="test"), base_url="http://127.0.0.1")
    page = client.get(f"/session/{s.id}?t=test").text
    for proposal in (sql, fix):
        assert f'id="proposal-{proposal["pid"]}"' in page
        assert proposal["title"] in page
    assert "2 fixes awaiting review" in page
    assert "Keep the staged orders available for review" in page
    assert "Snowflake worksheet" in page and "Copy SQL" in page
    assert 'data-list="proposals"' in page and 'id="checkpoints"' in page
    assert page.count(f"/proposal/{deployment['pid']}/approve?t=test") == 1
    for proposal in (sql, fix):
        approved = client.post(
            f"/session/{s.id}/proposal/{proposal['pid']}/approve?t=test",
            data={"digest": file_fixes.review_digest(proposal)},
        )
        assert approved.status_code == 200 and s.proposal(proposal["pid"])["status"] == "approved"
    assert source.read_text(encoding="utf-8") == "SELECT 1\n"
    applied = client.post(f"/session/{s.id}/proposal/{fix['pid']}/apply?t=test")
    assert applied.status_code == 200 and source.read_text(encoding="utf-8") == "SELECT 2\n"
    applied_sql = client.post(
        f"/session/{s.id}/proposal/{sql['pid']}/applied?t=test",
        data={"digest": file_fixes.review_digest(sql)},
    )
    assert applied_sql.status_code == 200 and s.proposal(sql["pid"])["status"] == "applied"
    # Revising a brief does not hide existing fixes or their review history.
    engine.draft(s, contract(), engine.state(s)["revision"])
    page = client.get(f"/session/{s.id}?t=test").text
    assert f'id="proposal-{fix["pid"]}"' in page and f'id="proposal-{sql["pid"]}"' in page
    assert "Work is blocked until you approve the brief" in page


def test_project_tools_are_discoverable_and_submit_briefs_over_stdio(project):
    import asyncio
    import json
    import sys

    from mcp import ClientSession, StdioServerParameters, stdio_client

    s, _ = project

    async def probe():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from grayson.cli import main; main()", "mcp", "serve"],
            cwd=str(s.workspace.root),
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write, read_timeout_seconds=20) as client,
        ):
            initialized = await client.initialize()
            assert "project_schema" in initialized.instructions
            assert "project_draft" in initialized.instructions
            listed = await client.list_tools()
            names = {t.name for t in listed.tools}
            assert {"project_schema", "project_draft"} <= names
            assert not {"project_approve", "project_approve_candidate"} & names
            schema = await client.call_tool("project_schema", {})
            assert not schema.is_error and "contract" in json.loads(schema.content[0].text)
            result = await client.call_tool(
                "project_draft", {"session_id": s.id, "spec": contract()}
            )
            assert not result.is_error
            saved = json.loads(result.content[0].text)["project"]
            assert saved["phase"] == "awaiting_brief"
            assert not saved["approved_digest"]

    asyncio.run(probe())


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


@pytest.fixture
def revalidatable_project(project):
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
    return s, executor, original_result


def test_revalidation_is_finite_idempotent_and_keeps_history(revalidatable_project):
    from grayson.projects.reuse import revalidate

    s, executor, original_result = revalidatable_project
    replay = revalidate(s, "source-change-1", original_result["revision"], executor)["project"]
    assert replay["phase"] == "complete" and replay["revalidation_of"] == s.id
    assert engine.state(s)["verification"] == original_result["verification"]
    duplicate = revalidate(s, "source-change-1", original_result["revision"], executor)["project"]
    assert duplicate["revision"] == replay["revision"]
    with pytest.raises(ValueError, match="no preapproved"):
        revalidate(s, "source-change-2", engine.state(s)["revision"], executor)


@pytest.mark.parametrize(
    "failure", ["create_error", "metadata_error", "attach_error", "process_exit"]
)
def test_revalidation_recovers_unattached_reservation(revalidatable_project, monkeypatch, failure):
    from fastapi.testclient import TestClient

    from grayson.projects import reuse
    from grayson.ui.server import build_app

    s, executor, original = revalidatable_project
    revision = original["revision"]
    existing_sessions = set(s.workspace.list_session_ids())
    with monkeypatch.context() as patch:
        if failure == "create_error":

            def fail_create(*args, **kwargs):
                raise OSError("session storage temporarily unavailable")

            patch.setattr(Session, "create", fail_create)
            expected = OSError
        elif failure == "metadata_error":
            log_event = Session.log_event

            def fail_after_metadata(child, actor, event_type, payload):
                if event_type == "session_created":
                    raise OSError("interrupted child initialization")
                return log_event(child, actor, event_type, payload)

            patch.setattr(Session, "log_event", fail_after_metadata)
            expected = OSError
        else:
            mutate = engine._mutate
            expected = SystemExit if failure == "process_exit" else OSError

            def interrupt_attach(session, revision, event, *args, **kwargs):
                if event == "revalidation_attached":
                    raise expected("simulated attachment failure")
                return mutate(session, revision, event, *args, **kwargs)

            patch.setattr(engine, "_mutate", interrupt_attach)
        with pytest.raises(expected):
            reuse.revalidate(s, "recover-me", revision, executor)
    reservation = engine.state(s)["revalidations"]["recover-me"]
    assert reservation["session"] is None
    orphan_ids = set(s.workspace.list_session_ids()) - existing_sessions
    client = TestClient(build_app(s.workspace, token="test"), base_url="http://127.0.0.1")
    if failure != "create_error":
        assert len(orphan_ids) == 1
        orphan = Session(s.workspace, next(iter(orphan_ids)))
        assert orphan.get_meta("project_revalidation_parent") == s.id
        assert "not attached" in engine.query_blocker(orphan)
        assert run_statement(orphan, "SELECT * FROM DB.S.ORDERS", executor=executor)["status"] == (
            "rejected"
        )
        assert orphan.budget_consumed_count() == 0
        if failure == "process_exit":
            # Older initialized orphans must be recognized from their project state too.
            orphan.set_meta("project_revalidation_parent", "")
        page = client.get("/?t=test")
        assert page.status_code == 200 and orphan.id not in page.text
    if failure == "process_exit":
        with pytest.raises(ValueError, match="in progress"):
            reuse.revalidate(s, "recover-me", revision, executor)
        now = reuse.time.time()
        monkeypatch.setattr(reuse.time, "time", lambda: now + 61)
    recovered = reuse.revalidate(s, "recover-me", revision, executor)["project"]
    assert recovered["phase"] == "complete"
    duplicate = reuse.revalidate(s, "recover-me", revision, executor)["project"]
    assert duplicate["revision"] == recovered["revision"]
    assert len(engine.state(s)["revalidations"]) == 1
    attached_id = engine.state(s)["revalidations"]["recover-me"]["session"]
    page = client.get("/?t=test")
    assert page.status_code == 200 and attached_id in page.text
    assert all(sid not in page.text for sid in orphan_ids)
    with pytest.raises(ValueError, match="no preapproved"):
        reuse.revalidate(s, "extra-run", engine.state(s)["revision"], executor)


@pytest.mark.parametrize("interruption", ["before_verify", "after_pass", "after_fail"])
def test_revalidation_resumes_attached_child(revalidatable_project, monkeypatch, interruption):
    from grayson.projects import reuse

    s, executor, original = revalidatable_project
    if interruption == "after_fail":
        with sqlite3.connect(executor.db_path) as con:
            con.execute('UPDATE "DB.S.CUSTOMERS" SET ACTIVE=0 WHERE CUSTOMER_ID=20')
    with monkeypatch.context() as patch:
        if interruption == "before_verify":

            def interrupt_verify(*args, **kwargs):
                raise SystemExit("exit after attachment")

            patch.setattr(engine, "verify", interrupt_verify)
        else:
            mutate = engine._mutate

            def interrupt_conclusion(session, revision, event, *args, **kwargs):
                if event == "revalidation_finished":
                    raise SystemExit("exit before conclusion")
                return mutate(session, revision, event, *args, **kwargs)

            patch.setattr(engine, "_mutate", interrupt_conclusion)
        with pytest.raises(SystemExit):
            reuse.revalidate(s, "attached-retry", original["revision"], executor)
    child_id = engine.state(s)["revalidations"]["attached-retry"]["session"]
    child = Session(s.workspace, child_id)
    queries_before = child.budget_consumed_count()
    if interruption == "before_verify":
        now = reuse.time.time()

        def live_attempt(state):
            state["lease"] = {"token": "other-verifier", "expires": now + 60}
            return state

        engine._mutate(child, engine.state(child)["revision"], "test_live_attempt", live_attempt)
        pending = reuse.revalidate(s, "attached-retry", original["revision"], executor)["project"]
        assert pending["phase"] == "verifying"
        assert child.budget_consumed_count() == queries_before
        monkeypatch.setattr(reuse.time, "time", lambda: now + 61)
    recovered = reuse.revalidate(s, "attached-retry", original["revision"], executor)["project"]
    assert recovered["phase"] == ("blocked" if interruption == "after_fail" else "complete")
    if interruption != "before_verify":
        assert child.budget_consumed_count() == queries_before
    queries_after = child.budget_consumed_count()
    assert (
        reuse.revalidate(s, "attached-retry", original["revision"], executor)["project"]
        == recovered
    )
    assert child.budget_consumed_count() == queries_after
    assert len(engine.state(s)["revalidations"]) == 1
    assert engine.state(s)["revalidations"]["attached-retry"]["session"] == child_id


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


@pytest.mark.parametrize("paused", [False, True])
def test_lowering_approval_releases_candidate_gate(project, paused):
    s, executor = project
    approve(s, contract(approval="guided"))
    submit(s, candidate(True))
    before = engine.state(s)
    assert before["phase"] == "candidate_review"
    if paused:
        engine.control(s, "pause", "Adjust review level", before["revision"])
    view = engine.set_approval(s, "bounded", engine.state(s)["revision"])
    if paused:
        view = engine.control(s, "resume", "Continue", view["project"]["revision"])
    assert view["project"]["phase"] == "verifying"
    assert view["project"]["candidate_digest"] == before["candidate_digest"]
    verify(s, executor)
    assert engine.state(s)["verification"]["verdict"] == "pass"


@pytest.mark.parametrize("ceiling", ["run", "workspace", "library"])
@pytest.mark.parametrize("paused", [False, True])
def test_tightening_approval_returns_pending_candidate_to_human(project, tmp_path, ceiling, paused):
    from fastapi.testclient import TestClient

    from grayson.config_edit import set_values
    from grayson.library import write_library_settings
    from grayson.projects.runner import drive
    from grayson.ui.server import build_app

    s, executor = project
    approve(s)
    submit(s, candidate(True))
    before = engine.state(s)
    if paused:
        engine.control(s, "pause", "Adjust review level", before["revision"])
    if ceiling == "run":
        engine.set_approval(s, "guided", engine.state(s)["revision"])
    elif ceiling == "workspace":
        set_values(s.workspace.root, {"projects.max_approval": "guided"})
    else:
        library = tmp_path / "team-library"
        library.mkdir()
        write_library_settings(library, {"project_max_approval": "guided"})
        set_values(s.workspace.root, {"library.path": str(library)})
    if paused:
        assert engine.status(s)["project"]["phase"] == "paused"
        engine.control(s, "resume", "Continue", engine.state(s)["revision"])
    elif ceiling == "workspace":
        assert engine.status(s)["project"]["phase"] == "candidate_review"

    def unexpected_provider(_):
        pytest.fail("The runner must wait for candidate approval without invoking the provider")

    view = drive(s, unexpected_provider, max_steps=4, executor=executor)
    p = view["project"]
    assert p["phase"] == "candidate_review"
    assert p["runner_steps"] == 0 and s.budget_consumed_count() == 0
    assert p["candidate_digest"] == before["candidate_digest"]
    assert engine.state(s)["phase"] == "candidate_review"
    assert engine.status(s)["project"]["revision"] == p["revision"]
    client = TestClient(build_app(s.workspace, token="test"), base_url="http://127.0.0.1")
    page = client.get(f"/session/{s.id}?t=test")
    assert page.status_code == 200 and "Approve this candidate" in page.text
    engine.approve_candidate(s, p["revision"], p["candidate_digest"])
    assert verify(s, executor)["verification"]["verdict"] == "pass"


def test_tightening_approval_preserves_live_verifier_and_existing_approval(project):
    import time

    s, _ = project
    approve(s)
    submit(s, candidate(True))

    def claim(p):
        p["lease"] = {"token": "active-verifier", "expires": time.time() + 60}
        return p

    engine._mutate(s, engine.state(s)["revision"], "test_claim", claim)
    p = engine.set_approval(s, "guided", engine.state(s)["revision"])["project"]
    assert p["phase"] == "verifying" and p["lease"]["token"] == "active-verifier"

    def release(p):
        p["lease"] = {}
        return p

    engine._mutate(s, p["revision"], "test_release", release)
    p = engine.status(s)["project"]
    assert p["phase"] == "candidate_review"
    engine.approve_candidate(s, p["revision"], p["candidate_digest"])
    p = engine.set_approval(s, "guided", engine.state(s)["revision"])["project"]
    assert p["phase"] == "verifying" and p["candidate_approved"] == p["candidate_digest"]


def test_concurrent_status_readers_reconcile_candidate_gate_once(project, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local

    from grayson.config_edit import set_values

    s, _ = project
    approve(s)
    submit(s, candidate(True))
    before = engine.state(s)
    set_values(s.workspace.root, {"projects.max_approval": "guided"})
    read_state = engine.state
    barrier, reader = Barrier(2), local()

    def simultaneous_initial_read(session):
        value = read_state(session)
        if not getattr(reader, "started", False):
            reader.started = True
            assert value["revision"] == before["revision"]
            barrier.wait(timeout=10)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(engine, "state", simultaneous_initial_read)
        with ThreadPoolExecutor(max_workers=2) as pool:
            views = list(pool.map(lambda _: engine.status(s), range(2)))
    for view in views:
        assert view["project"]["phase"] == "candidate_review"
        assert view["project"]["revision"] == before["revision"] + 1
        assert view["project"]["candidate_digest"] == before["candidate_digest"]
    assert engine.status(s)["project"]["revision"] == before["revision"] + 1
    # Ordinary mutations must still reject stale revisions rather than retrying.
    with pytest.raises(ValueError, match="project changed"):
        engine.set_approval(s, "bounded", before["revision"])


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
