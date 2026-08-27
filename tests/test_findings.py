from __future__ import annotations

import pytest
from pydantic import ValidationError

from grayson.findings.schemas import validate_finding


def base(**over):
    payload = {
        "title": "Duplicate keys in output",
        "severity": "high",
        "confidence": "high",
        "summary": "The output table has duplicate primary keys.",
        "affected_objects": ["DB.S.T"],
        "evidence": ["q_0001", "q_0002"],
    }
    payload.update(over)
    return payload


def test_standard_valid():
    f = validate_finding(base(), "standard_v1")
    assert f.severity == "high"


def test_evidence_required():
    with pytest.raises(ValidationError):
        validate_finding(base(evidence=[]), "standard_v1")


def test_bad_severity():
    with pytest.raises(ValidationError):
        validate_finding(base(severity="catastrophic"), "standard_v1")


def test_short_summary_rejected():
    with pytest.raises(ValidationError):
        validate_finding(base(summary="short"), "standard_v1")


def test_bug_hunter_requires_extra():
    with pytest.raises(ValueError, match="root_cause"):
        validate_finding(base(), "bug_hunter_v1")


def test_bug_hunter_valid_with_extra():
    f = validate_finding(
        base(
            extra={
                "resolution": "root_caused",
                "root_cause": "join fan-out",
                "blast_radius": "1200 rows since 2026-08-01",
                "alternatives_tested": "dedup bug ruled out; source dup ruled out",
            }
        ),
        "bug_hunter_v1",
    )
    assert f.extra["root_cause"] == "join fan-out"


def test_parity_requires_extra():
    with pytest.raises(ValueError, match="parity_result"):
        validate_finding(base(), "parity_v1")


def test_unknown_schema():
    with pytest.raises(ValueError, match="unknown findings schema"):
        validate_finding(base(), "nope_v9")


def test_inconclusive_is_a_valid_bug_hunter_result():
    """Reproducing and bounding an anomaly without isolating a cause is a real
    outcome; requiring root_cause unconditionally just gets one invented."""
    f = validate_finding(
        {
            "title": "Order ids duplicate after Tuesday",
            "severity": "high",
            "confidence": "medium",
            "summary": "Duplication reproduces and is bounded, but no stage owns it yet.",
            "evidence": ["q_0001"],
            "extra": {
                "resolution": "inconclusive",
                "blast_radius": "412 rows across 2 partitions",
                "alternatives_tested": "source duplication and promo join both ruled out",
                "remaining_hypotheses": "late-arriving CDC replay; needs the raw ingest log",
            },
        },
        "bug_hunter_v1",
    )
    assert f.extra["resolution"] == "inconclusive"


def test_inconclusive_still_requires_saying_what_is_open():
    with pytest.raises(ValueError, match="remaining_hypotheses"):
        validate_finding(
            {
                "title": "Something is off",
                "severity": "high",
                "confidence": "low",
                "summary": "Could not work out what is happening here at all.",
                "evidence": ["q_0001"],
                "extra": {
                    "resolution": "inconclusive",
                    "blast_radius": "unknown",
                    "alternatives_tested": "none",
                },
            },
            "bug_hunter_v1",
        )


def test_missing_resolution_names_both_routes():
    with pytest.raises(ValueError, match="Inconclusive is a legitimate result"):
        validate_finding(
            {
                "title": "Duplicate order ids",
                "severity": "high",
                "confidence": "high",
                "summary": "Order ids repeat after the promo join.",
                "evidence": ["q_0001"],
                "extra": {"blast_radius": "412", "alternatives_tested": "two"},
            },
            "bug_hunter_v1",
        )


def test_unknown_resolution_rejected():
    with pytest.raises(ValueError, match="must be one of"):
        validate_finding(
            {
                "title": "Duplicate order ids",
                "severity": "high",
                "confidence": "high",
                "summary": "Order ids repeat after the promo join.",
                "evidence": ["q_0001"],
                "extra": {
                    "resolution": "probably_fine",
                    "blast_radius": "412",
                    "alternatives_tested": "two",
                },
            },
            "bug_hunter_v1",
        )
