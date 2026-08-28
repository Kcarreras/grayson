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
        "reproduction": "SELECT PK, COUNT(*) FROM DB.S.T GROUP BY PK HAVING COUNT(*) > 1",
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
        validate_finding(base(extra={"resolution": "root_caused"}), "bug_hunter_v1")


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
            "reproduction": "re-run the cited query",
            "summary": "Duplication reproduces and is bounded, but no stage owns it yet.",
            "affected_objects": ["DB.S.ORDERS_ENRICHED"],
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
                "affected_objects": ["DB.S.T1"],
                "reproduction": "re-run the cited query",
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
                "affected_objects": ["DB.S.T1"],
                "reproduction": "re-run the cited query",
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
                "affected_objects": ["DB.S.T1"],
                "reproduction": "re-run the cited query",
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


def test_feature_readiness_requires_saying_what_leakage_testing_found():
    with pytest.raises(ValueError, match="leakage_assessment"):
        validate_finding(
            {
                "title": "Training set is fine",
                "severity": "info",
                "confidence": "high",
                "affected_objects": ["DB.S.T1"],
                "reproduction": "re-run the cited query",
                "summary": "Looks usable for the churn model.",
                "evidence": ["q_0001"],
                "extra": {
                    "row_grain": "one row per customer-month",
                    "label_definition": "churned within 30 days",
                    "readiness_verdict": "ready",
                },
            },
            "feature_readiness_v1",
        )


def test_feature_readiness_accepts_a_complete_assessment():
    f = validate_finding(
        {
            "title": "Leakage via post-hoc status column",
            "severity": "critical",
            "confidence": "high",
            "summary": "ACCOUNT_STATUS is written after the churn event it predicts.",
            "affected_objects": ["DB.ML.CHURN_FEATURES.ACCOUNT_STATUS"],
            "evidence": ["q_0001"],
            "reproduction": "compare ACCOUNT_STATUS.updated_at against LABEL_EVENT_AT",
            "extra": {
                "row_grain": "one row per customer-month, 2024-01 to 2026-06",
                "label_definition": "churned within 30 days of the month end",
                "leakage_assessment": "ACCOUNT_STATUS updated after the label event; "
                "no entity overlap across the temporal split",
                "readiness_verdict": "not_ready",
            },
        },
        "feature_readiness_v1",
    )
    assert f.extra["readiness_verdict"] == "not_ready"


# -- calibration: the top rungs cost what a real severe finding has -------


def test_high_confidence_requires_a_reproduction():
    """If nobody else can go and see it, it is not high confidence."""
    with pytest.raises(ValueError, match="requires a `reproduction`"):
        validate_finding(base(reproduction=""), "standard_v1")


def test_medium_confidence_needs_no_reproduction():
    f = validate_finding(base(confidence="medium", reproduction=""), "standard_v1")
    assert f.confidence == "medium"


@pytest.mark.parametrize("severity", ["critical", "high"])
def test_severe_findings_must_name_what_they_affect(severity):
    with pytest.raises(ValueError, match="requires `affected_objects`"):
        validate_finding(base(severity=severity, affected_objects=[]), "standard_v1")


@pytest.mark.parametrize("severity", ["medium", "low", "info"])
def test_lesser_severities_are_not_forced_to_name_objects(severity):
    assert validate_finding(base(severity=severity, affected_objects=[]), "standard_v1")


def test_schema_gaps_are_reported_before_calibration_ones():
    """A finding missing both should hear about the workflow's demand first — it is
    the more substantive gap."""
    with pytest.raises(ValueError, match="stage_boundary"):
        validate_finding(base(reproduction="", extra={}), "pipeline_qa_v1")


def test_rubric_is_publishable():
    from grayson.findings.schemas import SEVERITIES, rubric

    scale = rubric()
    assert set(scale["severity"]) == set(SEVERITIES)
    assert scale["enforced"], "the rubric must say which parts are actually enforced"


# -- pipeline_qa_v1 / rule_qa_v1 -----------------------------------------


def test_pipeline_finding_must_say_where_in_the_pipeline():
    with pytest.raises(ValueError, match="stage_boundary"):
        validate_finding(base(extra={"quantified_impact": "1200 rows"}), "pipeline_qa_v1")
    f = validate_finding(
        base(extra={"stage_boundary": "staging -> mart", "quantified_impact": "1200 rows"}),
        "pipeline_qa_v1",
    )
    assert f.extra["stage_boundary"] == "staging -> mart"


def test_accuracy_estimate_must_carry_its_sampling_design():
    """A bare percentage from an edge-weighted sample reads as an overall accuracy
    and is not one — the frame and size are what make it a measurement."""
    with pytest.raises(ValueError, match="sampling_frame"):
        validate_finding(
            base(
                extra={
                    "finding_kind": "accuracy_estimate",
                    "sample_size": "200",
                    "accuracy": "91%",
                    "error_modes": "news vs blog confused",
                }
            ),
            "rule_qa_v1",
        )


def test_rule_defect_needs_no_sampling_design():
    """Not every semantic-rule finding is an accuracy estimate — an unreachable
    category is a defect with no sample behind it at all."""
    f = validate_finding(
        base(
            extra={
                "finding_kind": "rule_defect",
                "rule_location": "categorize_url() regex branch 4",
                "observed_behaviour": "branch never fires; 0 rows assigned",
                "expected_behaviour": "should match vendor subdomains, per the taxonomy doc",
            }
        ),
        "rule_qa_v1",
    )
    assert f.extra["finding_kind"] == "rule_defect"


def test_unknown_rule_finding_kind_rejected():
    with pytest.raises(ValueError, match="finding_kind must be one of"):
        validate_finding(base(extra={"finding_kind": "vibes"}), "rule_qa_v1")
