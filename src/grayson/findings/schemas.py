"""Findings schemas: closed-ended structures every finding must satisfy.

Findings are the deterministic QA-of-QA output gate — a finding cannot be
recorded unless it validates against its workflow's schema AND cites evidence
(executed query ids). The evidence-existence check is enforced by the engine
(it needs the session); this module owns structural validation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

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

#: The fields every finding carries whatever its schema, in the order a reader
#: meets them, with the rule each is held to. Published so the console and the
#: agent see the same contract the validator enforces — a schema nobody can
#: read is a schema agents discover one rejection at a time.
BASE_FIELDS: list[dict] = [
    {
        "key": "title",
        "rule": "at least 3 characters",
        "required": True,
        "description": "What is wrong, in one line.",
    },
    {
        "key": "severity",
        "rule": " | ".join(SEVERITIES),
        "required": True,
        "description": "How much it matters, on the published scale (`finding rubric`).",
    },
    {
        "key": "confidence",
        "rule": " | ".join(CONFIDENCES),
        "required": True,
        "description": "How well the evidence supports the claim, not how it feels.",
    },
    {
        "key": "summary",
        "rule": "at least 10 characters",
        "required": True,
        "description": "The claim and what supports it, in a paragraph.",
    },
    {
        "key": "evidence",
        "rule": "one or more executed query ids (q_XXXX)",
        "required": True,
        "description": "The queries that demonstrate it — they must have run in this session.",
    },
    {
        "key": "affected_objects",
        "rule": "required for severity critical or high",
        "required": False,
        "description": "Tables and columns involved, fully qualified.",
    },
    {
        "key": "reproduction",
        "rule": "required for confidence high",
        "required": False,
        "description": "How someone else goes and sees it: a query, a filter, a locator.",
    },
    {
        "key": "proposed_remediation",
        "rule": "free text",
        "required": False,
        "description": "What would fix it, if you know.",
    },
    {
        "key": "open_questions",
        "rule": "list of strings",
        "required": False,
        "description": "What is still unsettled and would change the finding.",
    },
    {
        "key": "supersedes",
        "rule": "an earlier finding id (f_XXX)",
        "required": False,
        "description": "The finding this one corrects; applied only if the user accepts.",
    },
    {
        "key": "extra",
        "rule": "object — the schema's own fields live here",
        "required": False,
        "description": "Workflow-specific fields: the schema below says which.",
    },
]
BASE_FIELD_KEYS: frozenset[str] = frozenset(f["key"] for f in BASE_FIELDS)

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


def _field(f: Any) -> dict:
    """A workflow findings field as a plain dict, whether it arrives as a
    WorkflowTemplate model or as the YAML's own dict."""

    def get(k: str, d: Any = None) -> Any:
        return f.get(k, d) if isinstance(f, dict) else getattr(f, k, d)

    return {
        "key": str(get("key") or ""),
        "description": str(get("description") or ""),
        "required": bool(get("required", True)),
        "choices": [str(c) for c in (get("choices") or [])],
    }


def effective_extra(schema_name: str, workflow_fields: Iterable[Any] | None = None) -> list[dict]:
    """The unconditional `extra` fields a finding must (or may) carry under a
    built-in schema plus a workflow's own additions, in the order an agent
    should read them: the schema's first, then the workflow's.

    Each entry: key, description, required, choices, source ('schema' or
    'workflow'). A workflow field whose key the schema already requires
    tightens it — description and choices are the workflow's, and it stays
    required — rather than appearing twice.
    """
    spec = FINDINGS_SCHEMAS.get(schema_name, {"required_extra": []})
    out: list[dict] = [
        {"key": key, "description": desc, "required": True, "choices": [], "source": "schema"}
        for key, desc in spec.get("required_extra", [])
    ]
    by_key = {e["key"]: e for e in out}
    for raw in workflow_fields or []:
        f = _field(raw)
        if not f["key"]:
            continue
        if f["key"] in by_key:
            base = by_key[f["key"]]
            base["description"] = f["description"] or base["description"]
            base["choices"] = f["choices"]
            base["source"] = "schema+workflow"
            continue
        entry = {**f, "source": "workflow"}
        out.append(entry)
        by_key[f["key"]] = entry
    return out


def describe_schema(schema_name: str, workflow_fields: Iterable[Any] | None = None) -> dict:
    """Everything a finding under this schema is held to, for agents and the
    console: the base fields with their rules, the effective `extra` fields,
    the discriminator and the branches it selects, the enforced calibration
    rules, and an example payload shaped to pass.

    `workflow_fields` are the workflow's own additions (WorkflowTemplate
    .findings_fields); the description is of the combined contract.
    """
    known = schema_name in FINDINGS_SCHEMAS
    spec = FINDINGS_SCHEMAS.get(schema_name, {"required_extra": []})
    extra = effective_extra(schema_name, workflow_fields)
    conditional = {
        value: [{"key": k, "description": d} for k, d in fields]
        for value, fields in (spec.get("conditional_extra") or {}).items()
    }
    example_extra: dict = {}
    for e in extra:
        if e["required"]:
            example_extra[e["key"]] = e["choices"][0] if e["choices"] else "..."
    discriminator = spec.get("discriminator") or ""
    if discriminator and conditional:
        first = next(iter(conditional))
        example_extra[discriminator] = first
        for f in conditional[first]:
            example_extra[f["key"]] = "..."
    example = {
        "title": "What is wrong, in one line",
        "severity": "medium",
        "confidence": "medium",
        "summary": "The claim, and what in the cited queries supports it.",
        "affected_objects": ["DB.SCHEMA.TABLE"],
        "evidence": ["q_0001"],
    }
    if example_extra:
        example["extra"] = example_extra
    return {
        "name": schema_name,
        "known": known,
        "base_fields": [dict(f) for f in BASE_FIELDS],
        "required_extra": extra,
        "discriminator": discriminator,
        "discriminator_hint": spec.get("discriminator_hint") or "",
        "conditional_extra": conditional,
        "enforced": list(rubric()["enforced"]),
        "example": example,
    }


def validate_finding(
    payload: dict, schema_name: str, workflow_fields: Iterable[Any] | None = None
) -> Finding:
    """Structural validation. Raises pydantic ValidationError or ValueError.

    `workflow_fields` are the workflow's own additions to the named schema
    (WorkflowTemplate.findings_fields): required ones must be present, and a
    field with `choices` must hold one of them.
    """
    if schema_name not in FINDINGS_SCHEMAS:
        known = ", ".join(sorted(FINDINGS_SCHEMAS))
        raise ValueError(f"unknown findings schema '{schema_name}' (known: {known})")
    data = dict(payload)
    data["schema_name"] = schema_name
    finding = Finding.model_validate(data)
    spec = FINDINGS_SCHEMAS[schema_name]
    # the discriminator is validated before the fields it selects, so the error
    # names the real problem instead of cascading into a list of missing fields
    extra = effective_extra(schema_name, workflow_fields)
    required = [(e["key"], e["description"]) for e in extra if e["required"]]
    required += _resolve_conditional(finding, schema_name, spec)
    missing = [f"{key} ({desc})" for key, desc in required if not finding.extra.get(key)]
    if missing:
        raise ValueError(
            f"findings schema '{schema_name}' requires these fields in `extra`: "
            + "; ".join(missing)
        )
    off_list = [
        f"{e['key']} must be one of {e['choices']}, got '{finding.extra[e['key']]}'"
        for e in extra
        if e["choices"]
        and finding.extra.get(e["key"]) not in (None, "")
        and str(finding.extra.get(e["key"])) not in e["choices"]
    ]
    if off_list:
        raise ValueError(f"findings schema '{schema_name}': " + "; ".join(off_list))
    # calibration last: what the workflow demands is the more substantive gap, so
    # it gets reported first when a finding is missing both
    _check_calibration(finding)
    return finding
