"""QA view library: registry of analysis-ready views with base-file pointers.

Agents never hold DDL rights. At session setup the coverage check offers existing
views to reuse, flags stale ones with regenerated DDL, and points agents at the
work-repo base files to assemble any missing view — all executed by the user.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from grayson.util import atomic_write_text, utcnow

if TYPE_CHECKING:
    from grayson.core.session import Session

_VIEW_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}\Z")


class ViewEntry(BaseModel):
    # extra="allow": fields a newer grayson adds to an entry survive an older
    # one's rewrite — model_dump() carries them back out (the round-trip
    # contract of docs/LIBRARY.md "Format stability").
    model_config = ConfigDict(extra="allow")

    name: str
    purpose: str = ""

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        # A view name becomes part of a DDL filename; reject anything that could
        # escape views/ddl (path separators, '..', newlines).
        if not _VIEW_NAME.match(v):
            raise ValueError(
                f"invalid view name {v!r}: use a bare or dotted SQL identifier "
                "(letters, digits, _ , $)"
            )
        return v

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
        # Unknown top-level keys round-trip: a newer grayson (or a hand edit)
        # may keep data beside "views"; a rewrite never strips what it doesn't know.
        existing: dict = {}
        if self.registry_path.is_file():
            try:
                existing = yaml.safe_load(self.registry_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                existing = {}
        payload = {
            "views": [v.model_dump() for v in views],
            **{k: v for k, v in existing.items() if k != "views" and isinstance(k, str)},
        }
        atomic_write_text(
            self.registry_path,
            "# QA view library registry (managed by `grayson views`)\n"
            + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        )

    def list(self) -> list[ViewEntry]:
        return self._load()

    def get(self, name: str) -> ViewEntry | None:
        return next((v for v in self._load() if v.name.upper() == name.upper()), None)

    def register(
        self,
        entry: ViewEntry,
        ddl: str | None = None,
        source_last_altered: dict[str, str] | None = None,
    ) -> ViewEntry:
        views = self._load()
        views = [v for v in views if v.name.upper() != entry.name.upper()]
        if ddl is not None:
            ddl_dir = self.dir / "ddl"
            ddl_dir.mkdir(parents=True, exist_ok=True)
            ddl_file = ddl_dir / f"{entry.name.lower()}.sql"
            ddl_file.write_text(ddl, encoding="utf-8")
            entry.ddl_path = f"ddl/{ddl_file.name}"
        if source_last_altered:
            # The staleness baseline: what the sources looked like when this view
            # was (re)built. coverage_check compares future LAST_ALTERED to this.
            entry.source_last_altered = {k.upper(): str(v) for k, v in source_last_altered.items()}
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


def enter_session_scope(registry: ViewRegistry, session: Session, targets: list[str]) -> list[str]:
    """Put the library views matching `targets` into the session's query scope.

    The whole point of the view library is that agents query these views; without
    this, a registered view read is an out-of-scope warning (a hard block under
    strict scope) and evidence citing it would not count as touching the
    investigation. Names are added as registered, so queries referencing the view
    the same way — bare or fully qualified — pass the guard and count as evidence.
    """
    names = sorted(v.name.upper() for v in registry.matching(targets))
    if names:
        session.add_scope(names)
        session.log_event("system", "views_in_scope", {"views": names})
    return names
