"""Library findings schemas: named, shareable extensions of the built-ins.

The six built-in schemas are fixed with the release. A team's own schema is
a YAML file in the library's `findings_schemas/` directory that names a
built-in as its `base` and extends it — never replaces it: the base fields
every finding carries, the calibration rules, and the base schema's required
fields stay, so no library schema can be weaker than the built-in it starts
from, and findings stay comparable across the whole library.

What a library schema adds beyond a workflow's own `findings_fields` is that
it is shared (several workflows name it), owned (author-only edits, lineage
on forks), and can branch: one field's value selects which further fields a
finding needs — the honest partial result (`outcome: inconclusive`) gets a
shape of its own instead of being forced into a confident one.

Effective schema for a finding = built-in base → library schema → the
workflow's own fields, one merge rule throughout (a later layer tightens a
field the earlier one requires; it never relaxes it).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from grayson.findings.schemas import BASE_FIELD_KEYS, FINDINGS_SCHEMAS

_ROUND_TRIP = ConfigDict(extra="allow")
_FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SCHEMA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DIR_NAME = "findings_schemas"


class FindingField(BaseModel):
    """A field a schema or workflow requires (or documents) in a finding's `extra`.

    `choices` closes the value set; `required: false` documents a field for
    the agent without gating on it. A key that matches a field an earlier
    layer already requires tightens that field (description and choices)
    rather than adding a second one.
    """

    model_config = _ROUND_TRIP

    key: str
    description: str = ""
    required: bool = True
    choices: list[str] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _key_shape(cls, v: str) -> str:
        if not _FIELD_KEY_RE.match(v):
            raise ValueError(
                f"findings field key '{v}' must be lowercase letters, digits or '_' "
                "(starting with a letter), e.g. owner_team"
            )
        if v in BASE_FIELD_KEYS:
            raise ValueError(
                f"findings field '{v}' is a base field every finding already carries — "
                "extra fields live in `extra` and need their own key"
            )
        return v

    @field_validator("choices")
    @classmethod
    def _choices(cls, v: list[str]) -> list[str]:
        cleaned = [str(c).strip() for c in v if str(c).strip()]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("findings field choices repeat a value")
        return cleaned

    def label(self) -> str:
        """One line for previews and the console."""
        quals = []
        if self.choices:
            quals.append(f"one of: {' | '.join(self.choices)}")
        if not self.required:
            quals.append("optional")
        head = self.key + (f" ({'; '.join(quals)})" if quals else "")
        desc = " ".join(self.description.split())
        return f"{head}: {desc}" if desc else head


def _unique_keys(fields: list[FindingField], where: str) -> None:
    keys = [f.key for f in fields]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        raise ValueError(f"duplicate findings field key(s) in {where}: {', '.join(dupes)}")


class LibrarySchema(BaseModel):
    model_config = _ROUND_TRIP

    name: str
    title: str = ""
    description: str = ""
    #: the built-in this schema extends. Only built-ins: a chain of library
    #: schemas would make "what does a finding need" a lineage walk.
    base: str = "standard_v1"
    fields: list[FindingField] = Field(default_factory=list)
    #: one of `fields`, with `choices`: its value selects a branch. Allowed
    #: only when the base does not branch already — one discriminator per
    #: schema keeps the contract readable and the errors specific.
    discriminator: str = ""
    #: choice value -> the further fields that value requires
    branches: dict[str, list[FindingField]] = Field(default_factory=dict)
    created_by: str = ""
    forked_from: str = ""

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not SCHEMA_NAME_RE.match(v):
            raise ValueError(
                "schema name must be 1-64 lowercase letters, digits or '_' (starting with "
                "a letter), e.g. orders_triage_v1"
            )
        return v

    @field_validator("base")
    @classmethod
    def _base(cls, v: str) -> str:
        if v not in FINDINGS_SCHEMAS:
            known = ", ".join(sorted(FINDINGS_SCHEMAS))
            raise ValueError(f"base must be a built-in schema (known: {known}), got '{v}'")
        return v

    @model_validator(mode="after")
    def _shape(self) -> LibrarySchema:
        _unique_keys(self.fields, "fields")
        top = {f.key: f for f in self.fields}
        base_spec = FINDINGS_SCHEMAS[self.base]
        base_disc = base_spec.get("discriminator") or ""
        if self.branches and not self.discriminator:
            raise ValueError("branches need a discriminator — the field whose value selects one")
        if self.discriminator:
            if base_disc:
                raise ValueError(
                    f"base '{self.base}' already branches on '{base_disc}' — a schema "
                    "carries one discriminator; extend a base that does not branch"
                )
            field = top.get(self.discriminator)
            if field is None:
                raise ValueError(
                    f"discriminator '{self.discriminator}' is not one of this schema's fields"
                )
            if not field.choices:
                raise ValueError(
                    f"discriminator '{self.discriminator}' needs `choices` — the values a "
                    "branch can be selected by"
                )
            if not field.required:
                raise ValueError(f"discriminator '{self.discriminator}' must be required")
            unknown = [v for v in self.branches if v not in field.choices]
            if unknown:
                raise ValueError(
                    f"branch value(s) {', '.join(unknown)} are not choices of "
                    f"'{self.discriminator}' ({', '.join(field.choices)})"
                )
        for value, fields in self.branches.items():
            _unique_keys(fields, f"branch '{value}'")
            clash = [f.key for f in fields if f.key in top]
            if clash:
                raise ValueError(
                    f"branch '{value}' repeats top-level field(s) {', '.join(clash)} — a "
                    "field is unconditional or branch-specific, not both"
                )
        return self

    def branch_values(self) -> list[str]:
        """Every value the discriminator can take, branches declared or not."""
        if not self.discriminator:
            return []
        field = next((f for f in self.fields if f.key == self.discriminator), None)
        return list(field.choices) if field else []


class SchemaNotFound(KeyError):
    pass


def schemas_dir_beside(workflows_dir: Path | None) -> Path | None:
    """The library's findings_schemas/ directory, given its workflows/ one —
    both live at the library root, so callers holding a workflows_dir (the
    engine, the registry) reach the schemas without a second parameter."""
    return None if workflows_dir is None else workflows_dir.parent / DIR_NAME


_file_cache: dict[Path, tuple[float, LibrarySchema]] = {}


def _load_file(path: Path) -> LibrarySchema:
    mtime = path.stat().st_mtime
    cached = _file_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema = LibrarySchema.model_validate(data)
    _file_cache[path] = (mtime, schema)
    return schema


@lru_cache(maxsize=1)
def core_schema_names() -> frozenset[str]:
    return frozenset(FINDINGS_SCHEMAS)


def load_schema_report(schemas_dir: Path | None) -> tuple[dict[str, LibrarySchema], list[dict]]:
    """Load library schema files, returning (loadable schemas, problems).

    Same contract as the workflow registry: a file with a problem is excluded
    from the loadable set, and every exclusion is reported, never silent.
    """
    out: dict[str, LibrarySchema] = {}
    problems: list[dict] = []

    def problem(path: Path, message: str, name: str | None = None) -> None:
        problems.append({"file": path.name, "name": name, "problem": message})

    if schemas_dir is None or not schemas_dir.is_dir():
        return out, problems
    for path in sorted(schemas_dir.glob("*.yaml")):
        try:
            schema = _load_file(path)
        except yaml.YAMLError as e:
            problem(path, f"YAML does not parse: {e}")
            continue
        except ValueError as e:
            problem(path, f"does not validate as a findings schema: {e}")
            continue
        if schema.name in core_schema_names():
            problem(
                path,
                f"shadows the built-in schema '{schema.name}' — built-ins are canonical; "
                "extend it under a new name instead",
                schema.name,
            )
            continue
        if schema.name in out:
            problem(
                path,
                f"duplicate schema name '{schema.name}' (already defined by another file)",
                schema.name,
            )
            continue
        out[schema.name] = schema
    return out, problems


def schema_problems(schemas_dir: Path | None) -> list[dict]:
    return load_schema_report(schemas_dir)[1]


def list_library_schemas(schemas_dir: Path | None) -> list[LibrarySchema]:
    loaded, _ = load_schema_report(schemas_dir)
    return [loaded[name] for name in sorted(loaded)]


def get_library_schema(name: str, schemas_dir: Path | None) -> LibrarySchema:
    loaded, problems = load_schema_report(schemas_dir)
    if name in loaded:
        return loaded[name]
    for p in problems:
        if p.get("name") == name:
            raise SchemaNotFound(
                f"findings schema '{name}' exists in the library but is not loadable: "
                f"{p['problem']} (file: {p['file']}; run `grayson schema lint`)"
            )
    known = ", ".join(sorted({*core_schema_names(), *loaded}))
    raise SchemaNotFound(f"unknown findings schema '{name}' (known: {known})")


def known_schema(name: str, schemas_dir: Path | None) -> bool:
    """Built-in, or a loadable library schema."""
    if name in core_schema_names():
        return True
    loaded, _ = load_schema_report(schemas_dir)
    return name in loaded


def known_schema_names(schemas_dir: Path | None) -> list[str]:
    loaded, _ = load_schema_report(schemas_dir)
    return sorted({*core_schema_names(), *loaded})
