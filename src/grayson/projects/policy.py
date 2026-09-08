"""Project permission ceilings; restrictive settings take effect immediately."""

import json
import tomllib

from grayson.library import read_library_settings

LEVELS = {"guided": 0, "milestones": 1, "bounded": 2}


def effective(session, requested: str, *, override: str | None = None) -> dict:
    run = (
        override
        or json.loads(session.get_meta("project_v1") or "{}").get("approval_override")
        or requested
    )
    config_path = session.workspace.root / "grayson.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    workspace = config.get("projects", {}).get("max_approval", "bounded")
    library = "bounded"
    if session.workspace.config.library_path:
        library = read_library_settings(session.workspace.config.library_path).get(
            "project_max_approval", "milestones"
        )
    values = {"run": run, "workspace": workspace, "library": library}
    for side, value in values.items():
        if value not in LEVELS:
            raise ValueError(f"invalid {side} project approval level {value!r}")
    chosen = min(values.values(), key=LEVELS.get)
    return {
        "approval": chosen,
        "sources": values,
        "human_only": [
            "approve_brief",
            "change_scope",
            "change_criteria",
            "increase_budget",
            "approve_ddl",
            "execute_ddl",
        ],
    }
