"""Creating and editing library findings schemas, with the ownership rules
the workflow library already enforces: built-ins are canonical, a schema
edits in place only for its author, anyone else forks, a legacy file with
no author is anyone's to edit and the first save stamps the editor.

The YAML file stays the source of truth; every write round-trips through
the same LibrarySchema validation the registry loads with.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from grayson.findings.library import (
    SCHEMA_NAME_RE,
    FindingField,
    LibrarySchema,
    core_schema_names,
    load_schema_report,
)
from grayson.findings.schemas import FINDINGS_SCHEMAS, effective_extra
from grayson.util import atomic_write_text, dump_yaml, order_keys, unified_diff_text

SCAFFOLD = """\
name: {name}
title: {title}
description: >
  Say what findings under this schema are for and who reads them — a
  workflow's author picks a schema by this.
base: standard_v1
fields:
  - key: owner_team
    description: Which team owns the fix.
    required: true
    # a closed list keeps a verdict from being hedged into prose
    # choices: [data-platform, finance-eng]
# One field's value can select further fields. Name it here (it must be one
# of `fields`, required, with `choices`) and give each value its branch:
# discriminator: outcome
# branches:
#   fixed:
#     - key: fix_reference
#       description: The change that fixed it.
#   deferred:
#     - key: deferred_until
#       description: When it will be picked up, and by whom.
"""


class SchemaAuthoringError(ValueError):
    pass


def _validate_name(name: str) -> str:
    if not SCHEMA_NAME_RE.match(name):
        raise SchemaAuthoringError(
            "schema name must be 1-64 lowercase letters, digits or '_' (starting with a "
            "letter), e.g. orders_triage_v1"
        )
    return name


def _check_name_free(schemas_dir: Path, name: str) -> None:
    if name in core_schema_names():
        raise SchemaAuthoringError(
            f"'{name}' is a built-in schema — built-ins are canonical; pick a new name"
        )
    loaded, problems = load_schema_report(schemas_dir)
    if name in loaded or any(p.get("name") == name for p in problems):
        raise SchemaAuthoringError(f"a library schema named '{name}' already exists")
    if (schemas_dir / f"{name}.yaml").exists():
        raise SchemaAuthoringError(f"{name}.yaml already exists in findings_schemas/")


def dump_schema(schema: LibrarySchema) -> str:
    known = (
        "name",
        "title",
        "description",
        "created_by",
        "forked_from",
        "base",
        "fields",
        "discriminator",
        "branches",
    )
    return dump_yaml(order_keys(schema.model_dump(), known))


def create_schema(
    schemas_dir: Path,
    name: str,
    base: str | None = None,
    fork_of: str | None = None,
    title: str = "",
    user_id: str | None = None,
) -> Path:
    """Scaffold a new library schema: blank on a built-in base, or a fork of
    an existing library schema (lineage recorded)."""
    _validate_name(name)
    schemas_dir.mkdir(parents=True, exist_ok=True)
    _check_name_free(schemas_dir, name)
    path = schemas_dir / f"{name}.yaml"
    if fork_of:
        from grayson.findings.library import get_library_schema

        if fork_of in core_schema_names():
            raise SchemaAuthoringError(
                f"'{fork_of}' is built in — extend it with `--base {fork_of}` rather than forking"
            )
        source = get_library_schema(fork_of, schemas_dir)  # SchemaNotFound propagates
        schema = source.model_copy(
            update={
                "name": name,
                "title": title or (f"{source.title} (fork)" if source.title else ""),
                "created_by": user_id or "",
                "forked_from": source.name,
            }
        )
        text = dump_schema(schema)
    else:
        base = base or "standard_v1"
        if base not in FINDINGS_SCHEMAS:
            known = ", ".join(sorted(FINDINGS_SCHEMAS))
            raise SchemaAuthoringError(f"base must be a built-in schema (known: {known})")
        default_title = re.sub(r"_v\d+$", "", name).replace("_", " ").title()
        text = SCAFFOLD.format(name=name, title=title or default_title)
        text = text.replace("base: standard_v1", f"base: {base}", 1)
        if user_id:
            text = text.replace("base:", f"created_by: {user_id}\nbase:", 1)
    atomic_write_text(path, text)
    return path


def can_edit_schema(schema: LibrarySchema, user_id: str | None) -> bool:
    if schema.name in core_schema_names():
        return False
    if not schema.created_by:
        return True
    return bool(user_id) and schema.created_by == user_id


def _existing(schemas_dir: Path, name: str) -> LibrarySchema | None:
    path = schemas_dir / f"{name}.yaml"
    if not path.exists():
        return None
    try:
        return LibrarySchema.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValueError):
        return None


def validate_schema_text(
    schemas_dir: Path, name: str, text: str, user_id: str | None
) -> LibrarySchema:
    """Everything a save checks, without the write."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise SchemaAuthoringError(f"YAML does not parse: {e}") from e
    try:
        schema = LibrarySchema.model_validate(data)
    except ValueError as e:
        raise SchemaAuthoringError(f"does not validate as a findings schema: {e}") from e
    if schema.name != name:
        raise SchemaAuthoringError(
            f"the YAML names '{schema.name}' but you are editing '{name}' — renames are a "
            "fork (new file), not an edit"
        )
    if name in core_schema_names():
        raise SchemaAuthoringError(f"'{name}' is a built-in schema — extend it under a new name")
    existing = _existing(schemas_dir, name)
    if existing is not None and not can_edit_schema(existing, user_id):
        raise SchemaAuthoringError(
            f"'{name}' was created by '{existing.created_by}' — fork it under a new name "
            "instead of editing their copy"
        )
    if not schema.created_by and user_id:
        schema = schema.model_copy(update={"created_by": user_id})
    return schema


def save_schema_yaml(schemas_dir: Path, name: str, text: str, user_id: str | None) -> LibrarySchema:
    schema = validate_schema_text(schemas_dir, name, text, user_id)
    schemas_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(schemas_dir / f"{name}.yaml", dump_schema(schema))
    return schema


def save_schema(schemas_dir: Path, schema: LibrarySchema, user_id: str | None) -> LibrarySchema:
    """Write a schema model, under the same rules as a YAML save."""
    return save_schema_yaml(schemas_dir, schema.name, dump_schema(schema), user_id)


def workflows_using(schema_name: str, workflows_dir: Path | None) -> list[str]:
    """Names of the workflows (core and library) that name this schema."""
    from grayson.workflows import list_workflows

    return [t.name for t in list_workflows(workflows_dir) if t.findings_schema == schema_name]


def delete_schema(
    schemas_dir: Path, name: str, user_id: str | None, used_by: list[str] | None = None
) -> Path:
    """Remove a library schema file under the edit rule. A schema still named
    by a workflow stays: every finding those workflows record validates
    against it."""
    if name in core_schema_names():
        raise SchemaAuthoringError(f"'{name}' is built in and cannot be deleted")
    path = schemas_dir / f"{name}.yaml"
    if not path.is_file():
        raise SchemaAuthoringError(f"no library file findings_schemas/{name}.yaml to delete")
    existing = _existing(schemas_dir, name)
    if existing is not None and not can_edit_schema(existing, user_id):
        raise SchemaAuthoringError(
            f"'{name}' was created by '{existing.created_by}' — only its author can delete it"
        )
    if used_by:
        raise SchemaAuthoringError(
            f"'{name}' is the findings schema of {len(used_by)} workflow(s) "
            f"({', '.join(used_by[:5])}) — point them at another schema first"
        )
    path.unlink()
    return path


def diff_schema_yaml(before: str, after: str, name: str) -> str:
    return unified_diff_text(
        before,
        after,
        f"findings_schemas/{name}.yaml (library)",
        f"findings_schemas/{name}.yaml (after save)",
    )


# -- element edits -----------------------------------------------------------

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _bool(op: dict, key: str, default: bool) -> bool:
    if key not in op:
        return default
    raw = op[key]
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "yes", "on", "1")


def parse_choices(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(c).strip() for c in raw if str(c).strip()]
    return [c.strip() for c in re.split(r"[|,\n]+", str(raw or "")) if c.strip()]


def _field_from(op: dict, key: str) -> dict:
    return {
        "key": key,
        "description": str(op.get("description") or "").strip(),
        "required": _bool(op, "required", True),
        "choices": parse_choices(op.get("choices", [])),
    }


def _find(items: list[dict], key: str, what: str) -> int:
    for i, item in enumerate(items):
        if item.get("key") == key:
            return i
    raise SchemaAuthoringError(f"no {what} '{key}' in this schema")


def apply_schema_edit(schema: LibrarySchema, op: dict) -> LibrarySchema:
    """One element operation on a schema, returning the edited schema.

    `op["kind"]`: meta (title, description, base) | field (action upsert |
    delete | move on `fields`, or on a branch when `branch` names a value) |
    discriminator (`key` names the field whose value selects a branch; empty
    clears it and its branches). Validation errors surface as
    SchemaAuthoringError, naming the problem.
    """
    data = schema.model_dump()
    kind = op.get("kind")
    action = op.get("action", "upsert")
    if kind == "meta":
        for key in ("title", "description"):
            if key in op:
                data[key] = str(op[key] or "").strip()
        if "base" in op:
            base = str(op["base"] or "").strip()
            if base not in FINDINGS_SCHEMAS:
                known = ", ".join(sorted(FINDINGS_SCHEMAS))
                raise SchemaAuthoringError(f"base must be a built-in schema (known: {known})")
            data["base"] = base
    elif kind == "field":
        branch = str(op.get("branch") or "").strip()
        if branch:
            items = data.setdefault("branches", {}).setdefault(branch, [])
            what = f"branch '{branch}' field"
        else:
            items = data.setdefault("fields", [])
            what = "field"
        _edit_listed(items, what, op, action)
        if branch and not data["branches"].get(branch):
            data["branches"].pop(branch, None)  # an emptied branch has nothing to say
    elif kind == "discriminator":
        key = str(op.get("key") or "").strip()
        data["discriminator"] = key
        if key != schema.discriminator:
            data["branches"] = {}  # branches belong to the old field's values
    else:
        raise SchemaAuthoringError(f"unknown element kind '{kind}'")
    try:
        return LibrarySchema.model_validate(data)
    except ValueError as e:
        raise SchemaAuthoringError(f"the edit does not validate: {e}") from e


def _edit_listed(items: list[dict], what: str, op: dict, action: str) -> None:
    if action == "delete":
        items.pop(_find(items, op.get("key", ""), what))
        return
    if action == "move":
        i = _find(items, op.get("key", ""), what)
        j = i - 1 if op.get("direction") == "up" else i + 1
        if 0 <= j < len(items):
            items[i], items[j] = items[j], items[i]
        return
    if action != "upsert":
        raise SchemaAuthoringError(f"unknown action '{action}'")
    key = (op.get("key") or "").strip()
    if not _KEY_RE.match(key):
        raise SchemaAuthoringError(
            f"{what} key must be lowercase letters, digits or '_' (starting with a letter), "
            f"e.g. owner_team — got '{key}'"
        )
    orig = (op.get("orig_key") or "").strip()
    existing = (
        next((i for i, it in enumerate(items) if it.get("key") == orig), None) if orig else None
    )
    if key in [it.get("key") for i, it in enumerate(items) if i != existing]:
        raise SchemaAuthoringError(f"a {what} with key '{key}' already exists")
    base = dict(items[existing]) if existing is not None else {}
    base.update(_field_from(op, key))
    if existing is not None:
        items[existing] = base
    else:
        items.append(base)


# -- preview and lint ----------------------------------------------------------


def render_schema_preview(
    schema: LibrarySchema, schemas_dir: Path | None = None, used_by: list[str] | None = None
) -> str:
    """Deterministic, human-readable rendering of a schema for sign-off — the
    effective contract, base and own fields alike, the way an agent's user
    is shown it."""
    heading = f"{schema.name} — {schema.title or schema.name}"
    lines = [heading, "=" * min(72, len(heading))]
    if schema.description.strip():
        lines += [schema.description.strip(), ""]
    provenance = [f"extends {schema.base}"]
    if schema.created_by:
        provenance.append(f"created by {schema.created_by}")
    if schema.forked_from:
        provenance.append(f"forked from {schema.forked_from}")
    lines.append(" | ".join(provenance))
    lines += ["", "Fields — every finding carries the base fields, then these in `extra`"]
    entries = effective_extra(schema.name, None, schemas_dir)
    if not entries:
        # not yet loadable from the directory (a draft): resolve by hand
        entries = effective_extra(schema.base)
        from grayson.findings.schemas import _layer

        _layer(entries, schema.fields, "library")
    for e in entries:
        quals = []
        if e["choices"]:
            quals.append(f"one of: {' | '.join(e['choices'])}")
        if not e["required"]:
            quals.append("optional")
        quals.append("built-in" if e["source"] == "schema" else "this schema's")
        desc = " ".join(e["description"].split())
        lines.append(f"  - {e['key']} ({'; '.join(quals)})" + (f": {desc}" if desc else ""))
    if not entries:
        lines.append("  (none beyond the base fields)")
    base_spec = FINDINGS_SCHEMAS[schema.base]
    if base_spec.get("discriminator"):
        lines += ["", f"Branches on `{base_spec['discriminator']}` (built in to {schema.base})"]
        for value, fields in base_spec.get("conditional_extra", {}).items():
            lines.append(f"  {value}:")
            for k, d in fields:
                lines.append(f"    - {k}: {' '.join(d.split())}")
    if schema.discriminator:
        lines += ["", f"Branches on `{schema.discriminator}`"]
        for value in schema.branch_values():
            lines.append(f"  {value}:")
            fields = schema.branches.get(value, [])
            for f in fields:
                lines.append(f"    - {f.label()}")
            if not fields:
                lines.append("    (no further fields)")
    lines += ["", f"Used by: {', '.join(used_by) if used_by else 'no workflow yet'}"]
    return "\n".join(lines)


def lint_schema(schema: LibrarySchema) -> list[str]:
    """Semantic warnings for one schema."""
    out: list[str] = []
    _lint_schema(schema, lambda message: out.append(message))
    return out


def _lint_schema(schema: LibrarySchema, warn: Callable[[str], None]) -> None:
    if not schema.description.strip():
        warn("no description — workflow authors pick a schema by it")
    if not re.search(r"_v\d+$", schema.name):
        warn(
            f"name '{schema.name}' has no version suffix (like _v1) — findings on record "
            "cite the schema by name, so a tightened schema should be a new one"
        )
    if not schema.fields:
        warn("no fields — this schema adds nothing to its base")
    for f in schema.fields:
        if not f.description.strip():
            warn(f"field '{f.key}' has no description — say what belongs there")
    for value, fields in schema.branches.items():
        for f in fields:
            if not f.description.strip():
                warn(f"branch '{value}' field '{f.key}' has no description")
    if schema.discriminator:
        empty = [v for v in schema.branch_values() if not schema.branches.get(v)]
        if empty and len(empty) == len(schema.branch_values()):
            warn(
                f"'{schema.discriminator}' is the discriminator but no value has a branch — "
                "either give a value its further fields or drop the discriminator"
            )


def lint_schemas(schemas_dir: Path | None, workflows_dir: Path | None = None) -> dict:
    """Lint every library schema file. Returns {ok, checked, errors, warnings}."""
    loaded, problems = load_schema_report(schemas_dir)
    errors = [{**p, "level": "error"} for p in problems]
    warnings: list[dict] = []
    checked: list[str] = []
    if schemas_dir is not None and schemas_dir.is_dir():
        checked = sorted(p.name for p in schemas_dir.glob("*.yaml"))
    for name, schema in loaded.items():
        file = f"{name}.yaml"
        for message in lint_schema(schema):
            warnings.append({"file": file, "name": name, "problem": message, "level": "warning"})
        if workflows_dir is not None and not workflows_using(name, workflows_dir):
            warnings.append(
                {
                    "file": file,
                    "name": name,
                    "problem": "no workflow names this schema — nothing validates against it",
                    "level": "warning",
                }
            )
    return {"ok": not errors, "checked": checked, "errors": errors, "warnings": warnings}


def fields_from_workflow(fields: list[Any]) -> list[FindingField]:
    """A workflow's findings_fields as schema fields (same model, copied)."""
    return [
        FindingField.model_validate(f.model_dump() if hasattr(f, "model_dump") else f)
        for f in fields
    ]
