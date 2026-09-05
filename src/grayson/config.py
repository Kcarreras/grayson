"""Workspace configuration: grayson.toml parsing, guard profiles, library pointer."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from grayson.knowledge.policy import KnowledgePolicy, PolicyError

CONFIG_FILENAME = "grayson.toml"

BUILTIN_PROFILES: dict[str, dict[str, int]] = {
    "strict": {"auto_limit": 1000, "timeout_seconds": 60, "budget_warn": 25, "budget_cap": 50},
    "moderate": {"auto_limit": 10000, "timeout_seconds": 120, "budget_warn": 50, "budget_cap": 0},
    "generous": {"auto_limit": 100000, "timeout_seconds": 300, "budget_warn": 0, "budget_cap": 0},
}


class GuardSettings(BaseModel):
    """Independent guard controls. 0 always means 'off' for that control."""

    auto_limit: int = Field(default=10000, ge=0)
    timeout_seconds: int = Field(default=120, ge=0)
    budget_warn: int = Field(default=50, ge=0)
    budget_cap: int = Field(default=0, ge=0)


class ScopeConfig(BaseModel):
    allowed: list[str] = Field(default_factory=list)  # "DB.SCHEMA" fnmatch globs
    strict: bool = False


class WorkflowDefaults(BaseModel):
    """Per-workflow session defaults ([workflow_defaults.<name>] in grayson.toml).

    Set by the team in the console settings; an unset field inherits the usual
    resolution (explicit flag > this > last-used/template suggestion), so a
    workflow like table-onboarding can default to strict scope without taking
    the choice away at session start."""

    guard_profile: str | None = None
    strict_scope: bool | None = None


class WorkspaceConfig(BaseModel):
    connection: str = "default"
    default_guard_profile: str = "moderate"
    guard_profiles: dict[str, GuardSettings] = Field(default_factory=dict)
    scopes: ScopeConfig = Field(default_factory=ScopeConfig)
    workflow_defaults: dict[str, WorkflowDefaults] = Field(default_factory=dict)
    library_path: Path | None = None
    library_auto_push: bool = False
    #: which knowledge lifecycle actions an agent may take alone ([knowledge] in
    #: grayson.toml); in team mode the library's own policy narrows it further
    knowledge: KnowledgePolicy = Field(default_factory=KnowledgePolicy)

    @classmethod
    def load(cls, path: Path) -> WorkspaceConfig:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        profiles = {name: GuardSettings(**vals) for name, vals in BUILTIN_PROFILES.items()}
        for name, vals in data.get("guard_profiles", {}).items():
            profiles[name] = GuardSettings(**vals)
        library = data.get("library", {}).get("path")
        library_path = Path(library).expanduser() if library else None
        if library_path is not None and not library_path.is_absolute():
            library_path = (path.resolve().parent / library_path).resolve()
        knowledge_raw = data.get("knowledge", {})
        try:
            knowledge = KnowledgePolicy.from_config(
                knowledge_raw if isinstance(knowledge_raw, dict) else {}
            )
        except PolicyError as e:
            raise ValueError(f"{CONFIG_FILENAME} [knowledge]: {e}") from e
        return cls(
            connection=data.get("connection", {}).get("name", "default"),
            default_guard_profile=data.get("defaults", {}).get("guard_profile", "moderate"),
            guard_profiles=profiles,
            scopes=ScopeConfig(**data.get("scopes", {})),
            workflow_defaults={
                name: WorkflowDefaults(**vals)
                for name, vals in data.get("workflow_defaults", {}).items()
                if isinstance(vals, dict)
            },
            library_path=library_path,
            library_auto_push=bool(data.get("library", {}).get("auto_push", False)),
            knowledge=knowledge,
        )

    def resolve_profile(self, name: str | None) -> GuardSettings:
        chosen = name or self.default_guard_profile
        if chosen not in self.guard_profiles:
            known = ", ".join(sorted(self.guard_profiles))
            raise KeyError(f"unknown guard profile '{chosen}' (known: {known})")
        return self.guard_profiles[chosen].model_copy()


CONFIG_TEMPLATE = """\
# grayson workspace configuration

[connection]
name = "default"          # snow CLI named connection to use

[defaults]
guard_profile = "moderate"

[scopes]
# db.schema globs agents may read without warnings, e.g. ["ANALYTICS.*", "RAW.PUBLIC"]
allowed = []
# true = block out-of-scope reads instead of warning
strict = false

# Guard profiles: named combinations of independent controls (0 = off).
[guard_profiles.strict]
auto_limit = 1000
timeout_seconds = 60
budget_warn = 25
budget_cap = 50

[guard_profiles.moderate]
auto_limit = 10000
timeout_seconds = 120
budget_warn = 50
budget_cap = 0

[guard_profiles.generous]
auto_limit = 100000
timeout_seconds = 300
budget_warn = 0
budget_cap = 0

# Team library repo (see docs/SPEC.md s11a). Uncomment to link a local clone:
# [library]
# path = "~/work/data-qa-library"

# Knowledge policy: which lifecycle actions on facts an agent may take alone
# (docs/LIBRARY.md, "Standing, pruning, and the knowledge policy").
#   propose    — the agent proposes everything; a human retires, supersedes, resolves
#   curate     — evidence-backed actions (retire, supersede, dismiss a question,
#                reconcile) are the agent's; judgment-only ones (resolve a contested
#                pair, restore) stay the human's
#   autonomous — the agent does all of it; the human audits after
# A linked team library's own policy (library.toml) can narrow this, never widen it.
[knowledge]
policy = "curate"
# lowest status a briefing ranks as knowledge rather than a hypothesis:
# user_confirmed | data_inferred | proposed
trust = "data_inferred"
# a proposed fact nobody confirmed within this many days reads as unverified
proposed_horizon_days = 90
# facts shown per table at session start; the rest are counted and fetchable
briefing_cap = 12
# per-action overrides, e.g. to keep one action human under `autonomous`:
# [knowledge.agent]
# retire = "user"
"""
