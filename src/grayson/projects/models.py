"""Versioned contracts: unknown execution semantics fail closed."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from grayson.checks.regression import Expectation
from grayson.projects.identifiers import is_project_object_name


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectDefaults(StrictModel):
    format: Literal[1] = 1
    kind: Literal["analysis", "pipeline"] = "pipeline"
    approval: Literal["guided", "milestones", "bounded"] = "milestones"
    max_iterations: int = Field(default=12, ge=1, le=100)
    max_queries: int = Field(default=200, ge=1, le=10000)
    max_minutes: int = Field(default=120, ge=1, le=1440)
    max_stalled_iterations: int = Field(default=3, ge=1, le=20)
    evidence_minutes: int = Field(default=60, ge=1, le=1440)
    max_revalidations: int = Field(default=0, ge=0, le=100)
    max_actions: int = Field(default=200, ge=1, le=2000)


class JoinContract(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    left: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    right: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    left_keys: list[str] = Field(min_length=1)
    right_keys: list[str] = Field(min_length=1)
    how: Literal["left", "inner"] = "left"
    cardinality: Literal["one_to_one", "many_to_one", "one_to_many"] = "many_to_one"
    max_matches: int = Field(default=1, ge=1, le=10000)
    max_unmatched_left: int = Field(default=0, ge=0)
    max_unmatched_right: int | None = Field(default=None, ge=0)
    semantics: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def shape(self):
        if len(self.left_keys) != len(self.right_keys):
            raise ValueError("join key lists must have equal lengths")
        if self.cardinality != "one_to_many" and self.max_matches != 1:
            raise ValueError("only one_to_many joins may allow multiple matches")
        return self


class AcceptanceCheck(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    name: str = Field(min_length=1, max_length=160)
    kind: Literal["grain", "population", "measure", "values", "assertion"]
    relation: str = Field(default="candidate", pattern=r"^[a-z][a-z0-9_]{0,47}$")
    keys: list[str] = Field(default_factory=list)
    baseline_sql: str = ""
    column: str = ""
    group_by: list[str] = Field(default_factory=list)
    absolute_tolerance: float = Field(default=0, ge=0, allow_inf_nan=False)
    relative_percent: float = Field(default=0, ge=0, allow_inf_nan=False)
    max_missing: int = Field(default=0, ge=0)
    max_added: int = Field(default=0, ge=0)
    sql: str = ""
    expectation: Expectation = Field(default_factory=Expectation)
    rationale: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def shape(self):
        if self.kind in {"grain", "population", "values"} and not self.keys:
            raise ValueError("grain/population checks require keys")
        if self.kind in {"population", "measure", "values"} and not self.baseline_sql:
            raise ValueError("population/measure checks require independent baseline_sql")
        if self.kind in {"measure", "values"} and not self.column:
            raise ValueError("measure checks require a column")
        if self.kind == "assertion" and "{{relation}}" not in self.sql:
            raise ValueError("assertion SQL must reference {{relation}}")
        return self


class Contract(StrictModel):
    format: Literal[1] = 1
    goal: str = Field(min_length=1, max_length=8000)
    deliverable: str = Field(min_length=1, max_length=4000)
    deployment_target: str = ""
    materialization: Literal["view", "table"] = "view"
    kind: Literal["analysis", "pipeline"] = "pipeline"
    scope: list[str] = Field(min_length=1)
    semantics: dict[str, str] = Field(min_length=1)
    semantic_checks: dict[str, list[str]] = Field(default_factory=dict)
    human_semantic_review: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    data_window: str = Field(min_length=1, max_length=4000)
    policy: ProjectDefaults = Field(default_factory=ProjectDefaults)
    joins: list[JoinContract] = Field(default_factory=list)
    checks: list[AcceptanceCheck] = Field(min_length=1, max_length=100)
    minimum_rows: int = Field(default=1, ge=0)
    measure_exemption: str = ""
    review_questions: list[str] = Field(
        default_factory=lambda: [
            "Do joins respect the approved grain, cardinality and unmatched-row policy?",
            "Could filters, DISTINCT, aggregation or null handling hide population or value loss?",
            "Does the implementation follow every approved semantic definition and exclusion?",
            "What does the evidence leave unproven, including deployment behaviour?",
        ],
        min_length=1,
    )

    @model_validator(mode="after")
    def coverage(self):
        if any(not is_project_object_name(name) for name in self.scope):
            raise ValueError("project source tables must use unquoted DB.SCHEMA.OBJECT identifiers")
        for items in (self.joins, self.checks):
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate contract IDs")
        kinds = {c.kind for c in self.checks if c.relation == "candidate"}
        if not {"grain", "population"} <= kinds:
            raise ValueError("every project requires candidate grain and population checks")
        if "measure" not in kinds and not self.measure_exemption:
            raise ValueError("require a candidate measure check or an explicit measure_exemption")
        if not all(k.strip() and v.strip() for k, v in self.semantics.items()):
            raise ValueError("semantic definitions cannot be blank")
        known = {c.id for c in self.checks}
        covered = set(self.semantic_checks) | set(self.human_semantic_review)
        if covered != set(self.semantics):
            raise ValueError(
                "map every semantic definition to semantic_checks or human_semantic_review"
            )
        if any(not ids or not set(ids) <= known for ids in self.semantic_checks.values()):
            raise ValueError("semantic_checks must reference existing acceptance check IDs")
        self.scope = sorted(set(s.upper() for s in self.scope))
        self.policy.kind = self.kind
        return self


class Node(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    kind: Literal["query", "join"] = "query"
    sql: str = ""
    # Join nodes use their approved contract's inputs and keys. Projections are
    # expressions over aliases l and r, never arbitrary SQL statements.
    columns: dict[str, str] = Field(default_factory=dict)
    purpose: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def shape(self):
        if self.id == "candidate" or self.id.startswith("grayson_"):
            raise ValueError("candidate and grayson_* are reserved relation names")
        if self.kind == "query" and (not self.sql or self.columns):
            raise ValueError("query nodes require SQL and no join columns")
        if self.kind == "join" and (self.sql or not self.columns):
            raise ValueError("join nodes require projected columns, not SQL")
        return self


class Candidate(StrictModel):
    format: Literal[1] = 1
    summary: str = Field(min_length=1, max_length=8000)
    nodes: list[Node] = Field(min_length=1, max_length=50)
    output: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    diagnosis: str = Field(min_length=1, max_length=8000)
    addressed_checks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def shape(self):
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)) or self.output not in ids:
            raise ValueError("candidate needs unique nodes and an existing output")
        return self


class Review(StrictModel):
    answers: list[str] = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    issues: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=8000)


class PlanStep(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    task: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["pending", "working", "done", "blocked"] = "pending"
    evidence: list[str] = Field(default_factory=list)


class PlanAction(StrictModel):
    steps: list[PlanStep]


class InterventionAction(StrictModel):
    kind: Literal["label_sample", "confirm_semantics", "choose", "free_response", "scope_request"]
    title: str = Field(min_length=1)
    payload: dict = Field(
        description="Request object for the selected intervention kind",
        examples=[{"question": "Which data window should this analysis use?"}],
    )
