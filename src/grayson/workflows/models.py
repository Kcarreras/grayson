"""Workflow template data model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: every template model tolerates and round-trips unknown fields: a newer
#: grayson's additions survive an older one's edit-and-save (the round-trip
#: contract of docs/LIBRARY.md "Format stability").
_ROUND_TRIP = ConfigDict(extra="allow")


class SetupInput(BaseModel):
    model_config = _ROUND_TRIP

    key: str
    prompt: str
    required: bool = True


class CheckDef(BaseModel):
    model_config = _ROUND_TRIP

    key: str
    title: str
    description: str = ""
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
    suggested_guard_profile: str = "moderate"
    setup_inputs: list[SetupInput] = Field(default_factory=list)
    required_checks: list[CheckDef] = Field(default_factory=list)
    #: breadth without gates. A workflow that must cover thirty fundamentals
    #: cannot make all thirty mandatory — on a five-column lookup table most do
    #: not apply, and an unwaivable gate on an inapplicable check is how you
    #: teach agents to launder evidence. Suggested checks are surfaced at
    #: session start and in the console; nothing blocks on them.
    suggested_checks: list[CheckDef] = Field(default_factory=list)
    findings_schema: str = "standard_v1"
    #: provenance for library workflows: who authored the file (`grayson user`
    #: id) and, for forks, which workflow it started from. Empty on core
    #: templates and legacy library files.
    created_by: str = ""
    forked_from: str = ""

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

    def unknown_input_keys(self, provided: dict) -> list[str]:
        return sorted(set(provided) - set(self.input_keys()))

    def missing_required_inputs(self, provided: dict) -> list[str]:
        return [
            i.key
            for i in self.setup_inputs
            if i.required and not str(provided.get(i.key) or "").strip()
        ]
