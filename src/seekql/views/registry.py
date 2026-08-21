"""QA view library: registry of analysis-ready views with base-file pointers.

Agents never hold DDL rights. At session setup the coverage check offers existing
views to reuse, flags stale ones with regenerated DDL, and points agents at the
work-repo base files to assemble any missing view — all executed by the user.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from seekql.util import utcnow


class ViewEntry(BaseModel):
    name: str
    purpose: str = ""
    source_tables: list[str] = Field(default_factory=list)
    base_files: list[str] = Field(default_factory=list)  # work-repo paths/globs
    ddl_path: str | None = None  # relative to views/
    created_at: str = Field(default_factory=utcnow)
    source_last_altered: dict[str, str] = Field(default_factory=dict)

    def normalized_sources(self) -> set[str]:
        return {t.upper() for t in self.source_tables}


class ViewRegistry:
    def __init__(self, views_dir: Path):
        self.dir = views_dir

    @property
    def registry_path(self) -> Path:
        return self.dir / "registry.yaml"

    def _load(self) -> list[ViewEntry]:
        if not self.registry_path.is_file():
            return []
        data = yaml.safe_load(self.registry_path.read_text(encoding="utf-8")) or {}
        return [ViewEntry.model_validate(v) for v in data.get("views", [])]

    def _save(self, views: list[ViewEntry]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"views": [v.model_dump() for v in views]}
        self.registry_path.write_text(
            "# QA view library registry (managed by `seekql views`)\n"
            + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def list(self) -> list[ViewEntry]:
        return self._load()

    def get(self, name: str) -> ViewEntry | None:
        return next((v for v in self._load() if v.name.upper() == name.upper()), None)

    def register(self, entry: ViewEntry, ddl: str | None = None) -> ViewEntry:
        views = self._load()
        views = [v for v in views if v.name.upper() != entry.name.upper()]
        if ddl is not None:
            ddl_dir = self.dir / "ddl"
            ddl_dir.mkdir(parents=True, exist_ok=True)
            ddl_file = ddl_dir / f"{entry.name.lower()}.sql"
            ddl_file.write_text(ddl, encoding="utf-8")
            entry.ddl_path = f"ddl/{ddl_file.name}"
        views.append(entry)
        self._save(views)
        return entry

    def matching(self, tables: list[str]) -> list[ViewEntry]:
        wanted = {t.upper() for t in tables}
        return [v for v in self._load() if v.normalized_sources() & wanted]

    def coverage_check(
        self, targets: list[str], current_last_altered: dict[str, str] | None = None
    ) -> dict:
        """Reuse / refresh / gaps for the session's target tables."""
        current = {k.upper(): str(v) for k, v in (current_last_altered or {}).items()}
        wanted = {t.upper() for t in targets}
        reuse, refresh = [], []
        covered: set[str] = set()
        for v in self.matching(targets):
            covered |= v.normalized_sources() & wanted
            stale_reasons = []
            for src, then in v.source_last_altered.items():
                now = current.get(src.upper())
                if now is not None and now != str(then):
                    stale_reasons.append(f"{src} changed since view was built")
            entry = {
                "name": v.name,
                "purpose": v.purpose,
                "source_tables": v.source_tables,
                "ddl_path": v.ddl_path,
                "base_files": v.base_files,
            }
            if stale_reasons:
                refresh.append({**entry, "reasons": stale_reasons})
            else:
                reuse.append(entry)
        gaps = sorted(wanted - covered)
        return {
            "reuse": reuse,
            "refresh": refresh,
            "gaps": gaps,
            "fully_covered": not gaps,
        }
