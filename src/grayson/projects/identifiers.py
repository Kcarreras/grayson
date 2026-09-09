"""Physical project objects must have stable, unquoted Snowflake names."""

import re

_PART = r"[A-Za-z_][A-Za-z0-9_$]{0,254}"
_OBJECT = re.compile(rf"{_PART}\.{_PART}\.{_PART}")


def is_project_object_name(value: str) -> bool:
    return _OBJECT.fullmatch(value) is not None
