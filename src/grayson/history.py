"""Cross-session history helpers, e.g. suggesting a guard profile from past use."""

from __future__ import annotations

from grayson.core.session import Session
from grayson.workspace import Workspace


def suggest_guard_profile(workspace: Workspace, targets: list[str]) -> str | None:
    """The guard profile last used on any of these target tables, if any.

    Lets a new session on a familiar table default to the profile that worked
    there before (last-used wins), instead of only the workflow's suggestion.
    """
    wanted = {t.upper() for t in targets}
    if not wanted:
        return None
    best: tuple[str, str] | None = None  # (created_at, profile)
    for sid in workspace.list_session_ids():
        try:
            s = Session(workspace, sid)
        except (OSError, ValueError):
            continue
        if not (wanted & set(s.targets)):
            continue
        profile = s.get_meta("guard_profile")
        created = s.get_meta("created_at") or ""
        if profile and (best is None or created > best[0]):
            best = (created, profile)
    return best[1] if best else None
