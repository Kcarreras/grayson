"""Workspace configuration: seekql.toml parsing, guard profiles, library pointer."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

CONFIG_FILENAME = "seekql.toml"

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


class WorkspaceConfig(BaseModel):
    connection: str = "default"
    default_guard_profile: str = "moderate"
    guard_profiles: dict[str, GuardSettings] = Field(default_factory=dict)
    scopes: ScopeConfig = Field(default_factory=ScopeConfig)
    library_path: Path | None = None
    library_auto_push: bool = False

    @classmethod
    def load(cls, path: Path) -> WorkspaceConfig:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        profiles = {name: GuardSettings(**vals) for name, vals in BUILTIN_PROFILES.items()}
        for name, vals in data.get("guard_profiles", {}).items():
            profiles[name] = GuardSettings(**vals)
        library = data.get("library", {}).get("path")
        return cls(
            connection=data.get("connection", {}).get("name", "default"),
            default_guard_profile=data.get("defaults", {}).get("guard_profile", "moderate"),
            guard_profiles=profiles,
            scopes=ScopeConfig(**data.get("scopes", {})),
            library_path=Path(library).expanduser() if library else None,
            library_auto_push=bool(data.get("library", {}).get("auto_push", False)),
        )

    def resolve_profile(self, name: str | None) -> GuardSettings:
        chosen = name or self.default_guard_profile
        if chosen not in self.guard_profiles:
            known = ", ".join(sorted(self.guard_profiles))
            raise KeyError(f"unknown guard profile '{chosen}' (known: {known})")
        return self.guard_profiles[chosen].model_copy()


CONFIG_TEMPLATE = """\
# seekql workspace configuration

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
"""
