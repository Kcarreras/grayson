"""Knowledge library: one markdown file per table, YAML frontmatter facts.

Team-shareable by design — each fact carries provenance (who confirmed it, when,
on what evidence) and one fact per list entry keeps diffs small and merges clean.
Facts are the durable, agent-readable tribal knowledge that makes each QA session
faster and more reliable than the last.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from grayson.util import utcnow

#: version of the knowledge-doc format this grayson reads and writes. Docs
#: without a `format:` key predate stamping and are format 1 by definition.
#: The compatibility contract lives in docs/LIBRARY.md ("Format stability"):
#: changes within a format are additive-only, and a breaking change bumps this
#: and ships a migration step in FORMAT_STEPS the same release.
KNOWLEDGE_FORMAT = 1

#: future breaking changes register here: FORMAT_STEPS[n] rewrites a doc dict
#: from format n to n+1 (applied in sequence by upgrade_doc, run deliberately
#: via `grayson library migrate` — never implicitly on read). Empty today:
#: format 1 is the first stamped format, and stamping an unstamped doc needs
#: no reshaping.
FORMAT_STEPS: dict[int, Callable[[dict], dict]] = {}


class KnowledgeDocError(ValueError):
    """A knowledge doc exists but cannot be parsed (bad YAML front-matter,
    leftover merge-conflict markers, a malformed fact). Subclasses ValueError
    so every existing caller that handles bad input keeps working; the UI
    catches it specifically to show *which* file is broken instead of a 500."""


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

#: frontmatter keys this format defines; anything else in a doc is preserved
#: verbatim under the doc's "extra" key and written back untouched
_KNOWN_FRONT_KEYS = {"table", "format", "facts", "definition_files", *PROFILE_KEYS}


class Fact(BaseModel):
    # extra='allow': a fact written by a newer grayson may carry fields this
    # version does not know; they round-trip through read -> model_dump ->
    # write instead of being silently stripped on the next rewrite
    model_config = ConfigDict(extra="allow")

    id: str
    fact: str
    status: FactStatus = "proposed"
    created_by: str = "agent"
    created_at: str = Field(default_factory=utcnow)
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    evidence: list[str] = Field(default_factory=list)
    #: the configured user id (`grayson user set`) the write is attributable to.
    #: created_by records the actor KIND (agent|user); author records WHOSE
    #: workspace/identity produced it — the traceability handle in a shared library.
    author: str | None = None


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
                "format": KNOWLEDGE_FORMAT,
                "facts": [],
                "definition_files": [],
                "notes": "",
                "extra": {},
                **{
                    k: (v.copy() if isinstance(v, list) else v)
                    for k, v in _PROFILE_DEFAULTS.items()
                },
            }
        front, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        rel = path.relative_to(self.dir)
        try:
            data = yaml.safe_load(front) or {} if front else {}
        except yaml.YAMLError as e:
            raise KnowledgeDocError(
                f"knowledge doc {rel} has malformed front-matter (hand edit or "
                f"unresolved merge conflict?): {e}"
            ) from e
        if not isinstance(data, dict):
            raise KnowledgeDocError(f"knowledge doc {rel}: front-matter is not a mapping")
        try:
            facts = [Fact.model_validate(f).model_dump() for f in data.get("facts") or []]
        except ValueError as e:
            raise KnowledgeDocError(f"knowledge doc {rel} has a malformed fact: {e}") from e
        table = data.get("table", fqn.upper())
        try:
            fmt = int(data.get("format", 1))  # unstamped docs predate stamping: format 1
        except (TypeError, ValueError) as e:
            raise KnowledgeDocError(
                f"knowledge doc {rel}: 'format' must be an integer, got {data.get('format')!r}"
            ) from e
        doc = {
            "table": table,
            "format": fmt,
            "facts": facts,
            "definition_files": data.get("definition_files", []),
            "notes": _strip_heading(body, table),
            # frontmatter this version does not define — kept verbatim so a
            # rewrite by this grayson never strips what a newer one (or a
            # hand-edit) recorded
            "extra": {k: v for k, v in data.items() if k not in _KNOWN_FRONT_KEYS},
        }
        for key, default in _PROFILE_DEFAULTS.items():
            doc[key] = data.get(key, default.copy() if isinstance(default, list) else default)
        # Hand-edited files write these in looser shapes; normalize once here so
        # every consumer (graph, completeness, templates, agents) sees dicts.
        doc["relationships"] = _norm_relationships(doc.get("relationships"))
        doc["columns"] = _norm_columns(doc.get("columns"))
        return doc

    def fact(self, fqn: str, fact_id: str) -> dict | None:
        return next((f for f in self.read(fqn)["facts"] if f["id"] == fact_id), None)

    # -- write -----------------------------------------------------------

    def _write(self, fqn: str, doc: dict[str, Any]) -> None:
        # A reader may load a newer doc best-effort, but must never REWRITE one:
        # this version writes only the fields it defines, so a rewrite would
        # discard whatever the newer format added. Visible refusal beats silent
        # loss — the invariant that keeps mixed grayson versions safe on one
        # shared library.
        fmt = int(doc.get("format", KNOWLEDGE_FORMAT))
        if fmt > KNOWLEDGE_FORMAT:
            raise KnowledgeDocError(
                f"knowledge doc for {doc.get('table', fqn.upper())} is format {fmt}, newer "
                f"than this grayson writes (format {KNOWLEDGE_FORMAT}) — refusing to "
                "rewrite it. Upgrade grayson to edit this doc, or edit the file by hand."
            )
        path = self.table_path(fqn)
        path.parent.mkdir(parents=True, exist_ok=True)
        front = {"table": doc["table"], "format": KNOWLEDGE_FORMAT}
        for key in PROFILE_KEYS:  # only write populated profile fields (small diffs)
            if doc.get(key):
                front[key] = doc[key]
        for key, value in (doc.get("extra") or {}).items():
            front.setdefault(key, value)  # round-trip, but never clobber a defined key
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
        from grayson.util import atomic_write_text

        atomic_write_text(path, text)

    def add_fact(
        self,
        fqn: str,
        fact_text: str,
        fact_id: str | None = None,
        status: FactStatus = "proposed",
        created_by: str = "agent",
        evidence: list[str] | None = None,
        author: str | None = None,
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
        from grayson.identity import get_user_id

        fact = Fact(
            id=fid,
            fact=fact_text,
            status=status,
            created_by=created_by,
            evidence=evidence or [],
            author=author or get_user_id(),
        )
        doc["facts"].append(fact.model_dump())
        self._write(fqn, doc)
        return fact.model_dump()

    def answer_open_question(
        self,
        fqn: str,
        question: str,
        answer: str,
        status: FactStatus = "proposed",
        created_by: str = "agent",
        evidence: list[str] | None = None,
    ) -> dict:
        """Resolve one open question with an answer, atomically: the answer is
        recorded as a fact (question and answer together, so it reads standalone
        in future briefings) and the question leaves the open list.

        No session required — this is the lightweight path for a question a
        human can simply answer. Provenance rules unchanged: an agent relaying
        the user's answer records it `proposed`; confirmation stays a user
        action (console or `knowledge confirm`)."""
        doc = self.read(fqn)
        open_qs = [str(q) for q in doc.get("open_questions") or []]
        needle = question.strip().lower()
        matches = [q for q in open_qs if q.strip().lower() == needle]
        if not matches:  # substring convenience, but only when unambiguous
            matches = [q for q in open_qs if needle in q.lower()]
        if not matches:
            raise KeyError(
                f"no open question matching {question!r} on {fqn.upper()} "
                f"(open: {open_qs or 'none'})"
            )
        if len(matches) > 1:
            raise ValueError(
                f"{question!r} matches {len(matches)} open questions on {fqn.upper()} — "
                f"be more specific: {matches}"
            )
        resolved = matches[0]
        fact_text = f"{resolved.rstrip('?')}? — {answer.strip()}"
        fact = self.add_fact(
            fqn, fact_text, status=status, created_by=created_by, evidence=evidence
        )
        doc = self.read(fqn)  # re-read: add_fact rewrote the doc
        doc["open_questions"] = [q for q in (doc.get("open_questions") or []) if str(q) != resolved]
        self._write(fqn, doc)
        return {
            "question": resolved,
            "fact": fact,
            "open_questions_left": len(doc["open_questions"]),
        }

    def confirm_fact(self, fqn: str, fact_id: str, by: str = "user") -> dict:
        from grayson.identity import get_user_id

        doc = self.read(fqn)
        for f in doc["facts"]:
            if f["id"] == fact_id:
                f["status"] = "user_confirmed"
                # a generic 'user' resolves to the configured id when one is set,
                # so shared-library history names the confirmer
                f["confirmed_by"] = (get_user_id() or by) if by == "user" else by
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
        if "relationships" in updates:
            rels = updates["relationships"]
            if not isinstance(rels, list) or not all(
                (isinstance(r, dict) and (r.get("to") or r.get("table")))
                or (isinstance(r, str) and r.strip())
                for r in rels
            ):
                raise ValueError(
                    "relationships must be a list of objects with at least a 'to' table "
                    "(a bare 'DB.SCHEMA.TABLE' string is accepted as shorthand)"
                )
            updates = {**updates, "relationships": _norm_relationships(rels)}
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

    # -- lint --------------------------------------------------------------

    def lint(self) -> dict:
        """Validate every knowledge doc against the current format.

        Hand edits and merges are first-class ways to write the library, so
        drift is normal — this is how a team finds it on demand instead of
        accumulating it: the doc that no longer parses, the fact id a merge
        duplicated, the doc a newer grayson wrote that this version cannot
        rewrite. Errors mean broken; warnings mean working but worth a look;
        unstamped is informational (stamped on the next write or migrate).
        """
        errors: list[dict] = []
        warnings: list[dict] = []
        unstamped: list[str] = []
        checked = 0
        if not self.dir.is_dir():
            return {"ok": True, "checked": 0, "errors": [], "warnings": [], "unstamped": []}
        for path in sorted(self.dir.rglob("*.md")):
            if path.name == "glossary.md":
                continue
            rel = str(path.relative_to(self.dir))
            parts = path.relative_to(self.dir).with_suffix("").parts
            if len(parts) != 3:
                warnings.append(
                    {
                        "file": rel,
                        "problem": "not at DB/SCHEMA/TABLE.md depth — invisible to "
                        "table listings and search",
                    }
                )
                continue
            fqn = ".".join(parts)
            checked += 1
            try:
                doc = self.read(fqn)
            except ValueError as e:  # KnowledgeDocError included
                errors.append({"file": rel, "problem": str(e)})
                continue
            stored = self._stored_format(fqn)
            if stored is None:
                unstamped.append(fqn.upper())
            elif stored > KNOWLEDGE_FORMAT:
                warnings.append(
                    {
                        "file": rel,
                        "problem": f"format {stored} is newer than this grayson writes "
                        f"({KNOWLEDGE_FORMAT}) — readable, but read-only until you upgrade",
                    }
                )
            if str(doc["table"]).upper() != fqn.upper():
                warnings.append(
                    {
                        "file": rel,
                        "problem": f"frontmatter table '{doc['table']}' disagrees with the "
                        f"path ('{fqn.upper()}') — was the file moved by hand?",
                    }
                )
            ids = [f["id"] for f in doc["facts"]]
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                errors.append(
                    {
                        "file": rel,
                        "problem": f"duplicate fact id(s) {dupes} — usually a merge gone "
                        "wrong; ids must be unique per table",
                    }
                )
        return {
            "ok": not errors,
            "checked": checked,
            "errors": errors,
            "warnings": warnings,
            "unstamped": unstamped,
        }

    # -- format migration -------------------------------------------------

    def _stored_format(self, fqn: str) -> int | None:
        """The format actually written in the file (None when missing/unstamped),
        as opposed to read()'s normalized view where unstamped means 1."""
        path = self.table_path(fqn)
        if not path.is_file():
            return None
        front, _ = _split_frontmatter(path.read_text(encoding="utf-8"))
        try:
            data = yaml.safe_load(front) or {} if front else {}
        except yaml.YAMLError:
            return None
        raw = data.get("format") if isinstance(data, dict) else None
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def migrate(self) -> dict:
        """Rewrite every table doc to the current format. Deliberate-only: this
        is invoked by `grayson library migrate` (which insists on a clean git
        tree and lands the result as one labeled, revertible commit) — never
        implicitly on read. Idempotent: an up-to-date doc is left untouched."""
        migrated, up_to_date, errors = [], [], []
        for fqn in self.all_tables():
            try:
                if self._stored_format(fqn) == KNOWLEDGE_FORMAT:
                    up_to_date.append(fqn)
                    continue
                self._write(fqn, upgrade_doc(self.read(fqn)))
                migrated.append(fqn)
            except KnowledgeDocError as e:
                # one broken or too-new doc must not abort the rest of the sweep
                errors.append({"table": fqn, "error": str(e)})
        return {
            "format": KNOWLEDGE_FORMAT,
            "migrated": migrated,
            "up_to_date": len(up_to_date),
            "errors": errors,
        }

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


def upgrade_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Apply FORMAT_STEPS in sequence until the doc is at the current format.

    A doc already current (or newer) passes through unchanged — _write is the
    gate that refuses newer formats. A gap in the ladder is a release bug and
    says so."""
    fmt = int(doc.get("format", 1))
    while fmt < KNOWLEDGE_FORMAT:
        step = FORMAT_STEPS.get(fmt)
        if step is None:
            raise KnowledgeDocError(
                f"no migration step from knowledge format {fmt} to {fmt + 1} — "
                "this is a grayson release bug, not a problem with your library"
            )
        doc = step(doc)
        fmt = doc["format"] = fmt + 1
    return doc


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


def _norm_relationships(value: object) -> list[dict]:
    """Canonicalize relationship entries: dicts pass through (with 'table'/'join'
    accepted as aliases for 'to'/'on'), a bare string is shorthand for its 'to'
    table, anything else is dropped rather than crashing a reader."""
    out: list[dict] = []
    for rel in value if isinstance(value, list) else []:
        if isinstance(rel, dict):
            rel = dict(rel)
            if not rel.get("to") and rel.get("table"):
                rel["to"] = rel.pop("table")
            if not rel.get("on") and rel.get("join"):
                rel["on"] = rel.pop("join")
            if rel.get("to"):
                out.append(rel)
        elif isinstance(rel, str) and rel.strip():
            out.append({"to": rel.strip()})
    return out


def _norm_columns(value: object) -> list[dict]:
    """Canonicalize column entries: dicts with a name pass through, a bare
    string is shorthand for an undescribed column, anything else is dropped."""
    out: list[dict] = []
    for col in value if isinstance(value, list) else []:
        if isinstance(col, dict) and col.get("name"):
            out.append(col)
        elif isinstance(col, str) and col.strip():
            out.append({"name": col.strip()})
    return out


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
