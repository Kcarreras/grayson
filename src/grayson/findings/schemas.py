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


#: `bug_hunter_v1` resolutions. An investigation that reproduces an anomaly,
#: bounds it, and honestly cannot isolate a cause is a real result — and it used
#: to have no valid output here, because `root_cause` was unconditionally
#: required. A schema that only accepts a confident answer will get one invented.
BUG_HUNTER_RESOLUTIONS = ["root_caused", "inconclusive"]

# Schema-specific required fields (beyond the base), enforced against `extra`.
FINDINGS_SCHEMAS: dict[str, dict] = {
    "standard_v1": {"required_extra": []},
    "bug_hunter_v1": {
        "required_extra": [
            ("resolution", f"One of: {' | '.join(BUG_HUNTER_RESOLUTIONS)}."),
            ("blast_radius", "Quantified scope: rows/keys/partitions affected."),
            ("alternatives_tested", "Competing explanations tested and ruled out."),
        ],
        "conditional_extra": {
            "root_caused": [("root_cause", "The isolated cause of the anomaly.")],
            "inconclusive": [
                (
                    "remaining_hypotheses",
                    "What is still open, and what evidence would settle it.",
                )
            ],
        },
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
    spec = FINDINGS_SCHEMAS[schema_name]
    required = list(spec["required_extra"])
    conditional = spec.get("conditional_extra") or {}
    if conditional:
        # the discriminator picks which extra fields apply; validate it first so
        # the error names the real problem instead of cascading
        resolution = str(finding.extra.get("resolution") or "").strip()
        if not resolution:
            raise ValueError(
                f"findings schema '{schema_name}' requires extra.resolution: "
                "'root_caused' if you isolated the cause, or 'inconclusive' if you "
                "reproduced and bounded the anomaly but could not isolate it. "
                "Inconclusive is a legitimate result — record it rather than "
                "asserting a cause you cannot evidence."
            )
        if resolution not in conditional:
            raise ValueError(
                f"findings schema '{schema_name}': resolution must be one of "
                f"{list(conditional)}, got '{resolution}'"
            )
        required += conditional[resolution]
    missing = [f"{key} ({desc})" for key, desc in required if not finding.extra.get(key)]
    if missing:
        raise ValueError(
            f"findings schema '{schema_name}' requires these fields in `extra`: "
            + "; ".join(missing)
        )
    return finding
