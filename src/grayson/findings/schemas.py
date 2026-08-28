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

#: What each severity is supposed to mean. Without a shared scale every finding
#: drifts upward — an agent with no rubric has no reason to say "low", and a
#: queue where everything is high is a queue with no priority in it. grayson
#: cannot judge whether a severity is *right*; it can publish the scale, and it
#: can make the higher rungs cost the specificity a real severe finding has
#: anyway (see `_check_calibration`).
SEVERITY_RUBRIC: dict[str, str] = {
    "critical": (
        "Wrong data is already being used for decisions, or will be before anyone "
        "notices. Silent corruption of a reported measure, a broken key that "
        "fans out downstream, a pipeline dropping rows on every run."
    ),
    "high": (
        "A real defect with material scope, but bounded or not yet consumed — a "
        "column wrong for one segment, a regression starting on a known date."
    ),
    "medium": (
        "A genuine defect with limited blast radius or a workaround in place; "
        "worth fixing on the normal cycle, not tonight."
    ),
    "low": (
        "A defect nobody is currently harmed by: cosmetic inconsistency, dead "
        "column, a stale definition that matches reality by accident."
    ),
    "info": (
        "Not a defect. Something the next investigation should know — a "
        "confirmed expectation, a documented quirk, a ruled-out hypothesis."
    ),
}

#: Confidence is about the *evidence*, not about how the finding feels. A
#: high-confidence claim is one someone else can go and see for themselves,
#: which is why that rung requires a reproduction.
CONFIDENCE_RUBRIC: dict[str, str] = {
    "high": (
        "Demonstrated, and reproducible by someone else from what you wrote down. "
        "Requires a `reproduction`."
    ),
    "medium": (
        "Well evidenced, but resting on a sample, a local statistic, or an "
        "inference the warehouse did not directly confirm."
    ),
    "low": ("A lead worth recording — suggestive evidence, alternatives not yet excluded."),
}

#: severities that have to name what they affect. A severe finding that cannot
#: say which objects are involved is a severity nobody can act on or check.
_SEVERITIES_NEEDING_OBJECTS = ("critical", "high")


def rubric() -> dict:
    """The calibration scale, for agents and the console to display."""
    return {
        "severity": SEVERITY_RUBRIC,
        "confidence": CONFIDENCE_RUBRIC,
        "enforced": [
            "confidence 'high' requires a non-empty `reproduction` — if nobody else "
            "can go and see it, it is not high confidence",
            "severity 'critical' or 'high' requires non-empty `affected_objects` — a "
            "severe finding that cannot name what it affects cannot be acted on",
        ],
        "note": (
            "grayson does not judge whether your severity is correct; that is the "
            "user's call when they accept or reject. It publishes the scale and makes "
            "the top rungs cost the specificity a real severe finding already has."
        ),
    }


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
        "discriminator": "resolution",
        "discriminator_hint": (
            "'root_caused' if you isolated the cause, or 'inconclusive' if you "
            "reproduced and bounded the anomaly but could not isolate it. Inconclusive "
            "is a legitimate result — record it rather than asserting a cause you "
            "cannot evidence."
        ),
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
    "feature_readiness_v1": {
        "required_extra": [
            ("row_grain", "One row per what, over what population and period."),
            ("label_definition", "The target column and what it means at prediction time."),
            (
                "leakage_assessment",
                "What was checked for leakage and point-in-time correctness, and what "
                "was found. 'not assessed' is not an acceptable value — say what you "
                "tested, even if the answer is that nothing leaked.",
            ),
            (
                "readiness_verdict",
                "ready | ready_with_caveats | not_ready, with the reason.",
            ),
        ]
    },
    "pipeline_qa_v1": {
        "required_extra": [
            (
                "stage_boundary",
                "Between which two pipeline stages the defect appears (or the single "
                "stage that introduces it). A pipeline finding that does not say where "
                "cannot be acted on.",
            ),
            (
                "quantified_impact",
                "How many rows, keys, or how much of which measure is affected — with "
                "the numbers, not 'some'.",
            ),
        ]
    },
    "rule_qa_v1": {
        "required_extra": [
            ("finding_kind", f"One of: {' | '.join(('accuracy_estimate', 'rule_defect'))}."),
        ],
        "conditional_extra": {
            # an accuracy number without its sampling design is not a measurement,
            # and "about 90%" from an edge-weighted sample reads as an overall
            # accuracy while being nothing of the kind
            "accuracy_estimate": [
                ("sample_size", "How many rows a human actually labelled."),
                (
                    "sampling_frame",
                    "Which population the sample was drawn from and how strata were "
                    "weighted. An edge-weighted sample estimates error modes well and "
                    "overall accuracy badly — say which this is.",
                ),
                (
                    "accuracy",
                    "The estimate with its interval, stated against the frame above.",
                ),
                ("error_modes", "Which categories are confused, and in which direction."),
            ],
            "rule_defect": [
                ("rule_location", "Where the rule is implemented (column, transform, regex)."),
                ("observed_behaviour", "What the rule does."),
                ("expected_behaviour", "What it should do, and on whose authority."),
            ],
        },
        "discriminator": "finding_kind",
    },
    "parity_v1": {
        "required_extra": [
            ("old_object", "Baseline object compared."),
            ("new_object", "Candidate object compared."),
            ("parity_result", "pass | fail with the quantified differences."),
        ]
    },
}


def _check_calibration(finding: Finding) -> None:
    """Make the top rungs of the scale cost what a real severe finding already has.

    grayson makes no judgement about whether a severity is *correct* — that is the
    user's call when they accept or reject. But with no rubric and no cost, every
    finding drifts to high, and a queue where everything is high has no priority
    in it. So the two claims that should be cheap to back up have to be backed up.
    """
    problems = []
    if finding.confidence == "high" and not finding.reproduction.strip():
        problems.append(
            "confidence 'high' requires a `reproduction`: how someone else can go and "
            "see this for themselves. If you cannot write one down, the honest "
            "confidence is 'medium'."
        )
    if finding.severity in _SEVERITIES_NEEDING_OBJECTS and not finding.affected_objects:
        problems.append(
            f"severity '{finding.severity}' requires `affected_objects`: name the "
            "tables or columns involved. A severe finding nobody can locate cannot be "
            "acted on — either name what it affects, or it is not this severe."
        )
    # both at once: an agent that has to discover these one call at a time will
    # take the hint that the cheapest route is to downgrade rather than to say more
    if problems:
        raise ValueError(" ".join(problems))


def _resolve_conditional(finding: Finding, schema_name: str, spec: dict) -> list[tuple[str, str]]:
    """Extra fields selected by the schema's discriminator, if it has one."""
    conditional = spec.get("conditional_extra") or {}
    if not conditional:
        return []
    key = spec.get("discriminator") or ""
    value = str(finding.extra.get(key) or "").strip()
    if not value:
        hint = spec.get("discriminator_hint") or (
            f"one of: {', '.join(conditional)} — it selects which further fields apply"
        )
        raise ValueError(f"findings schema '{schema_name}' requires extra.{key}: {hint}")
    if value not in conditional:
        raise ValueError(
            f"findings schema '{schema_name}': {key} must be one of "
            f"{list(conditional)}, got '{value}'"
        )
    return conditional[value]


def validate_finding(payload: dict, schema_name: str) -> Finding:
    """Structural validation. Raises pydantic ValidationError or ValueError."""
    if schema_name not in FINDINGS_SCHEMAS:
        known = ", ".join(sorted(FINDINGS_SCHEMAS))
        raise ValueError(f"unknown findings schema '{schema_name}' (known: {known})")
    data = dict(payload)
    data["schema_name"] = schema_name
    finding = Finding.model_validate(data)
    spec = FINDINGS_SCHEMAS[schema_name]
    # the discriminator is validated before the fields it selects, so the error
    # names the real problem instead of cascading into a list of missing fields
    required = list(spec["required_extra"]) + _resolve_conditional(finding, schema_name, spec)
    missing = [f"{key} ({desc})" for key, desc in required if not finding.extra.get(key)]
    if missing:
        raise ValueError(
            f"findings schema '{schema_name}' requires these fields in `extra`: "
            + "; ".join(missing)
        )
    # calibration last: what the workflow demands is the more substantive gap, so
    # it gets reported first when a finding is missing both
    _check_calibration(finding)
    return finding
