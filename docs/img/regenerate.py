"""Regenerate the console screenshots under docs/img.

Builds a bug-hunter session against the seeded sandbox warehouse (real
queries, real charts), knowledge docs, external checks and a team workflow,
serves the console with the real server, and screenshots each page in both
themes with headless Chromium.

    uv run python docs/img/regenerate.py            # writes docs/img/*.png
    GRAYSON_CHROME=/path/to/chrome uv run python docs/img/regenerate.py

The demo workspace is built under a throwaway directory and removed afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)
TOKEN = "docs-shot"
PORT = 8799
CHROME = os.environ.get("GRAYSON_CHROME", "chromium")

tmp = Path.home() / "shop-qa"  # a readable workspace root for the Settings page
shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir()
os.environ["GRAYSON_CONFIG_DIR"] = str(tmp / "cfg")
os.environ["GRAYSON_USER_ID"] = "kcg"

from grayson.charts.spec import add_chart  # noqa: E402
from grayson.checks import ChecksStore  # noqa: E402
from grayson.cli import SANDBOX_CONFIG  # noqa: E402
from grayson.core import engine  # noqa: E402
from grayson.core.run import run_statement  # noqa: E402
from grayson.core.session import Session  # noqa: E402
from grayson.interventions import build_request  # noqa: E402
from grayson.knowledge import KnowledgeStore  # noqa: E402
from grayson.sandbox.executor import SandboxExecutor, sandbox_db_path  # noqa: E402
from grayson.sandbox.seed import seed_sandbox  # noqa: E402
from grayson.ui.server import build_app  # noqa: E402
from grayson.workflows.authoring import create_workflow  # noqa: E402
from grayson.workspace import Workspace  # noqa: E402

# -- workspace + warehouse ------------------------------------------------
ws = Workspace.init(tmp / "workspace")
(ws.root / "grayson.toml").write_text(SANDBOX_CONFIG, encoding="utf-8")
seed_sandbox(sandbox_db_path(ws.root))
ws = Workspace(ws.root)  # re-read config
ex = SandboxExecutor(sandbox_db_path(ws.root))

T = "SANDBOX.SHOP.ORDERS_ENRICHED"

# -- session ----------------------------------------------------------------
s = Session.create(
    ws,
    workflow="bug-hunter",
    targets=[T],
    guard=ws.config.guard_profiles["moderate"].model_copy(),
    guard_profile="moderate",
    title="Duplicated revenue rows in ORDERS_ENRICHED",
)
engine.seed_from_workflow(s)
s.set_setup_inputs(
    {
        "anomaly_description": "Revenue summed from ORDERS_ENRICHED runs ~13% above ORDERS "
        "for the same period; finance noticed on the August close.",
        "example_locator": "ORDER_ID 1042 appears twice in ORDERS_ENRICHED.",
        "expectation": "One row per ORDER_ID; row counts match ORDERS exactly.",
    }
)


def q(sql: str, label: str) -> str:
    out = run_statement(s, sql, worker="agent", label=label, executor=ex)
    if out["status"] != "executed":
        print("QUERY FAILED", label, out, file=sys.stderr)
        sys.exit(1)
    return out["qid"]


q1 = q(
    f"SELECT COUNT(*) AS ROWS_ENRICHED, COUNT(DISTINCT ORDER_ID) AS ORDER_IDS FROM {T}",
    "row count vs distinct order ids",
)
q2 = q("SELECT COUNT(*) AS ROWS_SOURCE FROM SANDBOX.SHOP.ORDERS", "source row count")
q3 = q(
    f"SELECT PROMO_CODE, COUNT(*) - COUNT(DISTINCT ORDER_ID) AS EXTRA_ROWS FROM {T} "
    "GROUP BY PROMO_CODE HAVING COUNT(*) - COUNT(DISTINCT ORDER_ID) > 0 ORDER BY EXTRA_ROWS DESC",
    "which promo codes fan out",
)
q4 = q(
    "SELECT CODE, COUNT(*) AS ISSUES FROM SANDBOX.SHOP.PROMOS GROUP BY CODE HAVING COUNT(*) > 1",
    "non-unique join keys in PROMOS",
)
q5 = q(
    f"SELECT ORDER_ID, COUNT(*) AS N FROM {T} GROUP BY ORDER_ID HAVING COUNT(*) > 1 "
    "ORDER BY ORDER_ID LIMIT 25",
    "sample of duplicated order ids",
)
q6 = q(
    f"SELECT ORDER_DATE AS DAY, COUNT(*) - COUNT(DISTINCT ORDER_ID) AS INFLATED_ROWS FROM {T} "
    "GROUP BY ORDER_DATE ORDER BY ORDER_DATE",
    "inflation by order day",
)
q7 = q(
    f"SELECT STATUS, COUNT(*) - COUNT(DISTINCT ORDER_ID) AS EXTRA_ROWS FROM {T} GROUP BY STATUS",
    "inflation by order status",
)

add_chart(
    s,
    q6,
    "line",
    "DAY",
    ["INFLATED_ROWS"],
    "Inflated rows by order day",
    note="duplication tracks promo usage, not load time",
)
add_chart(
    s,
    q3,
    "bar",
    "PROMO_CODE",
    ["EXTRA_ROWS"],
    "Duplicated rows by promo code",
    note="the fan-out isolates to two codes",
)

engine.complete_checkpoint(
    s, "validate_expectation", [q1, q2], "3396 rows vs 3000 orders; expectation holds in ORDERS"
)
engine.complete_checkpoint(s, "replicate_anomaly", [q1, q5], "396 order_ids appear twice")
engine.complete_checkpoint(
    s, "scope_blast_radius", [q3, q7], "confined to SUMMER25 and FLASH5; every status affected"
)
engine.complete_checkpoint(s, "upstream_trace", [q3, q4], "both codes were re-issued in PROMOS")

engine.record_finding(
    s,
    {
        "title": "LEFT JOIN to PROMOS fans out on re-issued codes",
        "severity": "high",
        "confidence": "high",
        "affected_objects": [T, "SANDBOX.SHOP.PROMOS"],
        "reproduction": "run q_0003 and q_0004",
        "summary": (
            "WHY THIS MATTERS: every order that used SUMMER25 or FLASH5 appears twice, so "
            "revenue and order counts built on ORDERS_ENRICHED are inflated. "
            "BLAST RADIUS: 396 of 3000 orders, across all statuses and the whole period. "
            "ROOT CAUSE: PROMOS carries two rows for each of those codes (the campaigns were "
            "re-issued), and the enrichment joins on CODE alone."
        ),
        "evidence": [q1, q3, q4],
        "extra": {
            "resolution": "root_caused",
            "blast_radius": "396 orders duplicated (13.2% of rows); SUMMER25 and FLASH5 only",
            "alternatives_tested": "load-time duplication (no clustering by day, q_0006); "
            "status-specific handling (every status affected, q_0007)",
            "root_cause": "non-unique CODE in SANDBOX.SHOP.PROMOS fans out the LEFT JOIN",
        },
        "proposed_remediation": "Dedupe PROMOS on CODE (keep the latest issue) or join on "
        "CODE plus validity window; re-run q_0001 to verify 3000 rows.",
    },
    worker="agent",
)

s.add_intervention(
    "confirm_semantics",
    "Are duplicate promo codes ever legitimate in PROMOS?",
    "Decides whether this is a data defect or a business rule the join must respect.",
    build_request(
        "confirm_semantics",
        {
            "statement": "A promo code identifies exactly one campaign; a re-issued code is a "
            "data defect, not a deliberate second campaign.",
            "context": "SUMMER25 and FLASH5 each have two rows in PROMOS with different "
            "descriptions and discount rates.",
            "sample": [
                {"CODE": "SUMMER25", "DESCRIPTION": "Summer sale", "DISCOUNT_PCT": 25.0},
                {"CODE": "SUMMER25", "DESCRIPTION": "Summer sale (extended)", "DISCOUNT_PCT": 20.0},
            ],
        },
    ),
)

# -- knowledge --------------------------------------------------------------
ks = KnowledgeStore(ws.knowledge_dir)
ks.set_profile(
    T,
    {
        "grain": "one row per ORDER_ID",
        "freshness": "rebuilt nightly from ORDERS ⟕ PROMOS",
        "owners": ["shop-data"],
        "relationships": [
            {"to": "SANDBOX.SHOP.ORDERS", "on": "ORDER_ID", "cardinality": "one-to-one"},
            {"to": "SANDBOX.SHOP.PROMOS", "on": "PROMO_CODE = CODE", "cardinality": "many-to-one"},
        ],
    },
)
ks.set_profile(
    "SANDBOX.SHOP.ORDERS",
    {
        "grain": "one row per ORDER_ID",
        "freshness": "appended continuously; complete by 06:00 UTC",
        "relationships": [
            {"to": "SANDBOX.SHOP.CUSTOMERS", "on": "CUSTOMER_ID", "cardinality": "many-to-one"},
        ],
    },
)
ks.set_profile("SANDBOX.SHOP.PROMOS", {"grain": "one row per PROMO_CODE (intended)"})
ks.add_fact(T, "AMOUNT is gross of discount; DISCOUNT_PCT is informational")
ks.add_fact("SANDBOX.SHOP.ORDERS", "STATUS 'refunded' orders keep their original AMOUNT")

# -- external checks --------------------------------------------------------
checks = {
    "results": [
        {
            "check_id": "customers_email_not_null",
            "status": "fail",
            "run_at": "2026-09-02T06:00:00Z",
            "name": "CUSTOMERS.EMAIL not null",
            "tables": ["SANDBOX.SHOP.CUSTOMERS"],
            "source": "dbt",
            "details": "86 rows with NULL email, all signed up on or after 2026-07-15",
            "ttl_hours": 26,
        },
        {
            "check_id": "orders_enriched_row_parity",
            "status": "fail",
            "run_at": "2026-09-02T06:00:00Z",
            "name": "ORDERS_ENRICHED row parity vs ORDERS",
            "tables": [T],
            "source": "airflow",
            "details": "3396 rows vs 3000 source orders — 396 unexplained extras",
            "metrics": {"expected": 3000, "actual": 3396},
            "sql": f"SELECT (SELECT COUNT(*) FROM {T}) - "
            "(SELECT COUNT(*) FROM SANDBOX.SHOP.ORDERS)",
            "ttl_hours": 26,
        },
        {
            "check_id": "payments_amount_positive",
            "status": "pass",
            "run_at": "2026-09-02T06:00:00Z",
            "name": "PAYMENTS amounts positive",
            "tables": ["SANDBOX.SHOP.PAYMENTS"],
            "source": "dbt",
            "ttl_hours": 26,
        },
        {
            "check_id": "promos_code_unique",
            "status": "pass",
            "run_at": "2026-08-27T06:00:00Z",
            "name": "PROMOS.CODE unique",
            "tables": ["SANDBOX.SHOP.PROMOS"],
            "source": "airflow",
            "ttl_hours": 26,
        },
    ]
}
cj = tmp / "checks.json"
cj.write_text(json.dumps(checks), encoding="utf-8")
ChecksStore(ws.checks_dir).ingest(cj)

# -- team workflow ----------------------------------------------------------
create_workflow(ws.workflows_dir, "orders-slim-health", fork_of="table-health", user_id="kcg")

# -- serve + shoot ----------------------------------------------------------
import uvicorn  # noqa: E402

app = build_app(ws, token=TOKEN)
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/?t={TOKEN}", timeout=1).read()
        break
    except Exception:
        time.sleep(0.1)

PAGES = [
    ("session", f"/session/{s.id}", 1500),
    ("knowledge", "/knowledge", 1120),
    ("checks", "/checks", 1120),
    ("workflows", "/workflows", 1500),
    ("workflow_detail", "/workflows/bug-hunter", 1120),
    ("settings", "/settings", 1000),
]
for name, path, height in PAGES:
    for theme in ("light", "dark"):
        args = [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size=1440,{height}",
            "--virtual-time-budget=6000",
            f"--screenshot={OUT / f'{name}_{theme}.png'}",
        ]
        if theme == "dark":
            args.append("--force-dark-mode")
        args.append(f"http://127.0.0.1:{PORT}{path}?t={TOKEN}")
        subprocess.run(args, check=True, capture_output=True, timeout=120)
        print("shot", name, theme)
server.should_exit = True
shutil.rmtree(tmp, ignore_errors=True)
print("wrote", len(PAGES) * 2, "screenshots to", OUT)
