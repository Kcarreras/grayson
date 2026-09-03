"""Workflow template data model."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from grayson.charts.spec import KINDS as CHART_KINDS
from grayson.findings.library import FindingField

_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")

#: every template model tolerates and round-trips unknown fields: a newer
#: grayson's additions survive an older one's edit-and-save (the round-trip
#: contract of docs/LIBRARY.md "Format stability").
_ROUND_TRIP = ConfigDict(extra="allow")


class SetupInput(BaseModel):
    model_config = _ROUND_TRIP

    key: str
    prompt: str
    required: bool = True
    #: the answer names tables that join the session's readable scope (as
    #: scope_extra, like library views do). This is how a strict-scope workflow
    #: gets deliberate context — the human names upstream/downstream tables at
    #: setup and exactly those become readable, instead of loosening the scope.
    adds_scope: bool = False


class ChartRequirement(BaseModel):
    """A picture a checkpoint must close with.

    Whether an agent charts is otherwise its own judgment, prompted by prose.
    Where the checkpoint's content IS a shape — a distribution, a trend, a
    stage-to-stage comparison — the workflow can say so, and the gate then
    refuses to close without a chart of an allowed kind whose query is cited
    as evidence. `kinds` bounds the choice (empty = any kind); `description`
    says what the picture should show, the way a check's description says
    what the evidence should show.
    """

    model_config = _ROUND_TRIP

    kinds: list[str] = Field(default_factory=list)
    description: str = ""

    @field_validator("kinds")
    @classmethod
    def _known_kinds(cls, v: list[str]) -> list[str]:
        unknown = [k for k in v if k not in CHART_KINDS]
        if unknown:
            raise ValueError(
                f"unknown chart kind(s) {', '.join(unknown)} — a session could never "
                f"satisfy this requirement (kinds: {', '.join(CHART_KINDS)})"
            )
        return v

    def allows(self, kind: str) -> bool:
        return not self.kinds or kind in self.kinds

    def label(self) -> str:
        """One line for previews, errors, and the console."""
        kinds = "|".join(self.kinds) if self.kinds else "any kind"
        return f"{kinds}: {' '.join(self.description.split())}" if self.description else kinds


class CheckDef(BaseModel):
    model_config = _ROUND_TRIP

    key: str
    title: str
    description: str = ""
    #: charts this checkpoint must cite to close (each entry is one required
    #: chart; a cited chart satisfies at most one). Empty leaves charting to
    #: the agent's judgment, which is the default and the common case.
    charts: list[ChartRequirement] = Field(default_factory=list)
    #: checkpoints that must close before this one can. The one genuinely
    #: sequential dependency in the core set — you cannot hunt a cause before
    #: the anomaly reproduces — was prose in a description until now.
    depends_on: list[str] = Field(default_factory=list)
    #: setup inputs this check works from. Declared rather than inferred: it
    #: tells the agent which of the user's answers this checkpoint is meant to
    #: test, and it lets lint catch a required input that no check ever uses —
    #: a question asked of the user and then quietly ignored.
    uses_inputs: list[str] = Field(default_factory=list)


class WorkflowTemplate(BaseModel):
    model_config = _ROUND_TRIP

    name: str
    title: str = ""
    description: str = ""
    #: free labels for finding a workflow in a catalog that has grown past a
    #: screen: a domain (orders, finance), a team, a cadence. The console
    #: filters by them; nothing else reads them.
    tags: list[str] = Field(default_factory=list)
    suggested_guard_profile: str = "moderate"
    #: workflows with a bounded shape (table-onboarding: one declared table,
    #: nothing open-ended) suggest strict scope; None leaves the workspace
    #: default in charge. Like the guard profile, a suggestion — flags and
    #: per-workflow workspace settings both outrank it.
    suggested_strict_scope: bool | None = None
    setup_inputs: list[SetupInput] = Field(default_factory=list)
    required_checks: list[CheckDef] = Field(default_factory=list)
    #: breadth without gates. A workflow that must cover thirty fundamentals
    #: cannot make all thirty mandatory — on a five-column lookup table most do
    #: not apply, and an unwaivable gate on an inapplicable check is how you
    #: teach agents to launder evidence. Suggested checks are surfaced at
    #: session start and in the console; nothing blocks on them.
    suggested_checks: list[CheckDef] = Field(default_factory=list)
    findings_schema: str = "standard_v1"
    #: the workflow's own additions to that schema (see FindingField). The
    #: effective schema a finding validates against is the built-in plus these.
    findings_fields: list[FindingField] = Field(default_factory=list)
    #: provenance for library workflows: who authored the file (`grayson user`
    #: id) and, for forks, which workflow it started from. Empty on core
    #: templates and legacy library files.
    created_by: str = ""
    forked_from: str = ""

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for tag in v:
            tag = str(tag).strip().lower()
            if not tag:
                continue
            if not _TAG_RE.match(tag):
                raise ValueError(
                    f"tag '{tag}' must be 1-32 lowercase letters, digits, '-', '_' or '.'"
                )
            if tag not in out:
                out.append(tag)
        return out

    @field_validator("findings_fields")
    @classmethod
    def _unique_field_keys(cls, v: list[FindingField]) -> list[FindingField]:
        keys = [f.key for f in v]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate findings field key(s): {', '.join(dupes)}")
        return v

    def check(self, key: str) -> CheckDef | None:
        """A checkpoint by key — required or suggested."""
        pool = self.required_checks + self.suggested_checks
        return next((c for c in pool if c.key == key), None)

    def required_check_keys(self) -> list[str]:
        return [c.key for c in self.required_checks]

    def suggested_check_keys(self) -> list[str]:
        return [c.key for c in self.suggested_checks]

    def unmet_dependencies(self, key: str, closed: set[str]) -> list[str]:
        """Which of `key`'s declared prerequisites are not yet closed."""
        check = self.check(key)
        if check is None:
            return []
        return [dep for dep in check.depends_on if dep not in closed]

    def input_keys(self) -> list[str]:
        return [i.key for i in self.setup_inputs]

    def chart_requirements(self) -> int:
        """How many checkpoints (required or suggested) demand a chart."""
        return sum(1 for c in self.required_checks + self.suggested_checks if c.charts)

    def checks_using(self, input_key: str) -> list[str]:
        """Keys of the checkpoints that declare they work from a setup input."""
        return [
            c.key
            for c in self.required_checks + self.suggested_checks
            if input_key in c.uses_inputs
        ]

    def unknown_input_keys(self, provided: dict) -> list[str]:
        return sorted(set(provided) - set(self.input_keys()))

    def missing_required_inputs(self, provided: dict) -> list[str]:
        return [
            i.key
            for i in self.setup_inputs
            if i.required and not str(provided.get(i.key) or "").strip()
        ]
