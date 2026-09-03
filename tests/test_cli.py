"""End-to-end CLI flow against the fake snow binary."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from grayson.cli import app

runner = CliRunner()


def invoke(*args):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def invoke_err(*args) -> dict:
    """Invoke expecting the CLI to refuse; returns the error payload."""
    result = runner.invoke(app, list(args))
    assert result.exit_code != 0, result.output
    return json.loads(result.stderr or result.output)


@pytest.fixture
def at_a_terminal(monkeypatch):
    """Pretend the CLI is being driven by a human at a prompt.

    User-only actions (close, waive, --force) are gated on an interactive
    terminal, since the CLI cannot otherwise tell its caller from an agent
    shelling out. Tests that exercise those paths opt in here.
    """
    import grayson.cli as cli

    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: True)


@pytest.fixture
def sid(workspace, fake_snow_env) -> str:
    out = invoke(
        "session",
        "start",
        "--workflow",
        "table-health",
        "--table",
        "DB.S.T1",
        "--guard-profile",
        "moderate",
        "--title",
        "e2e",
    )
    assert out["metadata_snapshot"]["status"] == "ok"
    return out["session"]["id"]


def test_full_flow(workspace, fake_snow_env, sid):
    # doctor: snow check may fail (no real snow) but workspace check passes
    out = invoke("session", "status", sid)
    assert out["stage"] == "setup"

    worker = invoke("worker", "join", sid, "--label", "main")["worker"]

    check = invoke("guard", "check", sid, "-q", "DELETE FROM DB.S.T1")
    assert check["allowed"] is False

    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1", "--worker", worker)
    assert run["status"] == "executed"
    assert run["row_count"] == 5
    assert len(run["preview"]) == 5

    found = invoke("cache", "find", sid, "--table", "DB.S.T1")
    assert found and found[0]["qid"] == run["qid"]

    local = invoke("cache", "query", sid, "-q", f"SELECT COUNT(*) AS n FROM {run['qid']}")
    assert local["rows"][0]["n"] == 5

    shown = invoke("cache", "show", sid, run["qid"], "--rows", "2")
    assert len(shown["preview"]) == 2

    log = invoke("query", "log", sid)
    assert any(e["qid"] == run["qid"] for e in log)

    invoke("session", "advance", sid, "--to", "analysis")
    # closing is a user action; an agent shelling out is refused outright
    assert "interactive terminal" in invoke_err("session", "close", sid)["error"]


def test_rejected_query_exit_zero_with_verdict(workspace, fake_snow_env, sid):
    out = invoke("query", "run", sid, "-q", "DROP TABLE DB.S.T1")
    assert out["status"] == "rejected"
    assert out["rule"] == "statement_type"
    assert out["suggestion"]


def test_auth_failure_pauses_agent(workspace, fake_snow_env, sid):
    out = invoke("query", "run", sid, "-q", "SELECT 'FAIL_AUTH' FROM DB.S.T1")
    assert out["status"] == "auth_required"
    assert "action_needed" in out


def test_budget_extension(workspace, fake_snow_env, sid):
    out = invoke("session", "budget", sid, "--cap", "7")
    assert out["guard"]["budget_cap"] == 7


def test_init_is_idempotent_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    invoke("init", str(tmp_path / "w1"))
    result = runner.invoke(app, ["init", str(tmp_path / "w1")])
    assert result.exit_code == 1


def test_workflow_list_and_show(workspace):
    names = {w["name"] for w in invoke("workflow", "list")}
    assert "bug-hunter" in names
    show = invoke("workflow", "show", "bug-hunter")
    assert show["findings_schema"] == "bug_hunter_v1"


def test_setup_requires_a_terminal(workspace):
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 1
    assert "interactive" in result.output and "harness init" in result.output


def test_session_start_records_setup_inputs(workspace, fake_snow_env):
    out = invoke(
        "session",
        "start",
        "--workflow",
        "bug-hunter",
        "--table",
        "DB.S.T1",
        "--input",
        "anomaly_description=duplicate revenue rows",
        "--input",
        "example_locator=ORDER_ID 4711 appears twice",
    )
    assert out["setup_inputs"]["anomaly_description"] == "duplicate revenue rows"
    assert out["setup_inputs_missing"] == ["expectation"]  # the third required input
    sid = out["session"]["id"]
    report = invoke("session", "report", sid)
    assert report["setup_inputs"]["example_locator"] == "ORDER_ID 4711 appears twice"


def test_session_start_rejects_unknown_inputs(workspace, fake_snow_env):
    result = runner.invoke(
        app,
        [
            "session",
            "start",
            "--workflow",
            "bug-hunter",
            "--table",
            "DB.S.T1",
            "--input",
            "not_a_real_key=x",
        ],
    )
    assert result.exit_code == 1
    assert "unknown setup input" in result.output


def test_session_start_interactive_requires_terminal(workspace, fake_snow_env):
    result = runner.invoke(
        app,
        ["session", "start", "--workflow", "bug-hunter", "--table", "DB.S.T1", "--interactive"],
    )
    assert result.exit_code == 1
    assert "terminal" in result.output


def test_workflow_lint_clean_and_broken(workspace):
    assert invoke("workflow", "lint")["ok"] is True
    (workspace.workflows_dir / "shadow.yaml").write_text(
        "name: bug-hunter\ntitle: Shadow\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["workflow", "lint"])
    assert result.exit_code == 1
    report = json.loads(result.output)
    assert report["ok"] is False
    assert "shadows the core workflow" in report["errors"][0]["problem"]


def test_workflow_new_and_fork(workspace):
    invoke("user", "set", "kcg")
    out = invoke("workflow", "new", "orders-health", "--fork", "table-health")
    assert out["lint"]["ok"] is True
    show = invoke("workflow", "show", "orders-health")
    assert show["forked_from"] == "table-health"
    assert show["created_by"] == "kcg"
    result = runner.invoke(app, ["workflow", "new", "bug-hunter"])
    assert result.exit_code == 1  # core names are canonical


def test_user_set_and_show():
    shown = invoke("user", "show")
    assert shown["user_id"] is None
    assert invoke("user", "set", "kcg")["user_id"] == "kcg"
    assert invoke("user", "show")["user_id"] == "kcg"
    result = runner.invoke(app, ["user", "set", "not ok!"])
    assert result.exit_code == 1


def test_unknown_workflow_start_fails(workspace, fake_snow_env):
    result = runner.invoke(app, ["session", "start", "--workflow", "nope", "--table", "DB.S.T1"])
    assert result.exit_code == 1


def test_checkpoint_and_findings_flow(workspace, fake_snow_env, at_a_terminal):
    out = invoke(
        "session",
        "start",
        "--workflow",
        "bug-hunter",
        "--table",
        "DB.S.T1",
        "--title",
        "bug",
    )
    sid = out["session"]["id"]
    assert out["workflow"]["required_checks"]

    # checkpoints seeded and open
    cps = invoke("checkpoint", "list", sid)
    assert cps and all(c["status"] == "open" for c in cps)

    # cannot enter review yet
    blocked = runner.invoke(app, ["session", "advance", sid, "--to", "review"])
    assert blocked.exit_code == 1

    # run a query for evidence
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    qid = run["qid"]

    # completing a checkpoint without evidence fails
    no_ev = runner.invoke(app, ["checkpoint", "complete", sid, "replicate_anomaly"])
    assert no_ev.exit_code == 1

    # complete every checkpoint with evidence
    for c in cps:
        _complete(sid, c["key"], qid, "--note", "done")

    ready = invoke("session", "readiness", sid)
    assert ready["checks_complete"]

    # add a finding
    finding = {
        "title": "Fan-out duplicates in output",
        "severity": "high",
        "confidence": "high",
        "affected_objects": ["DB.S.T1"],
        "reproduction": "re-run the cited query",
        "summary": "A one-to-many join duplicates rows in the final table.",
        "evidence": [qid],
        "extra": {
            "resolution": "root_caused",
            "root_cause": "join fan-out on non-unique key",
            "blast_radius": "1200 rows since 2026-08-01",
            "alternatives_tested": "source dup and dedup bug both ruled out",
        },
    }
    added = invoke("finding", "add", sid, "--json", json.dumps(finding))
    assert added["fid"] == "f_001"

    # now review is reachable
    adv = invoke("session", "advance", sid, "--to", "review")
    assert adv["stage"] == "review"

    invoke("finding", "accept", sid, "f_001")
    assert invoke("finding", "show", sid, "f_001")["accepted"] is True

    # fixes reachable now that a finding exists
    invoke("session", "advance", sid, "--to", "fixes")


def test_upgrade_dev_checkout_gets_instructions(monkeypatch):
    import grayson.cli as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv")

    def fake_run(args, **kwargs):
        class R:
            returncode = 0
            stdout = "some-other-tool v1.0.0\n"
            stderr = ""

        assert args[:3] == ["/usr/bin/uv", "tool", "list"]
        return R()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    out = invoke("upgrade")
    assert out["upgraded"] is False
    assert "git pull" in out["detail"]


def test_upgrade_runs_uv_tool_upgrade(monkeypatch):
    import grayson.cli as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)

        class R:
            returncode = 0
            stdout = "grayson-sql v0.1.0\n" if args[2] == "list" else "Updated grayson-sql\n"
            stderr = ""

        return R()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    out = invoke("upgrade")
    assert out["upgraded"] is True
    assert calls[-1] == ["/usr/bin/uv", "tool", "upgrade", "grayson-sql"]


def test_upgrade_without_uv_fails(monkeypatch):
    import grayson.cli as cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    result = runner.invoke(app, ["upgrade"])
    assert result.exit_code == 1


# -- user-only actions are gated on an interactive terminal ---------------


def _complete(sid_, key, qid, *extra):
    """`checkpoint complete` as an agent does it: with the bar chart(s) the
    workflow requires of the checkpoint, built from the cited query."""
    cp = next(c for c in invoke("checkpoint", "list", sid_) if c["key"] == key)
    charts = []
    for n, _req in enumerate(cp["requires_charts"], 1):
        ch = invoke(
            "chart",
            "add",
            sid_,
            "--artifact",
            qid,
            "--kind",
            "bar",
            "-x",
            "VAL",
            "-y",
            "ID",
            "--title",
            f"{key} chart {n}",
        )
        charts += ["-c", ch["chart_id"]]
    return invoke("checkpoint", "complete", sid_, key, "-e", qid, *charts, *extra)


def _clear_checks(sid_) -> list[str]:
    run = invoke("query", "run", sid_, "-q", "SELECT * FROM DB.S.T1")
    for key in invoke("workflow", "show", "table-health")["required_checks"]:
        _complete(sid_, key["key"], run["qid"])
    return [run["qid"]]


def test_force_cannot_be_claimed_by_a_shell_out(workspace, fake_snow_env, sid):
    # the old hole: --actor defaulted to "user" and --force was honored for it,
    # so a non-interactive `advance --force` cleared every gate
    err = invoke_err("session", "advance", sid, "--to", "review", "--force")
    assert "interactive terminal" in err["error"]
    err = invoke_err("session", "advance", sid, "--to", "review", "--force", "--actor", "user")
    assert "interactive terminal" in err["error"]
    assert invoke("session", "status", sid)["stage"] != "review"


def test_force_works_for_a_human_at_a_prompt(workspace, fake_snow_env, sid, at_a_terminal):
    invoke("session", "advance", sid, "--to", "review", "--force")
    assert invoke("session", "status", sid)["stage"] == "review"


def test_agent_started_sessions_are_not_recorded_under_the_human(workspace, fake_snow_env, sid):
    from grayson.core.session import Session
    from grayson.workspace import Workspace

    s = Session(Workspace.find(), sid)
    started = [e for e in s.events(50) if e["type"] == "session_created"]
    assert started and started[0]["actor"] == "agent"
    inputs = [e for e in s.events(50) if e["type"] == "setup_inputs_recorded"]
    assert all(e["actor"] == "agent" for e in inputs)


def test_agent_stage_changes_are_attributed_to_the_agent(workspace, fake_snow_env, sid):
    invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    invoke("session", "advance", sid, "--to", "synthesis")
    from grayson.core.session import Session
    from grayson.workspace import Workspace

    s = Session(Workspace.find(), sid)
    # the setup -> analysis hop is grayson's own; the declared one is the agent's,
    # and used to be recorded under the human's name
    declared = [e for e in s.events(50) if e["type"] == "stage_changed" and e["actor"] != "system"]
    assert declared and all(e["actor"] == "agent" for e in declared)


def test_clean_close_flow(workspace, fake_snow_env, sid, at_a_terminal):
    _clear_checks(sid)
    ready = invoke("session", "readiness", sid)
    assert ready["clean_close_available"] is True
    out = invoke("session", "close", sid, "--clean", "--note", "all four checks came back sound")
    assert out["stage"] == "closed"
    assert out["outcome"] == "clean"
    assert invoke("session", "status", sid)["outcome_note"] == "all four checks came back sound"


def test_clean_close_refused_while_checks_are_open(workspace, fake_snow_env, sid, at_a_terminal):
    assert "checkpoints still open" in invoke_err("session", "close", sid)["error"]


def test_waive_is_recorded_with_its_reason(workspace, fake_snow_env, sid, at_a_terminal):
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    for key in invoke("workflow", "show", "table-health")["required_checks"]:
        if key["key"] != "freshness":
            _complete(sid, key["key"], run["qid"])
    cp = invoke("checkpoint", "waive", sid, "freshness", "--reason", "static reference table")
    assert cp["status"] == "waived"
    assert cp["note"] == "static reference table"
    ready = invoke("session", "readiness", sid)
    assert ready["checks_complete"] is True
    assert ready["waived_checks"][0]["key"] == "freshness"


def test_waive_refused_without_a_terminal(workspace, fake_snow_env, sid):
    err = invoke_err("checkpoint", "waive", sid, "freshness", "--reason", "static table")
    assert "interactive terminal" in err["error"]


# -- profiling ------------------------------------------------------------


def test_profile_command_returns_citable_evidence(workspace, fake_snow_env, sid):
    doc = invoke("profile", "table", sid, "DB.S.T1")
    assert doc["queries_run"] <= 5
    log = {e["qid"] for e in invoke("query", "log", sid)}
    assert set(doc["evidence"]) <= log
    # the profile's own ids close a checkpoint — no hand-rolled battery needed
    # (plus the chart the workflow requires of null_completeness, from a cited query)
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    chart = invoke(
        "chart",
        "add",
        sid,
        "--artifact",
        run["qid"],
        "--kind",
        "bar",
        "-x",
        "VAL",
        "-y",
        "ID",
        "--title",
        "null rate per column",
    )
    args = [a for q in [*doc["evidence"], run["qid"]] for a in ("-e", q)]
    cp = invoke("checkpoint", "complete", sid, "null_completeness", *args, "-c", chart["chart_id"])
    assert cp["status"] == "complete" and cp["charts"] == [chart["chart_id"]]


def test_profile_stats_and_correlate_over_the_sample(workspace, fake_snow_env, sid):
    doc = invoke("profile", "table", sid, "DB.S.T1")
    stats = invoke("profile", "stats", sid, doc["sample_qid"])
    assert stats["computed"] == "local"
    corr = invoke("profile", "correlate", sid, doc["sample_qid"])
    assert corr["confidence_ceiling"] == "medium"
    assert "not by the warehouse" in corr["caveat"]


def test_profile_stats_on_a_missing_artifact_fails_clearly(workspace, fake_snow_env, sid):
    assert "no cached artifact" in invoke_err("profile", "stats", sid, "q_9999")["error"]


def test_finding_rubric_is_discoverable(workspace):
    scale = invoke("finding", "rubric")
    assert set(scale["severity"]) == {"critical", "high", "medium", "low", "info"}
    assert scale["enforced"]


def test_calibration_gates_apply_through_the_cli(workspace, fake_snow_env, sid):
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    payload = {
        "title": "Nulls in a required column",
        "severity": "high",
        "confidence": "high",
        "summary": "EMAIL is null for a material share of rows.",
        "evidence": [run["qid"]],
    }
    err = invoke_err("finding", "add", sid, "--json", json.dumps(payload))["error"]
    # both gaps in one message — one round trip, not two
    assert "affected_objects" in err and "reproduction" in err
    payload["affected_objects"] = ["DB.S.T1"]
    payload["reproduction"] = "SELECT COUNT(*) FROM DB.S.T1 WHERE EMAIL IS NULL"
    assert invoke("finding", "add", sid, "--json", json.dumps(payload))["severity"] == "high"


@pytest.mark.parametrize(
    ("args", "action"),
    [
        (("finding", "accept", "SID", "f_001"), "accepting a finding"),
        (("proposal", "approve", "SID", "p_001"), "approving a fix proposal"),
        (("proposal", "reject", "SID", "p_001"), "rejecting a fix proposal"),
        (("intervention", "respond", "SID", "i_001", "--json", "{}"), "answering an intervention"),
        (("checkpoint", "waive", "SID", "freshness", "--reason", "n/a"), "waiving a checkpoint"),
        (("session", "close", "SID"), "closing a session"),
        (("library", "migrate"), "migrating the library format"),
    ],
)
def test_every_human_boundary_refuses_a_shell_out(workspace, fake_snow_env, sid, args, action):
    """The whole point of a human boundary is that the agent cannot stand on both
    sides of it. None of these are reachable without a terminal."""
    err = invoke_err(*[sid if a == "SID" else a for a in args])["error"]
    assert action in err
    assert "interactive terminal" in err


def test_knowledge_confirm_refuses_a_shell_out(workspace, fake_snow_env):
    err = invoke_err("knowledge", "confirm", "DB.S.T1", "k_001")["error"]
    assert "confirming a knowledge fact" in err


def test_actor_user_flag_requires_a_terminal(workspace, fake_snow_env, sid):
    """A non-interactive caller must not write its actions into the audit trail
    under the human's name — `--actor user` needs a human at the prompt."""
    err = invoke_err("session", "advance", sid, "--to", "analysis", "--actor", "user")["error"]
    assert "interactive terminal" in err
    run = invoke("query", "run", sid, "-q", "SELECT * FROM DB.S.T1")
    err = invoke_err(
        "checkpoint", "complete", sid, "null_completeness", "-e", run["qid"], "--actor", "user"
    )["error"]
    assert "interactive terminal" in err
    # without the flag the same action proceeds, attributed to the agent
    out = invoke("session", "advance", sid, "--to", "analysis")
    assert out["stage"] == "analysis"
    events = invoke("session", "events", sid, "--limit", "5")
    stage_events = [e for e in events if e["type"] == "stage_changed"]
    # the shell-out's advance is the agent's (auto-advances log as 'system');
    # nothing here may claim the human's name
    assert any(e["actor"] == "agent" for e in stage_events)
    assert not any(e["actor"] == "user" for e in stage_events)


def test_workflow_preview_cli(workspace):
    out = invoke("workflow", "preview", "table-health")
    assert out["core"] is True
    assert "Setup inputs" in out["text"] and "Required checks" in out["text"]
    # a library workflow carries its lint findings in the same response
    invoke("workflow", "new", "empty-flow")
    (workspace.workflows_dir / "empty-flow.yaml").write_text(
        "name: empty-flow\ntitle: Empty\nfindings_schema: standard_v1\n", encoding="utf-8"
    )
    out = invoke("workflow", "preview", "empty-flow")
    assert out["core"] is False
    assert any("no required_checks" in e["problem"] for e in out["lint"])
    err = invoke_err("workflow", "preview", "no-such-workflow")
    assert "no-such-workflow" in err["error"]


def test_session_abandon_is_a_user_action(workspace, fake_snow_env, sid):
    err = invoke_err("session", "abandon", sid, "--reason", "wrong table")["error"]
    assert "abandoning a session" in err and "interactive terminal" in err
    assert invoke("session", "status", sid)["stage"] != "closed"


def test_session_abandon_at_a_terminal(workspace, fake_snow_env, sid, at_a_terminal):
    assert "needs a reason" in invoke_err("session", "abandon", sid, "--reason", "  ")["error"]
    out = invoke("session", "abandon", sid, "--reason", "wrong target table")
    assert out["stage"] == "closed" and out["outcome"] == "abandoned"
    assert out["readiness"]["next_action"] == "session is closed"
    again = invoke_err("session", "abandon", sid, "--reason", "again")["error"]
    assert again == "session is already closed"
    status = invoke("session", "status", sid)
    assert status["outcome"] == "abandoned" and status["outcome_note"] == "wrong target table"
    # nothing published: an abandoned session leaves no report in the library
    assert not (workspace.records_dir / sid).exists()


def test_session_delete_is_a_user_action(workspace, fake_snow_env, sid):
    err = invoke_err("session", "delete", sid, "--yes")["error"]
    assert "deleting a session" in err and "interactive terminal" in err
    assert invoke("session", "status", sid)["id"] == sid  # still there


def test_workflow_show_unpacks_the_findings_schema(workspace):
    show = invoke("workflow", "show", "bug-hunter")
    spec = show["findings_schema_spec"]
    assert spec["name"] == "bug_hunter_v1"
    assert [e["key"] for e in spec["required_extra"]] == [
        "resolution",
        "blast_radius",
        "alternatives_tested",
    ]
    assert spec["discriminator"] == "resolution"
    assert "extra" in spec["example"]
    schemas = invoke("workflow", "schemas")
    assert set(schemas) >= {"standard_v1", "bug_hunter_v1", "parity_v1"}
    assert schemas["standard_v1"]["required_extra"] == []


def test_workflow_delete(workspace):
    invoke("user", "set", "kcg")
    invoke("workflow", "new", "mine")
    out = invoke("workflow", "delete", "mine", "--yes")
    assert out["deleted"].endswith("mine.yaml")
    assert not (workspace.workflows_dir / "mine.yaml").exists()
    result = runner.invoke(app, ["workflow", "delete", "bug-hunter", "--yes"])
    assert result.exit_code == 1 and "cannot be deleted" in result.output + (result.stderr or "")
    (workspace.workflows_dir / "theirs.yaml").write_text(
        "name: theirs\ntitle: T\ncreated_by: mkoval2\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["workflow", "delete", "theirs", "--yes"])
    assert result.exit_code == 1 and "only its author" in result.output + (result.stderr or "")
    assert (workspace.workflows_dir / "theirs.yaml").exists()
