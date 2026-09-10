"""Consistent, actionable errors for user-supplied CLI files and payloads."""

import json
from pathlib import Path

import yaml


def read_text(file: Path) -> str:
    """Accept UTF-8 files from Windows editors, including an optional BOM."""
    try:
        return file.read_text(encoding="utf-8-sig")
    except UnicodeError as e:
        raise ValueError(f"cannot read {file}: save the file as UTF-8 and try again") from e
    except OSError as e:
        raise ValueError(f"cannot read {file}: {e.strerror or e}") from e


def parse_json(raw: str, *, label: str = "payload", object_only: bool = True):
    if not raw.strip():
        raise ValueError(f"no {label}: use --file, --json, or pipe JSON via stdin")
    try:
        value = json.loads(raw.lstrip("\ufeff"))
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e.msg} (line {e.lineno}, column {e.colno})") from e
    if object_only and not isinstance(value, dict):
        raise ValueError(f'{label} must be a JSON object, such as {{"field": "value"}}')
    return value


def read_spec(file: Path, *, expected: type = dict):
    raw = read_text(file)
    if not raw.strip():
        raise ValueError(f"{file} is empty: provide a JSON or YAML specification")
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ValueError(f"invalid JSON/YAML in {file}{location}: check its syntax") from e
    if not isinstance(value, expected):
        shape = "object (mapping)" if expected is dict else "list"
        raise ValueError(f"{file} must contain a JSON/YAML {shape}")
    return value
