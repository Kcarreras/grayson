"""Creating and editing library workflows, with the ownership rules enforced.

The rules (server-side, not advisory):
- Core templates are canonical — no library file may take a core name.
- A library workflow edits in place only for its author (matching `grayson
  user` id). Anyone else forks: a new file, a new name, their id as
  `created_by`, lineage recorded in `forked_from`.
- A legacy library file with no `created_by` is editable by anyone (there is
  no author to protect) — the first save stamps the editor's id.

The YAML file stays the source of truth; every write here round-trips through
the same WorkflowTemplate validation the registry loads with.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

import yaml

from grayson.charts.spec import KINDS as CHART_KINDS
from grayson.findings.schemas import FINDINGS_SCHEMAS, effective_extra
from grayson.util import atomic_write_text
from grayson.workflows.models import WorkflowTemplate
from grayson.workflows.registry import core_names, load_override_report

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

SCAFFOLD = """\
name: {name}
title: {title}
description: >
  Say when to use this workflow — agents pick workflows by this description.
suggested_guard_profile: moderate
setup_inputs:
  - key: target_description
    prompt: What should this session investigate, and why?
    required: true
required_checks:
  - key: first_checkpoint
    title: The first evidence-gated checkpoint
    description: >
      What must be demonstrated, with executed queries, before the session
      can advance. Write the intent down — agents close checkpoints better
      when it is explicit.
    uses_inputs: [target_description]
    # Where the checkpoint's content IS a shape, require the picture — the
    # gate then refuses to close without a chart of an allowed kind built
    # from a cited query. Leave it out to keep charting the agent's call.
    # charts:
    #   - kinds: [line, bar]
    #     description: the measure over time, so the onset is visible
suggested_checks:
  - key: a_fundamental
    title: Something worth checking where it applies
    description: >
      Suggested checks carry breadth without gating. Put things here that are
      worth doing on most targets but not all — a required check that does not
      apply to the table in front of the agent gets closed hollow, which is
      exactly what the evidence rail exists to prevent. Keep required_checks to
      the handful without which the investigation is meaningless.
findings_schema: standard_v1
"""


class WorkflowAuthoringError(ValueError):
    pass


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise WorkflowAuthoringError(
            "workflow name must be 1-64 lowercase letters, digits or '-' "
            "(starting with a letter or digit), e.g. orders-slim-health"
        )
    return name


def _check_name_free(workflows_dir: Path, name: str) -> None:
    if name in core_names():
        raise WorkflowAuthoringError(
            f"'{name}' is a core workflow — core templates are canonical; pick a new name"
        )
    loaded, problems = load_override_report(workflows_dir)
    if name in loaded or any(p.get("name") == name for p in problems):
        raise WorkflowAuthoringError(f"a library workflow named '{name}' already exists")
    if (workflows_dir / f"{name}.yaml").exists():
        raise WorkflowAuthoringError(f"{name}.yaml already exists in the library")


class _Dumper(yaml.SafeDumper):
    """Prose as folded blocks, the way the core templates are written, instead
    of quoted scalars with escaped newlines nobody can read in a diff."""


def _str_presenter(dumper: yaml.SafeDumper, value: str) -> yaml.Node:
    style = ">" if ("\n" in value or len(value) > 72) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str_presenter)


def _tidy(value: Any) -> Any:
    """Trailing whitespace off every string, recursively: a YAML block scalar
    keeps its final newline, and a save should not accumulate them."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [_tidy(v) for v in value]
    if isinstance(value, dict):
        return {k: _tidy(v) for k, v in value.items()}
    return value


def _dump(tpl: WorkflowTemplate) -> str:
    """Stable, human-editable YAML: field order matches how people read templates."""
    data = _tidy(tpl.model_dump())
    known = (
        "name",
        "title",
        "description",
        "tags",
        "created_by",
        "forked_from",
        "suggested_guard_profile",
        "suggested_strict_scope",
        "setup_inputs",
        "required_checks",
        "suggested_checks",
        "findings_schema",
        "findings_fields",
    )
    ordered = {key: data[key] for key in known if data.get(key) not in ("", [], None)}
    # unknown top-level fields (a newer grayson's, or a hand edit's) ride at the
    # end rather than being stripped by the rewrite
    ordered.update({k: v for k, v in data.items() if k not in known})
    return yaml.dump(ordered, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=88)


def create_workflow(
    workflows_dir: Path,
    name: str,
    fork_of: str | None = None,
    title: str = "",
    user_id: str | None = None,
) -> Path:
    """Scaffold a new library workflow (blank, or forked from an existing one)."""
    _validate_name(name)
    workflows_dir.mkdir(parents=True, exist_ok=True)
    _check_name_free(workflows_dir, name)
    path = workflows_dir / f"{name}.yaml"
    if fork_of:
        from grayson.workflows.registry import get_workflow

        base = get_workflow(fork_of, workflows_dir)  # WorkflowNotFound propagates
        fork_title = title or (f"{base.title} (fork)" if base.title else "")
        tpl = base.model_copy(
            update={
                "name": name,
                "title": fork_title,
                "created_by": user_id or "",
                "forked_from": base.name,
            }
        )
        text = _dump(tpl)
    else:
        text = SCAFFOLD.format(name=name, title=title or name.replace("-", " ").title())
        if user_id:
            text = text.replace(
                "suggested_guard_profile:", f"created_by: {user_id}\nsuggested_guard_profile:", 1
            )
    atomic_write_text(path, text)
    return path


def can_edit(tpl: WorkflowTemplate, user_id: str | None) -> bool:
    """In-place edit rights: never for core, author-only when authored."""
    if tpl.name in core_names():
        return False
    if not tpl.created_by:
        return True  # legacy file with no author to protect
    return bool(user_id) and tpl.created_by == user_id


def validate_workflow_text(
    workflows_dir: Path, name: str, text: str, user_id: str | None
) -> WorkflowTemplate:
    """Everything `save_workflow_yaml` checks, without the write: the YAML
    parses, validates as a template, keeps its name, names a known schema, has
    unique checkpoint keys, is not a core name, and is the caller's to edit.

    The console runs this on a draft before showing the review step, so what
    the person confirms is exactly what a save would accept.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise WorkflowAuthoringError(f"YAML does not parse: {e}") from e
    try:
        tpl = WorkflowTemplate.model_validate(data)
    except ValueError as e:
        raise WorkflowAuthoringError(f"does not validate as a workflow template: {e}") from e
    if tpl.name != name:
        raise WorkflowAuthoringError(
            f"the YAML names '{tpl.name}' but you are editing '{name}' — renames are a "
            "fork (new file), not an edit"
        )
    if tpl.findings_schema not in FINDINGS_SCHEMAS:
        known = ", ".join(sorted(FINDINGS_SCHEMAS))
        raise WorkflowAuthoringError(
            f"unknown findings_schema '{tpl.findings_schema}' (known: {known})"
        )
    keys = tpl.required_check_keys()
    if len(keys) != len(set(keys)):
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        raise WorkflowAuthoringError(f"duplicate checkpoint keys: {', '.join(dupes)}")
    if name in core_names():
        raise WorkflowAuthoringError(
            f"'{name}' is a core workflow — core templates are canonical; fork it instead"
        )
    existing = _existing(workflows_dir, name)
    if existing is not None and not can_edit(existing, user_id):
        raise WorkflowAuthoringError(
            f"'{name}' was created by '{existing.created_by}' — fork it under a "
            "new name instead of editing their copy"
        )
    if not tpl.created_by and user_id:
        tpl = tpl.model_copy(update={"created_by": user_id})
    return tpl


def _existing(workflows_dir: Path, name: str) -> WorkflowTemplate | None:
    """The library file as it stands, or None when absent or unparseable (a
    broken file has no enforceable author)."""
    path = workflows_dir / f"{name}.yaml"
    if not path.exists():
        return None
    try:
        return WorkflowTemplate.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValueError):
        return None


def save_workflow_yaml(
    workflows_dir: Path, name: str, text: str, user_id: str | None
) -> WorkflowTemplate:
    """Validate and write an edited library workflow file, enforcing ownership.

    `name` is the workflow being edited; the YAML's own `name` must match —
    renames go through fork/create so nothing silently claims another slot.
    """
    tpl = validate_workflow_text(workflows_dir, name, text, user_id)
    workflows_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(workflows_dir / f"{name}.yaml", _dump(tpl))
    return tpl


def delete_workflow(
    workflows_dir: Path, name: str, user_id: str | None, open_sessions: list[str] | None = None
) -> Path:
    """Remove a library workflow file, under the same ownership rule as an edit.

    Core templates cannot go; a colleague's file cannot go under your id; a
    file that no longer parses has no author to protect and can be removed by
    anyone (that is how a broken library file gets cleaned up). A workflow
    with sessions still open on it stays: those sessions resolve their
    checkpoints and schema from the file on every call. Closed sessions keep
    working from their own record.
    """
    if name in core_names():
        raise WorkflowAuthoringError(
            f"'{name}' is a core workflow — core templates ship with grayson and cannot be deleted"
        )
    path = workflows_dir / f"{name}.yaml"
    if not path.is_file():
        raise WorkflowAuthoringError(f"no library file {name}.yaml to delete")
    existing = _existing(workflows_dir, name)
    if existing is not None and not can_edit(existing, user_id):
        raise WorkflowAuthoringError(
            f"'{name}' was created by '{existing.created_by}' — only its author can delete it"
        )
    if open_sessions:
        raise WorkflowAuthoringError(
            f"'{name}' has {len(open_sessions)} open session(s) running on it "
            f"({', '.join(open_sessions[:5])}) — close or abandon them first"
        )
    path.unlink()
    return path


def open_sessions_on(workspace: Any, name: str) -> list[str]:
    """Ids of the sessions in this workspace still open on a workflow."""
    from grayson.core.session import Session

    out: list[str] = []
    for sid in workspace.list_session_ids():
        try:
            meta = Session(workspace, sid).meta_all()
        except (OSError, ValueError):
            continue
        if meta.get("workflow") == name and meta.get("stage") != "closed":
            out.append(sid)
    return out


def diff_yaml(before: str, after: str, name: str) -> str:
    """Unified diff of a workflow file, before and after an edit — the review
    step shows exactly what a save would change, line by line."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"workflows/{name}.yaml (library)",
            tofile=f"workflows/{name}.yaml (after save)",
        )
    )


# -- element-by-element editing ----------------------------------------------
#
# The console edits a workflow one element at a time — its header, one setup
# input, one checkpoint, one findings field — through forms that never show
# YAML. Each form becomes one operation here, applied to the template's dict
# form (so a field this version does not know still rides along), and the
# result re-validates as a whole template before anyone sees a diff of it.


def parse_list(text: str) -> list[str]:
    """Comma- or whitespace-separated keys from a form field."""
    return [t for t in re.split(r"[\s,]+", (text or "").strip()) if t]


def parse_chart_lines(text: str) -> list[dict]:
    """Chart requirements from the form's one-per-line shorthand.

        bar|line: the measure over time, so the onset is visible
        any: whatever shape fits
        null rate per column, ranked

    A line starts with the allowed kinds separated by `|` (or `any`), a
    colon, then what the picture should show; a line with no kinds prefix
    allows any kind. Unknown kinds are refused with the list that exists.
    """
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        kinds: list[str] = []
        description = line
        head, sep, rest = line.partition(":")
        tokens = [t.strip().lower() for t in re.split(r"[|,/ ]+", head) if t.strip()]
        if sep and tokens and all(t in CHART_KINDS or t == "any" for t in tokens):
            kinds = [t for t in tokens if t != "any"]
            description = rest.strip()
        elif sep and re.fullmatch(r"[a-z|,/]+", head.strip().lower()) and rest.strip():
            # a bare word before the colon reads as a kinds prefix; say which
            # kinds exist rather than silently taking "pie" as prose
            unknown = [t for t in tokens if t not in CHART_KINDS and t != "any"]
            raise WorkflowAuthoringError(
                f"unknown chart kind(s) {', '.join(unknown)} in '{line}' "
                f"(kinds: {', '.join(CHART_KINDS)}, or any)"
            )
        out.append({"kinds": kinds, "description": description})
    return out


def format_chart_lines(charts: list[Any]) -> str:
    """The inverse of `parse_chart_lines`, to prefill the form."""
    lines = []
    for c in charts:
        kinds = c.get("kinds") if isinstance(c, dict) else c.kinds
        desc = c.get("description") if isinstance(c, dict) else c.description
        lines.append(f"{'|'.join(kinds) if kinds else 'any'}: {' '.join((desc or '').split())}")
    return "\n".join(lines)


_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _need_key(value: str, what: str) -> str:
    key = (value or "").strip()
    if not _KEY_RE.match(key):
        raise WorkflowAuthoringError(
            f"{what} key must be lowercase letters, digits or '_' (starting with a "
            f"letter), e.g. null_completeness — got '{key}'"
        )
    return key


def _find(items: list[dict], key: str, what: str) -> int:
    for i, item in enumerate(items):
        if item.get("key") == key:
            return i
    raise WorkflowAuthoringError(f"no {what} '{key}' in this workflow")


def _move(items: list[dict], i: int, direction: str) -> None:
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(items):
        items[i], items[j] = items[j], items[i]


def apply_element_edit(tpl: WorkflowTemplate, op: dict) -> WorkflowTemplate:
    """One element operation on a template, returning the edited template.

    `op["kind"]` is meta | input | check | field; `op["action"]` (for
    everything but meta) is upsert | delete | move. Upserts identify the
    element being replaced by `orig_key` (absent for an addition) and take
    the element's fields; moves take `direction` (up | down) and, for checks,
    an optional `to_list` (required | suggested) to move between the two.
    Validation errors surface as WorkflowAuthoringError, naming the problem.
    """
    data = tpl.model_dump()
    kind = op.get("kind")
    action = op.get("action", "upsert")
    if kind == "meta":
        _edit_meta(data, op)
    elif kind == "input":
        _edit_listed(data, "setup_inputs", "setup input", op, action, _input_from)
    elif kind == "check":
        which = op.get("list") or "required"
        if which not in ("required", "suggested"):
            raise WorkflowAuthoringError("check list must be required or suggested")
        if (
            action == "move"
            and op.get("to_list") in ("required", "suggested")
            and op["to_list"] != which
        ):
            src, dst = data[f"{which}_checks"], data[f"{op['to_list']}_checks"]
            i = _find(src, op.get("key", ""), "checkpoint")
            dst.append(src.pop(i))
        else:
            _edit_listed(data, f"{which}_checks", "checkpoint", op, action, _check_from)
    elif kind == "field":
        _edit_listed(data, "findings_fields", "findings field", op, action, _field_from)
    else:
        raise WorkflowAuthoringError(f"unknown element kind '{kind}'")
    try:
        return WorkflowTemplate.model_validate(data)
    except ValueError as e:
        raise WorkflowAuthoringError(f"the edit does not validate: {e}") from e


def _edit_meta(data: dict, op: dict) -> None:
    for key in ("title", "description"):
        if key in op:
            data[key] = str(op[key] or "").strip()
    if "tags" in op:
        data["tags"] = op["tags"] if isinstance(op["tags"], list) else parse_list(op["tags"])
    if "suggested_guard_profile" in op:
        profile = str(op["suggested_guard_profile"] or "").strip()
        if not profile:
            raise WorkflowAuthoringError("suggested guard profile cannot be empty")
        data["suggested_guard_profile"] = profile
    if "suggested_strict_scope" in op:
        raw = str(op["suggested_strict_scope"] or "").strip().lower()
        data["suggested_strict_scope"] = (
            None if raw in ("", "inherit", "none") else raw in ("true", "yes", "on", "1")
        )
    if "findings_schema" in op:
        schema = str(op["findings_schema"] or "").strip()
        if schema not in FINDINGS_SCHEMAS:
            known = ", ".join(sorted(FINDINGS_SCHEMAS))
            raise WorkflowAuthoringError(f"unknown findings_schema '{schema}' (known: {known})")
        data["findings_schema"] = schema


def _edit_listed(data: dict, field: str, what: str, op: dict, action: str, build) -> None:
    items: list[dict] = data.setdefault(field, [])
    if action == "delete":
        items.pop(_find(items, op.get("key", ""), what))
        return
    if action == "move":
        _move(items, _find(items, op.get("key", ""), what), op.get("direction", "down"))
        return
    if action != "upsert":
        raise WorkflowAuthoringError(f"unknown action '{action}'")
    key = _need_key(op.get("key", ""), what)
    orig = (op.get("orig_key") or "").strip()
    existing = (
        next((i for i, it in enumerate(items) if it.get("key") == orig), None) if orig else None
    )
    taken = [it.get("key") for i, it in enumerate(items) if i != existing]
    if key in taken:
        raise WorkflowAuthoringError(f"a {what} with key '{key}' already exists")
    if field.endswith("_checks"):
        other = "suggested_checks" if field == "required_checks" else "required_checks"
        if key in [it.get("key") for it in data.get(other, [])]:
            raise WorkflowAuthoringError(
                f"checkpoint '{key}' is already in {other} — a check is one or the other"
            )
    base = dict(items[existing]) if existing is not None else {}
    base.update(build(op, key))
    if existing is not None:
        items[existing] = base
    else:
        items.append(base)


def _bool(op: dict, key: str, default: bool) -> bool:
    if key not in op:
        return default
    raw = op[key]
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "yes", "on", "1")


def _input_from(op: dict, key: str) -> dict:
    prompt = str(op.get("prompt") or "").strip()
    if not prompt:
        raise WorkflowAuthoringError(
            "a setup input needs a prompt — the question the human answers"
        )
    return {
        "key": key,
        "prompt": prompt,
        "required": _bool(op, "required", True),
        "adds_scope": _bool(op, "adds_scope", False),
    }


def _check_from(op: dict, key: str) -> dict:
    title = str(op.get("title") or "").strip()
    if not title:
        raise WorkflowAuthoringError("a checkpoint needs a title")
    out = {"key": key, "title": title, "description": str(op.get("description") or "").strip()}
    for lst in ("depends_on", "uses_inputs"):
        if lst in op:
            out[lst] = op[lst] if isinstance(op[lst], list) else parse_list(op[lst])
    if "charts" in op:
        out["charts"] = (
            op["charts"] if isinstance(op["charts"], list) else parse_chart_lines(op["charts"])
        )
    return out


def _field_from(op: dict, key: str) -> dict:
    choices = op.get("choices", [])
    if not isinstance(choices, list):
        choices = [c.strip() for c in re.split(r"[|,\n]+", str(choices)) if c.strip()]
    return {
        "key": key,
        "description": str(op.get("description") or "").strip(),
        "required": _bool(op, "required", True),
        "choices": choices,
    }


def render_preview(tpl: WorkflowTemplate) -> str:
    """Deterministic, human-readable rendering of a template for confirmation.

    The standard shape a workflow is shown in before someone commits to it —
    the console renders the same template visually; this is the terminal/chat
    form. Agents drafting workflows paste this to the user for sign-off, so it
    must show everything that matters: what the human is asked up front, what
    gates, in what order, from which answers, and what merely suggests.
    """
    heading = f"{tpl.name} — {tpl.title or tpl.name}"
    lines = [heading, "=" * min(72, len(heading))]
    if tpl.description.strip():
        lines += [tpl.description.strip(), ""]
    if tpl.tags:
        lines.append(f"tags: {', '.join(tpl.tags)}")
    provenance = []
    if tpl.created_by:
        provenance.append(f"created by {tpl.created_by}")
    if tpl.forked_from:
        provenance.append(f"forked from {tpl.forked_from}")
    strict = (
        ""
        if tpl.suggested_strict_scope is None
        else f" | strict scope: {tpl.suggested_strict_scope}"
    )
    lines.append(
        f"guard profile: {tpl.suggested_guard_profile}{strict} | "
        f"findings schema: {tpl.findings_schema}"
        + (f" | {', '.join(provenance)}" if provenance else "")
    )
    lines += ["", "Setup inputs — answers the human gives at session start"]
    for i in tpl.setup_inputs:
        req = "required" if i.required else "optional"
        scope = "; named tables join the session's readable scope" if i.adds_scope else ""
        lines.append(f"  - {i.key} ({req}{scope}): {' '.join(i.prompt.split())}")
    if not tpl.setup_inputs:
        lines.append("  (none)")
    lines += [
        "",
        "Required checks — each gates review until closed with evidence "
        "(or waived by a human with a reason)",
    ]
    for n, c in enumerate(tpl.required_checks, 1):
        qualifiers = []
        if c.depends_on:
            qualifiers.append(f"after: {', '.join(c.depends_on)}")
        if c.uses_inputs:
            qualifiers.append(f"uses: {', '.join(c.uses_inputs)}")
        suffix = f"  [{'; '.join(qualifiers)}]" if qualifiers else ""
        lines.append(f"  {n}. {c.key} — {c.title}{suffix}")
        if c.description.strip():
            lines.append(f"       {' '.join(c.description.split())}")
        for r in c.charts:
            lines.append(f"       requires chart — {r.label()}")
    if not tpl.required_checks:
        lines.append("  (none — nothing will gate; sessions can close without evidence of work)")
    lines += ["", "Suggested checks — breadth; never gate, done where they apply"]
    for c in tpl.suggested_checks:
        charts = f"  [chart: {'; '.join(r.label() for r in c.charts)}]" if c.charts else ""
        lines.append(f"  - {c.key} — {c.title}{charts}")
    if not tpl.suggested_checks:
        lines.append("  (none)")
    extra = effective_extra(tpl.findings_schema, tpl.findings_fields)
    lines += [
        "",
        f"Findings — every finding validates against {tpl.findings_schema}"
        + (" plus this workflow's own fields" if tpl.findings_fields else ""),
    ]
    for e in extra:
        quals = []
        if e["choices"]:
            quals.append(f"one of: {' | '.join(e['choices'])}")
        if not e["required"]:
            quals.append("optional")
        if e["source"] != "schema":
            quals.append("this workflow's")
        head = e["key"] + (f" ({'; '.join(quals)})" if quals else "")
        desc = " ".join(e["description"].split())
        lines.append(f"  - {head}: {desc}" if desc else f"  - {head}")
    if not extra:
        lines.append("  (the base fields only: title, severity, confidence, summary, evidence)")
    lines += [
        "",
        "Session shape: setup inputs -> guarded queries -> required checks (order above) -> "
        f"findings ({tpl.findings_schema}) -> user accepts/rejects -> fixes/verification -> close",
    ]
    return "\n".join(lines)
