"""Structured human-in-the-loop intervention types.

An intervention is a typed task an agent files for the user: label a sample,
confirm a semantic rule, pick an option, free-respond, or grant tables into
the session's readable scope. The UI renders it as an interactive form; the
agent reads back a structured response. This replaces the CSV round-trip in
the user's current workflow.
"""

from __future__ import annotations

from typing import Any

from grayson.util import is_object_name

INTERVENTION_KINDS = {
    "label_sample",
    "confirm_semantics",
    "choose",
    "free_response",
    "scope_request",
}


class InterventionError(ValueError):
    pass


def build_request(kind: str, payload: dict) -> dict:
    """Validate and normalize an agent's intervention request payload."""
    if kind not in INTERVENTION_KINDS:
        raise InterventionError(
            f"unknown intervention kind '{kind}' (kinds: {', '.join(sorted(INTERVENTION_KINDS))})"
        )
    if kind == "label_sample":
        rows = payload.get("rows")
        labels = payload.get("labels")
        if not isinstance(rows, list) or not rows:
            raise InterventionError("label_sample requires a non-empty 'rows' list")
        if not isinstance(labels, list) or len(labels) < 2:
            raise InterventionError("label_sample requires a 'labels' list of >= 2 options")
        return {
            "rows": rows,
            "labels": [str(x) for x in labels],
            "id_field": payload.get("id_field"),
            "allow_notes": bool(payload.get("allow_notes", True)),
            "instructions": payload.get("instructions", ""),
        }
    if kind == "confirm_semantics":
        statement = payload.get("statement")
        if not statement:
            raise InterventionError("confirm_semantics requires a 'statement' to confirm/deny")
        return {
            "statement": str(statement),
            "context": payload.get("context", ""),
            "sample": payload.get("sample", []),
        }
    if kind == "choose":
        options = payload.get("options")
        if not isinstance(options, list) or len(options) < 2:
            raise InterventionError("choose requires an 'options' list of >= 2")
        return {
            "options": [str(o) for o in options],
            "question": payload.get("question", ""),
            "multi": bool(payload.get("multi", False)),
        }
    if kind == "scope_request":
        # The ask that closes the scope loop: the agent names the tables whose
        # rows it wants and why; the human ticks what to grant. Names are
        # normalized here so the grant matches the guard's scope check.
        raw = payload.get("tables")
        if not isinstance(raw, list) or not raw:
            raise InterventionError("scope_request requires a non-empty 'tables' list")
        tables: list[str] = []
        for item in raw:
            name = str(item).strip().upper()
            if not is_object_name(name):
                raise InterventionError(
                    f"'{item}' is not a table name — use DB.SCHEMA.TABLE, one per entry"
                )
            if name not in tables:
                tables.append(name)
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise InterventionError(
                "scope_request requires a 'reason': what reading these tables settles"
            )
        return {"tables": tables, "reason": reason, "context": payload.get("context", "")}
    # free_response
    return {"question": str(payload.get("question", "")), "context": payload.get("context", "")}


def validate_response(kind: str, request: dict, response: dict) -> dict:
    """Validate a user's response against the request. Returns normalized response."""
    if kind == "label_sample":
        return _validate_labels(request, response)
    if kind == "confirm_semantics":
        decision = response.get("decision")
        if decision not in {"confirm", "deny", "unsure"}:
            raise InterventionError("response.decision must be confirm | deny | unsure")
        return {"decision": decision, "note": response.get("note", "")}
    if kind == "choose":
        selected = response.get("selected")
        valid = set(request.get("options", []))
        if request.get("multi"):
            if not isinstance(selected, list) or not set(selected) <= valid:
                raise InterventionError("response.selected must be a subset of the options")
        elif selected not in valid:
            raise InterventionError("response.selected must be one of the options")
        return {"selected": selected, "note": response.get("note", "")}
    if kind == "scope_request":
        granted = response.get("granted")
        if not isinstance(granted, list):
            raise InterventionError(
                "response.granted must be a list of the requested tables to allow "
                "(empty to decline)"
            )
        requested = request.get("tables", [])
        normalized = [str(g).strip().upper() for g in granted]
        unknown = [g for g in normalized if g not in requested]
        if unknown:
            raise InterventionError(f"response.granted names tables not requested: {unknown}")
        return {
            "granted": list(dict.fromkeys(normalized)),
            "declined": [t for t in requested if t not in normalized],
            "note": response.get("note", ""),
        }
    # free_response
    text = response.get("text")
    if not text:
        raise InterventionError("free_response requires non-empty 'text'")
    return {"text": str(text)}


def _validate_labels(request: dict, response: dict) -> dict:
    labels: list[dict[str, Any]] = response.get("labels")
    if not isinstance(labels, list):
        raise InterventionError("response.labels must be a list of {row_index, label} objects")
    valid_labels = set(request.get("labels", []))
    n_rows = len(request.get("rows", []))
    seen: set[int] = set()
    out = []
    for item in labels:
        idx = item.get("row_index")
        label = item.get("label")
        if not isinstance(idx, int) or not 0 <= idx < n_rows:
            raise InterventionError(f"row_index {idx} out of range 0..{n_rows - 1}")
        if label not in valid_labels:
            raise InterventionError(f"label '{label}' is not one of {sorted(valid_labels)}")
        seen.add(idx)
        out.append({"row_index": idx, "label": label, "note": item.get("note", "")})
    return {"labels": out, "labeled_count": len(seen), "total": n_rows}
