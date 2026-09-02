"""Knowledge library: one markdown file per table, YAML frontmatter facts.

Team-shareable by design — each fact carries provenance (who confirmed it, when,
on what evidence) and one fact per list entry keeps diffs small and merges clean.
Facts are the durable, agent-readable tribal knowledge that makes each QA session
faster and more reliable than the last.
"""

from __future__ import annotations

import hashlib
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
_KNOWN_FRONT_KEYS = {
    "table",
    "format",
    "facts",
    "definition_files",
    "definitions",
    "structure",
    *PROFILE_KEYS,
}

#: where a table is defined, structured. `definition_files` (bare paths) is the
#: format-1 spelling and is still written, derived from these, so an older
#: grayson or a hand reader sees the same list ("a rename writes both names").
#: An entry names a work-repo file (`path`), a captured copy beside the doc
#: (`snapshot`), or both; `kind` says what it is (dbt_model, view, ddl, job, ...),
#: `hash` fingerprints the text it was captured from so a later pass can say
#: "changed since", and `captured_at` dates the observation.
DEFINITION_KINDS = {"dbt_model", "dbt_seed", "dbt_snapshot", "view", "ddl", "job", "other"}
#: sidecar snapshots live beside the doc as <TABLE>.<kind>.sql — a sidecar is
#: a dated copy of derived state, never the doc itself, so it can be large and
#: regenerated without touching the merge-friendly facts
SNAPSHOT_SUFFIXES = {"dbt": ".dbt.sql", "ddl": ".ddl.sql"}
#: how much of a snapshot rides along in `knowledge show` (the file holds it all)
SNAPSHOT_INLINE_CHARS = 20_000


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
                "definitions": [],
                "structure": {},
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
        definitions = _norm_definitions(data.get("definitions"), data.get("definition_files"))
        doc = {
            "table": table,
            "format": fmt,
            "facts": facts,
            "definitions": definitions,
            # the format-1 spelling, always consistent with `definitions`
            "definition_files": [d["path"] for d in definitions if d.get("path")],
            "structure": data["structure"] if isinstance(data.get("structure"), dict) else {},
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
        definitions = _norm_definitions(doc.get("definitions"), doc.get("definition_files"))
        if definitions:
            front["definitions"] = definitions
        if doc.get("structure"):
            front["structure"] = doc["structure"]
        front |= {
            "definition_files": [d["path"] for d in definitions if d.get("path")],
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
        allowed = {*PROFILE_KEYS, "definition_files", "definitions", "notes"}
        bad = set(updates) - allowed
        if bad:
            raise ValueError(f"unknown profile fields: {sorted(bad)} (allowed: {sorted(allowed)})")
        if "definitions" in updates or "definition_files" in updates:
            # both spellings land as structured entries; setting either replaces the
            # path-bearing entries, and captured snapshots (no path) are kept
            incoming = _norm_definitions(
                updates.get("definitions"), updates.get("definition_files")
            )
            for d in incoming:
                _validate_definition(d)
            current = self.read(fqn)["definitions"]
            kept = [d for d in current if not d.get("path")]
            updates = {
                k: v for k, v in updates.items() if k not in ("definitions", "definition_files")
            }
            updates["definitions"] = _merge_definitions(kept, incoming)
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
        """The format-1 way to say where a table is defined: bare paths. They
        become structured entries (kind unknown); snapshots already captured
        stay."""
        return self.set_profile(fqn, {"definition_files": list(dict.fromkeys(files))})

    def upsert_definition(self, fqn: str, entry: dict) -> dict:
        """Add or refresh one definition entry, matched by path (or, for a
        path-less snapshot, by kind). Other entries are untouched — this is
        how an ingester records what it found without discarding what a human
        pointed at."""
        entry = _norm_definitions([entry], None)
        if not entry:
            raise ValueError("a definition needs at least a 'path' or a 'snapshot'")
        _validate_definition(entry[0])
        doc = self.read(fqn)
        doc["definitions"] = _merge_definitions(doc["definitions"], entry)
        self._write(fqn, doc)
        return self.read(fqn)

    # -- snapshots (sidecar copies of a definition) -------------------------

    def snapshot_path(self, fqn: str, kind: str) -> Path:
        suffix = SNAPSHOT_SUFFIXES.get(kind)
        if suffix is None:
            raise ValueError(f"unknown snapshot kind {kind!r} (kinds: {sorted(SNAPSHOT_SUFFIXES)})")
        return self.table_path(fqn).with_suffix(suffix)

    def write_snapshot(self, fqn: str, kind: str, text: str, header: str = "") -> dict:
        """Write a captured definition beside the doc and return the entry fields
        that describe it (snapshot name, hash of the text, capture time). The
        header — who captured it, from what — goes in as SQL comment lines so
        the file reads standalone."""
        from grayson.util import atomic_write_text

        path = self.snapshot_path(fqn, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = text.strip() + "\n"
        lines = [f"-- {line}" for line in header.strip().splitlines() if line.strip()]
        atomic_write_text(path, ("\n".join(lines) + "\n\n" if lines else "") + body)
        return {"snapshot": path.name, "hash": text_hash(text), "captured_at": utcnow()}

    def read_snapshot(self, fqn: str, name: str) -> str | None:
        """A snapshot's text by its file name (as recorded on the entry); None
        when the file is gone. Names are confined to the doc's own directory."""
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        path = self.table_path(fqn).parent / name
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    # -- structure (columns observed from the warehouse) ----------------------

    def sync_columns(
        self,
        fqn: str,
        live: list[dict],
        source: str = "describe",
        evidence: list[str] | None = None,
    ) -> dict:
        """Merge the warehouse's view of the columns into the doc: machine fields
        (type, nullable, order) come from the warehouse, human fields
        (description and anything else) are preserved. A recorded column the
        warehouse no longer has keeps its description and is flagged `dropped`;
        one with nothing human on it is simply removed. Records when and how
        the structure was observed under `structure`."""
        doc = self.read(fqn)
        drift = column_drift(doc, live)
        recorded = {str(c["name"]).upper(): dict(c) for c in doc["columns"]}
        merged: list[dict] = []
        for col in live:
            name = str(col["name"])
            entry = recorded.pop(name.upper(), {"name": name})
            entry["name"] = name
            if col.get("type"):
                entry["type"] = str(col["type"])
            if col.get("nullable") is not None:
                entry["nullable"] = bool(col["nullable"])
            entry.pop("dropped", None)
            merged.append(entry)
        removed: list[str] = []
        for name, entry in recorded.items():
            human = {k: v for k, v in entry.items() if k not in _MACHINE_COLUMN_KEYS and v}
            if human:
                entry["dropped"] = True
                merged.append(entry)
            else:
                removed.append(name)
        doc["columns"] = merged
        doc["structure"] = {
            "observed_at": utcnow(),
            "source": source,
            **({"evidence": list(evidence)} if evidence else {}),
        }
        self._write(fqn, doc)
        return {
            "table": doc["table"],
            "columns_total": len(merged),
            "added": drift["added"],
            "dropped": drift["dropped"],
            "type_changed": drift["type_changed"],
            "removed": removed,
            "first_observation": drift["status"] == "unrecorded",
            "structure": doc["structure"],
        }

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
            for d in doc["definitions"]:
                snap = d.get("snapshot")
                if snap and self.read_snapshot(fqn, str(snap)) is None:
                    warnings.append(
                        {
                            "file": rel,
                            "problem": f"definition snapshot '{snap}' is referenced but "
                            "missing beside the doc — re-run the capture or drop the entry",
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
    if not (doc.get("definitions") or doc.get("definition_files")):
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


#: column fields the warehouse owns; everything else on a column entry is human
_MACHINE_COLUMN_KEYS = {"name", "type", "nullable", "dropped"}


def text_hash(text: str) -> str:
    """A short, stable fingerprint of a definition's text (whitespace-insensitive
    at line ends, so an editor's trailing-space churn is not a 'change')."""
    norm = "\n".join(line.rstrip() for line in text.strip().splitlines())
    return "sha256:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def columns_from_describe(rows: list[dict]) -> list[dict]:
    """DESCRIBE TABLE output as column entries ({name, type, nullable}). Snowflake
    and the sandbox both return `name`/`type`/`null?`; casing varies by driver."""
    out: list[dict] = []
    for row in rows:
        upper = {str(k).upper(): v for k, v in row.items()}
        name = str(upper.get("NAME") or upper.get("COLUMN_NAME") or "").strip()
        if not name:
            continue
        kind = str(upper.get("KIND") or "COLUMN").upper()
        if kind != "COLUMN":  # Snowflake lists clustering keys etc. under other kinds
            continue
        null_flag = upper.get("NULL?", upper.get("NULLABLE"))
        nullable: bool | None
        if isinstance(null_flag, bool):
            nullable = null_flag
        elif isinstance(null_flag, str) and null_flag.strip().upper() in {"Y", "N", "YES", "NO"}:
            nullable = null_flag.strip().upper() in {"Y", "YES"}
        else:
            nullable = None
        out.append(
            {"name": name, "type": str(upper.get("TYPE") or "").strip(), "nullable": nullable}
        )
    return out


def _norm_type(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def column_drift(doc: dict[str, Any], live: list[dict]) -> dict[str, Any]:
    """How the recorded column list differs from the warehouse's, right now.

    `unrecorded` means the doc lists no columns (nothing to drift from — that
    is a completeness gap, reported elsewhere). Otherwise: columns the
    warehouse has that the doc does not (`added`), recorded columns the
    warehouse no longer has (`dropped`), and same-name columns whose recorded
    type no longer matches (`type_changed`). Columns already flagged dropped
    on the doc are not counted again.
    """
    recorded = [c for c in doc.get("columns") or [] if not c.get("dropped")]
    if not recorded:
        return {"status": "unrecorded", "added": [], "dropped": [], "type_changed": []}
    rec_by_name = {str(c["name"]).upper(): c for c in recorded}
    live_by_name = {str(c["name"]).upper(): c for c in live}
    added = [str(c["name"]) for c in live if str(c["name"]).upper() not in rec_by_name]
    dropped = [str(c["name"]) for c in recorded if str(c["name"]).upper() not in live_by_name]
    type_changed = []
    for name, rec in rec_by_name.items():
        cur = live_by_name.get(name)
        if cur is None or not rec.get("type") or not cur.get("type"):
            continue
        if _norm_type(rec["type"]) != _norm_type(cur["type"]):
            type_changed.append(
                {"name": str(rec["name"]), "recorded": str(rec["type"]), "live": str(cur["type"])}
            )
    status = "drifted" if (added or dropped or type_changed) else "in_sync"
    return {
        "status": status,
        "added": added,
        "dropped": dropped,
        "type_changed": type_changed,
        "observed_at": (doc.get("structure") or {}).get("observed_at"),
    }


def drift_report(store: KnowledgeStore, live_by_table: dict[str, list[dict]]) -> dict[str, dict]:
    """Column drift for every table with a live column list AND a recorded
    one — the session-start briefing line. Tables the library does not
    describe yet are left out (they are `knowledge_gaps`, not drift)."""
    out: dict[str, dict] = {}
    for fqn, live in live_by_table.items():
        try:
            doc = store.read(fqn)
        except ValueError:
            continue
        drift = column_drift(doc, live)
        if drift["status"] != "unrecorded":
            out[doc["table"]] = drift
    return out


def describe_drift(table: str, drift: dict) -> str:
    """One line a briefing can carry: what moved, by name."""
    parts = []
    if drift.get("added"):
        parts.append(f"{len(drift['added'])} added ({', '.join(drift['added'])}) — undescribed")
    if drift.get("dropped"):
        parts.append(f"{len(drift['dropped'])} dropped ({', '.join(drift['dropped'])})")
    if drift.get("type_changed"):
        changes = ", ".join(
            f"{c['name']} {c['recorded']}→{c['live']}" for c in drift["type_changed"]
        )
        parts.append(f"{len(drift['type_changed'])} type change(s) ({changes})")
    return f"{table}: " + "; ".join(parts)


def _validate_definition(entry: dict) -> None:
    kind = entry.get("kind")
    if kind is not None and str(kind) not in DEFINITION_KINDS:
        raise ValueError(
            f"unknown definition kind {kind!r} (kinds: {', '.join(sorted(DEFINITION_KINDS))})"
        )


def _norm_definitions(value: object, legacy_files: object) -> list[dict]:
    """Canonicalize definition entries: dicts with a path or snapshot pass
    through (string-valued fields as strings), a bare string is shorthand for
    its path, and bare `definition_files` paths not already covered are folded
    in — so a doc written by either spelling reads the same."""
    out: list[dict] = []
    seen: set[str] = set()

    def _add(entry: dict) -> None:
        key = f"path:{entry['path']}" if entry.get("path") else f"snap:{entry.get('snapshot')}"
        if key in seen:
            return
        seen.add(key)
        out.append(entry)

    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            entry = {k: v for k, v in item.items() if v not in (None, "", [])}
            for k in ("path", "snapshot", "kind", "repo", "ref", "hash", "captured_at"):
                if k in entry:
                    entry[k] = str(entry[k])
            if entry.get("path") or entry.get("snapshot"):
                _add(entry)
        elif isinstance(item, str) and item.strip():
            _add({"path": item.strip()})
    for item in legacy_files if isinstance(legacy_files, list) else []:
        if isinstance(item, str) and item.strip():
            _add({"path": item.strip()})
    return out


def _merge_definitions(current: list[dict], incoming: list[dict]) -> list[dict]:
    """Upsert incoming entries into current, matched by path — or, for a
    path-less snapshot, by kind (one captured DDL per table, one dbt copy).
    An incoming entry replaces its match whole: it is the newer observation."""

    def _key(d: dict) -> str:
        if d.get("path"):
            return f"path:{d['path']}"
        return f"snap:{d.get('kind') or d.get('snapshot')}"

    merged = {_key(d): d for d in current}
    for d in incoming:
        merged[_key(d)] = d
    return list(merged.values())


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
