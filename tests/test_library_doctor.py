"""`grayson library doctor`: drift surfaces on demand instead of accumulating."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from grayson.cli import app
from grayson.knowledge import KnowledgeStore
from grayson.library import library_doctor

runner = CliRunner()


@pytest.fixture
def ks(workspace):
    return KnowledgeStore(workspace.knowledge_dir)


def _doc_path(workspace, table="DB.S.T1"):
    db, schema, name = table.split(".")
    path = workspace.knowledge_dir / db / schema / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_healthy_library_is_ok(workspace, ks):
    ks.add_fact("DB.S.T1", "amounts are gross, not net")
    report = library_doctor(workspace)
    assert report["ok"] is True
    assert report["knowledge"] == {
        "ok": True,
        "checked": 1,
        "errors": [],
        "warnings": [],
        "unstamped": [],
    }
    assert report["records"]["ok"] is True


def test_broken_doc_is_an_error_naming_the_file(workspace, ks):
    _doc_path(workspace).write_text("---\ntable: [unclosed\n---\n", encoding="utf-8")
    report = library_doctor(workspace)
    assert report["ok"] is False
    assert report["knowledge"]["errors"][0]["file"] == "DB/S/T1.md"


def test_duplicate_fact_ids_are_an_error(workspace, ks):
    _doc_path(workspace).write_text(
        "---\ntable: DB.S.T1\nfacts:\n"
        "- {id: f1, fact: a, status: proposed}\n"
        "- {id: f1, fact: b, status: proposed}\n"
        "---\n",
        encoding="utf-8",
    )
    report = library_doctor(workspace)["knowledge"]
    assert report["ok"] is False
    assert "duplicate fact id" in report["errors"][0]["problem"]


def test_newer_format_and_moved_file_warn_but_do_not_fail(workspace, ks):
    _doc_path(workspace).write_text(
        "---\ntable: DB.S.OTHER\nformat: 99\nfacts: []\n---\n", encoding="utf-8"
    )
    report = library_doctor(workspace)
    knowledge = report["knowledge"]
    assert knowledge["ok"] is True  # warnings mean working-but-look, not broken
    problems = " | ".join(w["problem"] for w in knowledge["warnings"])
    assert "newer than this grayson writes" in problems
    assert "disagrees with the path" in problems


def test_unstamped_docs_are_reported_as_informational(workspace, ks):
    _doc_path(workspace).write_text("---\ntable: DB.S.T1\nfacts: []\n---\n", encoding="utf-8")
    knowledge = library_doctor(workspace)["knowledge"]
    assert knowledge["ok"] is True
    assert knowledge["unstamped"] == ["DB.S.T1"]


def test_misplaced_doc_warns(workspace, ks):
    stray = workspace.knowledge_dir / "notes.md"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("scratch\n", encoding="utf-8")
    knowledge = library_doctor(workspace)["knowledge"]
    assert any("invisible" in w["problem"] for w in knowledge["warnings"])


def test_broken_record_json_is_an_error(workspace, ks):
    rec = workspace.records_dir / "s1" / "f_001.json"
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text("{not json", encoding="utf-8")
    ok = workspace.records_dir / "s1" / "f_002.json"
    ok.write_text(json.dumps({"kind": "finding", "id": "f_002"}), encoding="utf-8")
    records = library_doctor(workspace)["records"]
    assert records["checked"] == 2
    assert records["ok"] is False
    assert records["errors"][0]["file"] == "s1/f_001.json"


def test_cli_exits_nonzero_on_errors(workspace, ks):
    result = runner.invoke(app, ["library", "doctor"])
    assert result.exit_code == 0, result.output
    _doc_path(workspace).write_text("---\ntable: [unclosed\n---\n", encoding="utf-8")
    result = runner.invoke(app, ["library", "doctor"])
    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False


def test_newer_record_format_is_flagged_not_silently_served(workspace):
    rec = workspace.records_dir / "s1" / "f_0001.json"
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(
        json.dumps({"kind": "finding", "format": 2, "id": "f_0001", "title": "x"}),
        encoding="utf-8",
    )
    report = library_doctor(workspace)
    assert report["ok"] is False
    assert "newer" in report["records"]["errors"][0]["problem"]
