"""Knowledge library: one markdown file per table, YAML frontmatter facts.

Team-shareable by design — each fact carries provenance (who confirmed it, when,
on what evidence) and one fact per list entry keeps diffs small and merges clean.
Facts are the durable, agent-readable tribal knowledge that makes each QA session
faster and more reliable than the last.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from grayson.util import utcnow

FactStatus = Literal["proposed", "data_inferred", "user_confirmed"]
_FQN_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")  # \Z: no trailing-newline match

#: structured base-descriptor fields, alongside the open-ended facts list.
#: `columns` entries are {name, type?, description?, nullable?}; `relationships`
#: entries are {to, on, cardinality?, note?}.
PROFILE_KEYS = ("grain", "freshness", "owners", "columns", "relationships", "open_questions")
_PROFILE_DEFAULTS: dict[str, object] = {
    "grain": "",
    "freshness": "",
    "owners": [],
    "columns": [],
    "relationships": [],
    "open_questions": [],
}


class Fact(BaseModel):
    id: str
    fact: str
    status: FactStatus = "proposed"
    created_by: str = "agent"
    created_at: str = Field(default_factory=utcnow)
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    evidence: list[str] = Field(default_factory=list)


class KnowledgeStore:
    def __init__(self, knowledge_dir: Path):
        self.dir = knowledge_dir

    # -- paths -----------------------------------------------------------

    @staticmethod
    def _parts(fqn: str) -> list[str]:
        parts = fqn.upper().split(".")
        if len(parts) != 3 or not all(_FQN_PART.match(p) for p in parts):
            raise ValueError(f"table must be a valid DB.SCHEMA.TABLE name, got {fqn!r}")
        return parts

    def table_path(self, fqn: str) -> Path:
        db, schema, table = self._parts(fqn)
        return self.dir / db / schema / f"{table}.md"

    # -- read ------------------------------------------------------------

    def read(self, fqn: str) -> dict[str, Any]:
        path = self.table_path(fqn)
        if not path.is_file():
            return {
                "table": fqn.upper(),
                "facts": [],
                "definition_files": [],
                "notes": "",
                **{
                    k: (v.copy() if isinstance(v, list) else v)
                    for k, v in _PROFILE_DEFAULTS.items()
                },
            }
        front, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        data = yaml.safe_load(front) or {} if front else {}
        facts = [Fact.model_validate(f).model_dump() for f in data.get("facts", [])]
        table = data.get("table", fqn.upper())
        doc = {
            "table": table,
            "facts": facts,
            "definition_files": data.get("definition_files", []),
            "notes": _strip_heading(body, table),
        }
        for key, default in _PROFILE_DEFAULTS.items():
            doc[key] = data.get(key, default.copy() if isinstance(default, list) else default)
        return doc

    def fact(self, fqn: str, fact_id: str) -> dict | None:
        return next((f for f in self.read(fqn)["facts"] if f["id"] == fact_id), None)

    # -- write -----------------------------------------------------------

    def _write(self, fqn: str, doc: dict[str, Any]) -> None:
        path = self.table_path(fqn)
        path.parent.mkdir(parents=True, exist_ok=True)
        front = {"table": doc["table"]}
        for key in PROFILE_KEYS:  # only write populated profile fields (small diffs)
            if doc.get(key):
                front[key] = doc[key]
        front |= {
            "definition_files": doc.get("definition_files", []),
            "facts": doc.get("facts", []),
        }
        notes = doc.get("notes", "").strip()
        text = (
            "---\n"
            + yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
            + "---\n\n"
            + f"# {doc['table']}\n"
            + (f"\n{notes}\n" if notes else "")
        )
        path.write_text(text, encoding="utf-8")

    def add_fact(
        self,
        fqn: str,
        fact_text: str,
        fact_id: str | None = None,
        status: FactStatus = "proposed",
        created_by: str = "agent",
        evidence: list[str] | None = None,
    ) -> dict:
        # 'agents propose; users confirm': user_confirmed status is reachable ONLY
        # through confirm_fact (a user action), never by writing a new fact. This
        # stops an agent from laundering an assertion into human-authority provenance.
        if status == "user_confirmed":
            raise ValueError(
                "cannot create a fact as 'user_confirmed'; add it as 'proposed' or "
                "'data_inferred', then confirm it via a user action (knowledge confirm)"
            )
        doc = self.read(fqn)
        fid = fact_id or _slug(fact_text, {f["id"] for f in doc["facts"]})
        if any(f["id"] == fid for f in doc["facts"]):
            raise ValueError(f"fact id '{fid}' already exists for {fqn}")
        fact = Fact(
            id=fid,
            fact=fact_text,
            status=status,
            created_by=created_by,
            evidence=evidence or [],
        )
        doc["facts"].append(fact.model_dump())
        self._write(fqn, doc)
        return fact.model_dump()

    def confirm_fact(self, fqn: str, fact_id: str, by: str = "user") -> dict:
        doc = self.read(fqn)
        for f in doc["facts"]:
            if f["id"] == fact_id:
                f["status"] = "user_confirmed"
                f["confirmed_by"] = by
                f["confirmed_at"] = utcnow()
                self._write(fqn, doc)
                return f
        raise KeyError(f"no fact '{fact_id}' for {fqn}")

    def set_profile(self, fqn: str, updates: dict[str, Any]) -> dict:
        """Merge structured base-descriptor fields (grain, columns, ...) into the doc."""
        allowed = {*PROFILE_KEYS, "definition_files", "notes"}
        bad = set(updates) - allowed
        if bad:
            raise ValueError(f"unknown profile fields: {sorted(bad)} (allowed: {sorted(allowed)})")
        if "columns" in updates:
            cols = updates["columns"]
            if not isinstance(cols, list) or not all(
                isinstance(c, dict) and c.get("name") for c in cols
            ):
                raise ValueError("columns must be a list of objects, each with at least a 'name'")
        doc = self.read(fqn)
        doc.update(updates)
        self._write(fqn, doc)
        return self.read(fqn)

    def set_definition_files(self, fqn: str, files: list[str]) -> dict:
        doc = self.read(fqn)
        doc["definition_files"] = list(dict.fromkeys(files))
        self._write(fqn, doc)
        return doc

    # -- search ----------------------------------------------------------

    def search(self, term: str) -> list[dict]:
        term_l = term.lower()
        hits = []
        if not self.dir.is_dir():
            return hits
        for path in sorted(self.dir.rglob("*.md")):
            if path.name == "glossary.md":
                if term_l in path.read_text(encoding="utf-8").lower():
                    hits.append({"source": "glossary", "match": path.name})
                continue
            try:
                front, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
                data = yaml.safe_load(front) or {} if front else {}
            except (yaml.YAMLError, OSError):
                continue
            table = data.get("table", path.stem)
            for f in data.get("facts", []):
                if (
                    term_l in str(f.get("fact", "")).lower()
                    or term_l in str(f.get("id", "")).lower()
                ):
                    hits.append(
                        {
                            "source": table,
                            "fact_id": f.get("id"),
                            "fact": f.get("fact"),
                            "status": f.get("status"),
                        }
                    )
        return hits

    def all_tables(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        out = []
        for path in sorted(self.dir.rglob("*.md")):
            if path.name == "glossary.md":
                continue
            rel = path.relative_to(self.dir).with_suffix("")
            parts = rel.parts
            if len(parts) == 3:
                out.append(".".join(parts).upper())
        return out


def completeness(doc: dict[str, Any]) -> dict[str, Any]:
    """How fully described a table is, in the base sense.

    Base-complete means: grain declared, every listed column described,
    freshness expectation stated, relationships mapped, and definition files
    pointed at. Open questions and facts are counted, not required — 'we don't
    know yet' recorded as an open question is legitimate knowledge.
    """
    cols = doc.get("columns") or []
    described = sum(1 for c in cols if c.get("description"))
    missing = []
    if not doc.get("grain"):
        missing.append("grain")
    if not cols:
        missing.append("columns")
    elif described < len(cols):
        missing.append(f"column_descriptions ({described}/{len(cols)})")
    if not doc.get("freshness"):
        missing.append("freshness")
    if not doc.get("relationships"):
        missing.append("relationships")
    if not doc.get("definition_files"):
        missing.append("definition_files")
    return {
        "base_complete": not missing,
        "missing": missing,
        "columns_total": len(cols),
        "columns_described": described,
        "facts": len(doc.get("facts") or []),
        "facts_user_confirmed": sum(
            1 for f in doc.get("facts") or [] if f.get("status") == "user_confirmed"
        ),
        "open_questions": len(doc.get("open_questions") or []),
    }


def _strip_heading(body: str, table: str) -> str:
    """Drop a leading '# TABLE' heading so it does not leak into notes and
    duplicate on rewrite."""
    stripped = body.lstrip("\n")
    lines = stripped.split("\n", 1)
    if lines and lines[0].strip().lstrip("#").strip().upper() == table.upper():
        return (lines[1] if len(lines) > 1 else "").strip()
    return body.strip()


def _split_frontmatter(text: str) -> tuple[str, str]:
    if text.startswith("---"):
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
        if m:
            return m.group(1), m.group(2)
    return "", text


def _slug(text: str, existing: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "fact"
    if base not in existing:
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    return f"{base}_{i}"
