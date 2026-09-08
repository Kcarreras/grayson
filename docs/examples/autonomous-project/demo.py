"""Executable local demonstration: measured fanout -> diagnosed repair -> DDL review.

Uses a scripted provider and a SQLite warehouse to demonstrate enforcement and
resumption reproducibly. It is not an LLM benchmark and connects to no live data.
Run: uv run python docs/examples/autonomous-project/demo.py --output NEW_DIRECTORY --serve
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import yaml

from grayson.config import GuardSettings
from grayson.core import engine as checkpoints
from grayson.core.session import Session
from grayson.projects import engine
from grayson.projects.runner import drive
from grayson.report import build_report, render_markdown
from grayson.sandbox.executor import SandboxExecutor
from grayson.workspace import Workspace

EXAMPLES = Path(__file__).parent


def load(name):
    return yaml.safe_load((EXAMPLES / name).read_text(encoding="utf-8"))


def provider(context):
    """A deterministic scenario driver; a real adapter can use an agent runtime."""
    brief = context["brief"]
    project = brief["project"]["project"]
    phase = project["phase"]
    if phase == "building":
        return {"action": "candidate", "payload": load("candidate-initial.yaml")}
    if phase == "needs_revision":
        failures = [r["id"] for r in project["verification"]["results"] if r["status"] != "pass"]
        print(
            json.dumps(
                {"failed_checks": failures, "action": "apply approved current-customer rule"}
            ),
            flush=True,
        )
        fixed = load("candidate-fixed.yaml")
        fixed["addressed_checks"] = failures
        return {"action": "candidate", "payload": fixed}
    if phase == "needs_review":
        return {
            "action": "review",
            "payload": {
                "summary": "The first attempt joined historical and current customer records, "
                "duplicating orders 1 and 3. Applying the approved ACTIVE=1 rule fixes "
                "fanout, preserves every order and amount, and selects the expected region.",
                "answers": [
                    "The right key is unique, join keys are non-null, and every order matches.",
                    "Identity checks pass; amounts reconcile per order, including the refund.",
                    "The current-region values match the independently approved ACTIVE=1 baseline.",
                    "Checks use fixed local data. Warehouse deployment and cadence are untested.",
                ],
                "evidence": [r["qid"] for r in project["verification"]["results"]],
                "issues": [],
                "limitations": [
                    "Scripted scenario, not an LLM quality benchmark",
                    "DDL has not been approved or executed",
                ],
            },
        }
    if phase == "ready_for_review":
        pending = brief["readiness"]["open_checks"]
        if pending:
            return {
                "action": "checkpoint",
                "payload": {
                    "key": pending[0],
                    "evidence": [r["qid"] for r in project["verification"]["results"]],
                    "note": "Validated against the current candidate's full-relation probes",
                },
            }
        return {"action": "finish"}
    return {"action": "deployment"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8752)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    workspace = Workspace.init(args.output / "workspace")
    path = args.output / "warehouse.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE "DB.S.ORDERS" (ID INT, CUSTOMER_ID INT, AMOUNT REAL);
        INSERT INTO "DB.S.ORDERS" VALUES (1,10,100), (2,20,200), (3,10,-10);
        CREATE TABLE "DB.S.CUSTOMERS" (CUSTOMER_ID INT, REGION TEXT, ACTIVE INT);
        INSERT INTO "DB.S.CUSTOMERS" VALUES (10,'old',0), (10,'north',1), (20,'south',1);
    """)
    con.close()
    executor = SandboxExecutor(path)
    session = Session.create(
        workspace,
        workflow="pipeline-development",
        targets=["DB.S.ORDERS", "DB.S.CUSTOMERS"],
        strict_scope=True,
        guard=GuardSettings(),
        guard_profile="moderate",
        title="Orders pipeline: catch fanout, repair, verify",
    )
    checkpoints.seed_from_workflow(session)
    # This fixture simulates the human's upfront approval. Normal agent surfaces
    # cannot call approve; users do it in the console or an interactive terminal.
    draft = engine.draft(session, load("brief.yaml"))["project"]
    engine.approve(session, draft["revision"], draft["contract_digest"], actor="user")
    result = drive(session, provider, executor=executor)
    (args.output / "project-evidence.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    (args.output / "report.md").write_text(render_markdown(build_report(session)), encoding="utf-8")
    url = f"http://127.0.0.1:{args.port}/session/{session.id}?t=project-demo"
    print(
        json.dumps(
            {
                "phase": result["project"]["phase"],
                "session": session.id,
                "url": url,
                "evidence": str(args.output / "project-evidence.json"),
            }
        ),
        flush=True,
    )
    if args.serve:
        import uvicorn

        # Demo-only executor injection prevents UI rechecks reaching a real connection.
        import grayson.core.run
        from grayson.ui.server import build_app

        grayson.core.run.get_executor = lambda *args, **kwargs: executor
        uvicorn.run(build_app(workspace, token="project-demo"), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
