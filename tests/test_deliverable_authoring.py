import json
from base64 import b64decode, b64encode
from html.parser import HTMLParser
from pathlib import Path

import pytest

from conftest import FakeExecutor, call_mcp
from grayson.config import GuardSettings
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.deliverable_authoring import Presentation, resolve_presentation
from grayson.deliverables import export_deliverable
from grayson.mcp.server import build_server


@pytest.fixture
def source(workspace):
    session = Session.create(
        workspace,
        workflow="table-health",
        targets=["DB.S.T1"],
        guard=GuardSettings(auto_limit=0),
        guard_profile="moderate",
    )
    qid = run_statement(
        session,
        "SELECT REGION, REVENUE FROM DB.S.T1",
        executor=FakeExecutor(
            rows=[
                {"REGION": "North", "REVENUE": 120},
                {"REGION": "</script><script>alert(1)</script>", "REVENUE": 80},
            ]
        ),
    )["qid"]
    spec = {
        "html": '<h1>Revenue review</h1><svg id="revenue"></svg>',
        "css": "body { background: #fff; }",
        "javascript": "document.querySelector('svg').dataset.rows = grayson.data('sales').length;",
        "summary": "Revenue review; inspect the regional differences before deciding.",
        "datasets": {"sales": {"qid": qid, "columns": ["REGION", "REVENUE"]}},
        "visuals": {"revenue": {"title": "Regional revenue", "datasets": ["sales"]}},
    }
    return session, spec


def test_authored_mcp_exports_real_data_and_curated_companion(source):
    session, spec = source
    result = call_mcp(
        build_server(session.workspace),
        "session_deliverable",
        {
            "session_id": session.id,
            "title": "Regional performance",
            "presentation": spec,
        },
    )
    assert result["mode"] == "authored"
    manifest = json.loads(Path(result["evidence"]).read_text())
    dataset = manifest["datasets"]["sales"]
    assert dataset["rows"][0] == {"REGION": "North", "REVENUE": 120}
    assert dataset["evidence"]["sql"] == "SELECT REGION, REVENUE FROM DB.S.T1"
    assert dataset["evidence"]["session_id"] == session.id
    assert len(dataset["sha256"]) == 64
    assert Presentation.model_validate(spec).model_dump() == json.loads(
        Path(result["presentation"]).read_text()
    )
    markdown = Path(result["markdown"]).read_text(encoding="utf-8")
    assert spec["summary"] in markdown
    assert "## Checkpoints" not in markdown
    assert "## Checkpoints" in Path(result["session_markdown"]).read_text(encoding="utf-8")
    html = Path(result["html"]).read_text(encoding="utf-8")
    assert 'sandbox="allow-scripts"' in html
    assert "allow-same-origin" not in html
    assert "\\u003c/script\\u003e" in html
    assert b64encode(spec["javascript"].encode()).decode() in html
    assert session.stage != "closed"


def test_freeform_source_preserves_raw_text_closing_tags(source):
    session, spec = source
    spec["javascript"] = 'const template = "</ScRiPt><b>£</b>"; // </script>\n'
    spec["css"] = '.label::after { content: "</StYlE> ✓"; } /* </style> */'
    result = export_deliverable(session, presentation=spec)

    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.elements = []

        def handle_starttag(self, tag, attrs):
            self.elements.append((tag, dict(attrs)))

    outer = Elements()
    outer.feed(Path(result["html"]).read_text(encoding="utf-8"))
    document = next(attrs["srcdoc"] for tag, attrs in outer.elements if tag == "iframe")
    inner = Elements()
    inner.feed(document)
    scripts = [attrs for tag, attrs in inner.elements if tag == "script"]
    assert len(scripts) == 3  # evidence JSON, fixed runtime, authored source
    assert not any(tag == "b" for tag, _ in inner.elements)
    script = next(attrs["src"] for attrs in scripts if "src" in attrs)
    css = next(attrs["href"] for tag, attrs in inner.elements if tag == "link")
    assert b64decode(script.split(",", 1)[1]).decode() == spec["javascript"]
    assert b64decode(css.split(",", 1)[1]).decode() == spec["css"]


@pytest.mark.parametrize(
    "change, message",
    [
        ({"qid": "q_9999"}, "successfully executed"),
        ({"columns": ["invented"]}, "columns"),
        ({"columns": ["REGION", "REGION"]}, "columns"),
        ({"rows": [{"REVENUE": 999}]}, "Extra inputs"),
        ({"max_rows": 0}, "greater than"),
    ],
)
def test_invalid_data_bindings_fail_before_export(source, change, message):
    session, spec = source
    spec["datasets"]["sales"].update(change)
    with pytest.raises(ValueError, match=message):
        export_deliverable(session, presentation=spec)
    assert not (session.dir / "deliverables").exists()


def test_purged_evidence_rejected(source):
    session, spec = source
    session.cache.drop_all_data()
    with pytest.raises(ValueError, match="removed"):
        resolve_presentation(session, spec)


def test_query_status_and_sql_match_required(source):
    session, spec = source
    qid = spec["datasets"]["sales"]["qid"]
    session.update_query(qid, status="error")
    with pytest.raises(ValueError, match="successfully executed"):
        resolve_presentation(session, spec)
    session.update_query(qid, status="executed", sql_executed="SELECT 1")
    with pytest.raises(ValueError, match="SQL does not match"):
        resolve_presentation(session, spec)


def test_projection_and_limit_remain_visible(source):
    session, spec = source
    spec["datasets"]["sales"].update(columns=["REVENUE"], max_rows=1)
    session.update_query(spec["datasets"]["sales"]["qid"], truncated=1)
    _, manifest = resolve_presentation(session, spec)
    data = manifest["datasets"]["sales"]
    assert data["rows"] == [{"REVENUE": 120}]
    assert data["export_limited"] and data["source_truncated"]
    assert data["cached_rows"] == 2


def test_visual_must_reference_supplied_dataset(source):
    session, spec = source
    spec["visuals"]["revenue"]["datasets"] = ["invented"]
    with pytest.raises(ValueError, match="unknown dataset"):
        resolve_presentation(session, spec)


@pytest.mark.parametrize(
    "html", ["<div>No anchor</div>", '<i id="revenue"></i><b id="revenue"></b>']
)
def test_visual_requires_unique_container(source, html):
    session, spec = source
    spec["html"] = html
    with pytest.raises(ValueError, match="exactly one HTML element"):
        resolve_presentation(session, spec)


def test_cache_count_must_match_query_audit(source):
    session, spec = source
    session.update_query(spec["datasets"]["sales"]["qid"], row_count=100)
    with pytest.raises(ValueError, match="row count differs"):
        resolve_presentation(session, spec)


def test_empty_result_is_valid_evidence(source):
    session, spec = source
    qid = run_statement(
        session,
        "SELECT * FROM DB.S.T1 WHERE 1=0",
        executor=FakeExecutor(rows=[]),
    )["qid"]
    spec["datasets"]["sales"] = {"qid": qid}
    _, manifest = resolve_presentation(session, spec)
    assert manifest["datasets"]["sales"]["rows"] == []
