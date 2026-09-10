"""Recovery and input contracts across the three public surfaces."""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from conftest import FakeExecutor, call_mcp
from grayson.cli import app
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.mcp.server import build_server
from grayson.ui.server import build_app

runner = CliRunner()


@pytest.mark.parametrize(
    "command",
    [
        ["finding", "add", "latest"],
        ["proposal", "add", "latest", "--kind", "ddl_snippet", "--title", "Fix"],
        ["intervention", "request", "latest", "--kind", "choose", "--title", "Choose"],
        ["knowledge", "set", "DB.S.T1"],
    ],
)
@pytest.mark.parametrize("payload", ["[]", "null", '"text"', "0", "", "  ", "{broken"])
def test_cli_payload_errors_are_actionable_json(workspace, command, payload):
    result = runner.invoke(app, [*command, "--json", payload])
    assert result.exit_code == 1
    assert not result.stdout
    message = json.loads(result.stderr)["error"]
    assert any(word in message for word in ("JSON object", "use --file", "invalid JSON"))


def test_cli_file_errors_and_utf8_bom(workspace):
    file = workspace.root / "profile.json"
    file.write_bytes(b'\xef\xbb\xbf{"grain":"one row per order"}')
    result = runner.invoke(app, ["knowledge", "set", "DB.S.T1", "--file", str(file)])
    assert result.exit_code == 0, result.output
    file.write_bytes(b"\xff\xfeinvalid")
    result = runner.invoke(app, ["knowledge", "set", "DB.S.T1", "--file", str(file)])
    assert result.exit_code == 1
    assert "UTF-8" in json.loads(result.stderr)["error"]
    result = runner.invoke(app, ["knowledge", "set", "DB.S.T1", "--file", "missing.json"])
    assert "cannot read missing.json" in json.loads(result.stderr)["error"]


def test_explicit_empty_json_still_conflicts_with_file(workspace):
    result = runner.invoke(
        app, ["knowledge", "set", "DB.S.T1", "--file", "missing.json", "--json", ""]
    )
    assert json.loads(result.stderr)["error"] == "pass --file or --json, not both"


@pytest.mark.parametrize("payload", ["", "[]", "null", "broken: ["])
@pytest.mark.parametrize(
    "command",
    [["criteria", "set", "latest", "p_001"], ["comparison", "create", "latest"]],
)
def test_cli_spec_errors_are_json(workspace, command, payload):
    file = workspace.root / "spec.yaml"
    file.write_text(payload, encoding="utf-8")
    result = runner.invoke(app, [*command, str(file)])
    assert result.exit_code == 1
    assert "spec.yaml" in json.loads(result.stderr)["error"]


@pytest.fixture
def session(workspace):
    return Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=workspace.config.guard_profiles["moderate"].model_copy(),
        guard_profile="moderate",
    )


@pytest.mark.parametrize("args", [[], ["--sql", ""], ["--sql", "  "]])
def test_blank_sql_explains_input_without_audit_noise(workspace, session, args):
    result = runner.invoke(app, ["query", "run", session.id, *args], input="")
    assert result.exit_code == 1
    assert "no SQL given" in json.loads(result.stderr)["error"]
    assert session.query_log() == []


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("criteria_set", {"pid": "p_001", "spec": {}}),
        ("criteria_queries", {}),
        ("criteria_show", {"pid": "p_001"}),
        ("criteria_run", {"pid": "p_001"}),
        ("criteria_promote", {"pid": "p_001", "criterion_id": "c", "check_id": "check"}),
        ("impact_plan", {}),
        ("impact_show", {}),
        ("impact_launch", {"digest": "missing"}),
        ("impact_run_checks", {}),
        ("comparison_create", {"spec": {}}),
        ("comparison_list", {}),
        ("comparison_show", {"comparison_id": "c"}),
        ("comparison_run", {"comparison_id": "c"}),
        ("comparison_report", {"comparison_id": "c"}),
        ("project_status", {}),
        ("project_draft", {"spec": {}}),
        ("project_candidate", {"spec": {}, "revision": 0}),
        ("project_verify", {"revision": 0}),
        ("project_pause", {"reason": "test", "revision": 0}),
        ("project_revalidate", {"request_id": "test", "revision": 0}),
    ],
)
def test_mcp_missing_session_returns_structured_error(workspace, tool, args):
    result = call_mcp(build_server(workspace), tool, {"session_id": "latest", **args})
    assert result["type"] == "FileNotFoundError"
    assert "no sessions yet" in result["error"]


def test_criteria_query_discovery_cli_mcp_parity(workspace, session):
    qid = run_statement(session, "SELECT * FROM DB.S.T1", executor=FakeExecutor())["qid"]
    before = session.query_stats()
    cli = runner.invoke(app, ["criteria", "queries", "latest", "--search", qid])
    assert cli.exit_code == 0, cli.output
    mcp = call_mcp(
        build_server(workspace), "criteria_queries", {"session_id": "latest", "search": qid}
    )
    assert json.loads(cli.stdout) == mcp
    assert mcp["queries"][0]["qid"] == qid
    assert session.query_stats() == before


def test_browser_errors_offer_recovery_and_preserve_api_errors(workspace):
    token = "private-access-token"
    client = TestClient(build_app(workspace, token=token), base_url="http://127.0.0.1")
    blocked = client.get("/", headers={"accept": "text/html"})
    assert blocked.status_code == 403
    assert "grayson ui serve" in blocked.text
    assert token not in blocked.text
    assert client.get("/").json() == {"detail": "invalid or missing access token"}
    missing = client.get("/no-such-page", headers={"accept": "text/html"})
    assert missing.status_code == 404
    assert "Go to sessions" in missing.text
    assert token not in missing.text
    assert client.get("/no-such-page").json() == {"detail": "Not Found"}


@pytest.mark.parametrize("path", ["/no-such-page", "/session/missing-session"])
def test_authenticated_stale_link_can_recover_to_sessions(workspace, path):
    token = "private-access-token"
    client = TestClient(build_app(workspace, token=token), base_url="http://127.0.0.1")
    page = client.get(f"{path}?t={token}", headers={"accept": "text/html"})
    assert page.status_code == 404
    assert token not in page.text
    assert client.cookies.get("grayson_token") == token
    assert client.get("/", headers={"accept": "text/html"}).status_code == 200


@pytest.mark.parametrize(
    ("host", "supplied"),
    [("127.0.0.1", "wrong-token"), ("untrusted.example", "private-access-token")],
)
def test_error_recovery_does_not_authenticate_invalid_requests(workspace, host, supplied):
    client = TestClient(
        build_app(workspace, token="private-access-token"), base_url=f"http://{host}"
    )
    page = client.get(f"/no-such-page?t={supplied}", headers={"accept": "text/html"})
    assert page.status_code == 404
    assert "set-cookie" not in page.headers
    assert client.get("/").status_code == 403


@pytest.mark.parametrize(
    ("workflow", "label"),
    [("goal-analysis", "ANALYSIS PROJECT"), ("pipeline-development", "PIPELINE PROJECT")],
)
def test_new_project_labels_and_live_updates(workspace, workflow, label):
    s = Session.create(
        workspace,
        workflow=workflow,
        targets=["DB.S.T1"],
        guard=workspace.config.guard_profiles["moderate"].model_copy(),
        guard_profile="moderate",
    )
    client = TestClient(build_app(workspace), base_url="http://127.0.0.1")
    page = client.get(f"/session/{s.id}")
    assert page.status_code == 200
    assert label in page.text
    assert "data-live" in page.text  # The first agent draft must appear without a manual reload.
    s.set_stage("closed")
    assert "data-live" not in client.get(f"/session/{s.id}").text


def test_attention_links_target_visible_proposal_cards(workspace, session):
    from grayson.core import proposals

    p = proposals.record_proposal(session, "ddl_snippet", "Fix", {"ddl": "SELECT 1"}, None)
    client = TestClient(build_app(workspace), base_url="http://127.0.0.1")
    page = client.get(f"/session/{session.id}")
    target = "proposal-" + p["pid"]
    assert f'href="#{target}"' in page.text
    assert f'id="{target}" data-fold="{target}"' in page.text
