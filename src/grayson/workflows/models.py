"""Workflow template data model."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SetupInput(BaseModel):
    key: str
    prompt: str
    required: bool = True


class CheckDef(BaseModel):
    key: str
    title: str
    description: str = ""


class WorkflowTemplate(BaseModel):
    name: str
    title: str = ""
    description: str = ""
    suggested_guard_profile: str = "moderate"
    setup_inputs: list[SetupInput] = Field(default_factory=list)
    required_checks: list[CheckDef] = Field(default_factory=list)
    open_stages: list[str] = Field(default_factory=lambda: ["analysis"])
    findings_schema: str = "standard_v1"
    #: provenance for library workflows: who authored the file (`grayson user`
    #: id) and, for forks, which workflow it started from. Empty on core
    #: templates and legacy library files.
    created_by: str = ""
    forked_from: str = ""

    def check(self, key: str) -> CheckDef | None:
        return next((c for c in self.required_checks if c.key == key), None)

    def required_check_keys(self) -> list[str]:
        return [c.key for c in self.required_checks]
