"""Findings schemas: closed-ended structures every finding must satisfy.

Findings are the deterministic QA-of-QA output gate — a finding cannot be
recorded unless it validates against its workflow's schema AND cites evidence
(executed query ids). The evidence-existence check is enforced by the engine
(it needs the session); this module owns structural validation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

SEVERITIES = ["critical", "high", "medium", "low", "info"]
CONFIDENCES = ["high", "medium", "low"]


class Finding(BaseModel):
    """Base finding shape shared by all schemas."""

    schema_name: str = "standard_v1"
    title: str = Field(min_length=3)
    severity: str
    confidence: str
    summary: str = Field(min_length=10)
    affected_objects: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(min_length=1, description="Executed query ids (q_XXXX).")
    reproduction: str = ""
    proposed_remediation: str = ""
    open_questions: list[str] = Field(default_factory=list)
    extra: dict = Field(default_factory=dict)
    #: fid of an earlier finding this one corrects. A proposal only: the actual
    #: supersession executes inside the user's accept action, never agent-side.
    supersedes: str | None = None

    @field_validator("severity")
    @classmethod
    def _sev(cls, v: str) -> str:
        if v not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}")
        return v

    @field_validator("confidence")
    @classmethod
    def _conf(cls, v: str) -> str:
        if v not in CONFIDENCES:
            raise ValueError(f"confidence must be one of {CONFIDENCES}")
        return v


# Schema-specific required fields (beyond the base), enforced against `extra`.
FINDINGS_SCHEMAS: dict[str, dict] = {
    "standard_v1": {"required_extra": []},
    "bug_hunter_v1": {
        "required_extra": [
            ("root_cause", "The isolated cause of the anomaly."),
            ("blast_radius", "Quantified scope: rows/keys/partitions affected."),
            ("alternatives_tested", "Competing explanations tested and ruled out."),
        ]
    },
    "parity_v1": {
        "required_extra": [
            ("old_object", "Baseline object compared."),
            ("new_object", "Candidate object compared."),
            ("parity_result", "pass | fail with the quantified differences."),
        ]
    },
}


def validate_finding(payload: dict, schema_name: str) -> Finding:
    """Structural validation. Raises pydantic ValidationError or ValueError."""
    if schema_name not in FINDINGS_SCHEMAS:
        known = ", ".join(sorted(FINDINGS_SCHEMAS))
        raise ValueError(f"unknown findings schema '{schema_name}' (known: {known})")
    data = dict(payload)
    data["schema_name"] = schema_name
    finding = Finding.model_validate(data)
    missing = [
        f"{key} ({desc})"
        for key, desc in FINDINGS_SCHEMAS[schema_name]["required_extra"]
        if not finding.extra.get(key)
    ]
    if missing:
        raise ValueError(
            f"findings schema '{schema_name}' requires these fields in `extra`: "
            + "; ".join(missing)
        )
    return finding
