from __future__ import annotations

import pytest

from grayson.views import ViewEntry, ViewRegistry


@pytest.fixture
def reg(workspace):
    return ViewRegistry(workspace.views_dir)


def test_empty_registry(reg):
    assert reg.list() == []


def test_register_and_get(reg):
    entry = ViewEntry(
        name="QA_PAGE_EVENTS",
        purpose="analysis-ready page events",
        source_tables=["ANALYTICS.WEB.PAGE_EVENTS"],
        base_files=["models/page_events.sql"],
        source_last_altered={"ANALYTICS.WEB.PAGE_EVENTS": "2026-08-20 00:00:00"},
    )
    saved = reg.register(entry, ddl="CREATE VIEW QA_PAGE_EVENTS AS SELECT * FROM ...")
    assert saved.ddl_path == "ddl/qa_page_events.sql"
    assert (reg.dir / "ddl" / "qa_page_events.sql").is_file()
    got = reg.get("qa_page_events")
    assert got is not None and got.base_files == ["models/page_events.sql"]


def test_register_replaces_same_name(reg):
    reg.register(ViewEntry(name="V", purpose="a"))
    reg.register(ViewEntry(name="V", purpose="b"))
    assert len(reg.list()) == 1 and reg.get("V").purpose == "b"


def test_matching(reg):
    reg.register(ViewEntry(name="V1", source_tables=["DB.S.A"]))
    reg.register(ViewEntry(name="V2", source_tables=["DB.S.B"]))
    assert {v.name for v in reg.matching(["DB.S.A"])} == {"V1"}


def test_coverage_reuse(reg):
    reg.register(
        ViewEntry(
            name="V1",
            source_tables=["DB.S.A"],
            source_last_altered={"DB.S.A": "2026-08-20 00:00:00"},
        )
    )
    cov = reg.coverage_check(["DB.S.A"], {"DB.S.A": "2026-08-20 00:00:00"})
    assert len(cov["reuse"]) == 1 and not cov["refresh"] and cov["fully_covered"]


def test_coverage_refresh_on_staleness(reg):
    reg.register(
        ViewEntry(
            name="V1",
            source_tables=["DB.S.A"],
            base_files=["m/a.sql"],
            source_last_altered={"DB.S.A": "2026-08-20 00:00:00"},
        )
    )
    cov = reg.coverage_check(["DB.S.A"], {"DB.S.A": "2026-08-21 09:00:00"})
    assert not cov["reuse"] and len(cov["refresh"]) == 1
    assert "changed" in cov["refresh"][0]["reasons"][0]
    assert cov["refresh"][0]["base_files"] == ["m/a.sql"]


def test_coverage_gaps(reg):
    reg.register(ViewEntry(name="V1", source_tables=["DB.S.A"]))
    cov = reg.coverage_check(["DB.S.A", "DB.S.B"])
    assert cov["gaps"] == ["DB.S.B"] and not cov["fully_covered"]


def test_unknown_fields_round_trip_through_a_rewrite(reg):
    # The docs/LIBRARY.md round-trip contract: what a newer grayson (or a hand
    # edit) wrote survives this version's full-registry rewrite — entry fields
    # and top-level keys alike.
    import yaml

    reg.register(ViewEntry(name="V1", purpose="a"))
    data = yaml.safe_load(reg.registry_path.read_text(encoding="utf-8"))
    data["views"][0]["freshness_sla"] = "hourly"  # a newer grayson's entry field
    data["team_notes"] = "hand-maintained"  # unknown top-level key
    reg.registry_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    reg.register(ViewEntry(name="V2", purpose="b"))  # triggers a full rewrite
    saved = yaml.safe_load(reg.registry_path.read_text(encoding="utf-8"))
    v1 = next(v for v in saved["views"] if v["name"] == "V1")
    assert v1["freshness_sla"] == "hourly"
    assert saved["team_notes"] == "hand-maintained"
