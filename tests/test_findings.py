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
