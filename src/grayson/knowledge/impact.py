"""Explicit lineage observations and reproducible, scoped investigation plans."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Literal

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from grayson.checks.regression import RegressionStore, run_checks
from grayson.core.criteria import digest
from grayson.core.session import Session
from grayson.knowledge.standing import StandingContext, effective_standing, parse_ts
from grayson.knowledge.store import KnowledgeStore, text_hash
from grayson.util import ensure_within, is_object_name, utcnow, write_json


class DependencyEdge(BaseModel):
    model_config = ConfigDict(extra="allow")
    upstream: str
    downstream: str
    source: str
    kind: str
    captured_at: str
    observed_at: str | None = None


class DependencyObservation(BaseModel):
    model_config = ConfigDict(extra="allow")
    format: Literal[1]
    source: str
    captured_at: str
    observed_at: str | None = None
    edges: list[DependencyEdge]
    definitions: dict[str, dict]
    unresolved: list[dict]
    changes: list[dict]


def sql_dependencies(sql: str, captured_at: str, source: str) -> dict:
    """Only actual fully qualified relation references; never guess dbt ref() names."""
    tables, unknown = set(), []
    try:
        tree = sqlglot.parse_one(sql, read="snowflake")
        for scope in traverse_scope(tree):
            for item in scope.sources.values():
                if isinstance(item, exp.Table):
                    if item.catalog and item.db and isinstance(item.this, exp.Identifier):
                        tables.add(".".join([item.catalog, item.db, item.name]).upper())
                    else:
                        unknown.append(item.sql())
    except (ValueError, sqlglot.errors.SqlglotError) as e:
        unknown.append(str(e))
    return {
        "format": 1,
        "upstream": sorted(tables),
        "unresolved": unknown,
        "captured_at": captured_at,
        "source": source,
        "coverage": "partial" if unknown else "explicit SQL references only",
    }


def ingest_manifest(store: KnowledgeStore, manifest: dict, repo: str | None = None) -> dict:
    from grayson.knowledge.dbt import _node_fqn

    metadata = manifest.get("metadata") or {}
    source = repo or metadata.get("project_name") or metadata.get("project_id") or "dbt-manifest"
    source_id = digest({"source": source})[:24]
    path = store.dir / "_dependencies" / f"{source_id}.json"
    ensure_within(store.dir, path)
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if previous:
        if previous.get("format") != 1:
            raise ValueError("unsupported dependency format; existing observation was preserved")
        DependencyObservation.model_validate(previous)
    nodes = {**(manifest.get("sources") or {}), **(manifest.get("nodes") or {})}
    edges, unresolved, definitions = [], [], {}
    observed_at = metadata.get("generated_at")
    captured_at = utcnow()

    def physical(uid, trail):
        if uid in trail:
            unresolved.append({"node": uid, "reason": "dependency cycle"})
            return []
        node = nodes.get(uid)
        if not isinstance(node, dict):
            unresolved.append({"node": uid, "reason": "node missing from manifest"})
            return []
        if (node.get("config") or {}).get("materialized") == "ephemeral":
            return [
                table
                for parent in (node.get("depends_on") or {}).get("nodes", [])
                for table in physical(parent, {*trail, uid})
            ]
        name = _node_fqn(node)
        if name.count(".") == 2 and is_object_name(name):
            return [name]
        unresolved.append({"node": uid, "reason": "fully qualified relation unavailable"})
        return []

    for uid, node in nodes.items():
        if (
            not isinstance(node, dict)
            or node.get("resource_type")
            not in {
                "model",
                "seed",
                "snapshot",
                "source",
            }
            or (node.get("config") or {}).get("materialized") == "ephemeral"
        ):
            continue
        children = physical(uid, set())
        if not children:
            continue
        child = children[0]
        code = node.get("compiled_code") or node.get("raw_code")
        definitions[child] = {
            "hash": text_hash(code) if code else None,
            "node": uid,
            "path": node.get("original_file_path"),
        }
        for parent in (node.get("depends_on") or {}).get("nodes", []):
            for upstream in physical(parent, {uid}):
                edges.append(
                    {
                        "upstream": upstream,
                        "downstream": child,
                        "source": source,
                        "kind": "dbt_manifest",
                        "node": uid,
                        "captured_at": captured_at,
                        "observed_at": observed_at,
                    }
                )
    old = (previous or {}).get("definitions", {})
    changes = []
    for table in sorted(set(old) | set(definitions)):
        before, after = old.get(table), definitions.get(table)
        if previous and before != after:
            changes.append(
                {
                    "table": table,
                    "kind": "definition"
                    if before and after
                    else "removed_from_manifest"
                    if before
                    else "added_to_manifest",
                    "before": before,
                    "after": after,
                    "observed_at": observed_at,
                    "source": source,
                }
            )
    value = {
        **(previous or {}),
        "format": 1,
        "source": source,
        "captured_at": captured_at,
        "observed_at": observed_at,
        "edges": edges,
        "definitions": definitions,
        "unresolved": unresolved,
        "changes": changes,
    }
    # Never replace a newer observation with an older manifest.
    old_ts = parse_ts((previous or {}).get("observed_at"))
    new_ts = parse_ts(observed_at)
    if old_ts and (not new_ts or new_ts < old_ts):
        raise ValueError("manifest is older than the recorded dependency observation")
    write_json(path, value)
    return {
        "edges": len(edges),
        "changes": changes,
        "unresolved": unresolved,
        "source": source,
        "observed_at": observed_at,
    }


def _freshness(value: str | None, days: int) -> str:
    ts = parse_ts(value)
    if not ts or ts > datetime.now(UTC):
        return "unknown"
    return "recent" if (datetime.now(UTC) - ts).total_seconds() <= days * 86400 else "stale"


def build_plan(
    session: Session, changed_tables: list[str] | None = None, freshness_days: int = 7
) -> dict:
    if not 1 <= freshness_days <= 3650:
        raise ValueError("freshness_days must be 1–3650")
    workspace = session.workspace
    store = KnowledgeStore(workspace.knowledge_dir)
    edges, changes, errors, observations = [], [], [], []
    for path in sorted((store.dir / "_dependencies").glob("*.json")):
        try:
            ensure_within(store.dir, path)
            observation = json.loads(path.read_text(encoding="utf-8"))
            if observation.get("format") != 1:
                raise ValueError("unsupported dependency format")
            DependencyObservation.model_validate(observation)
            edges.extend(observation["edges"])
            changes.extend(observation["changes"])
            observations.append(
                {
                    "source": observation["source"],
                    "observed_at": observation.get("observed_at"),
                    "captured_at": observation["captured_at"],
                    "unresolved": observation["unresolved"],
                }
            )
        except (ValueError, KeyError, TypeError, OSError) as e:
            errors.append({"file": path.name, "error": str(e)})
    docs = {}
    for table in store.all_tables():
        try:
            doc = store.read(table)
            docs[table] = doc
            for definition in doc["definitions"]:
                change = definition.get("change_v1")
                if change:
                    if change.get("format") != 1:
                        errors.append({"file": table, "error": "unsupported definition change"})
                        continue
                    changes.append({"table": table, "kind": "definition", **change})
                deps = definition.get("dependencies_v1")
                if deps:
                    if deps.get("format") != 1:
                        errors.append(
                            {"file": table, "error": "unsupported definition dependencies"}
                        )
                        continue
                    edges.extend(
                        {
                            "upstream": upstream,
                            "downstream": table,
                            "source": deps["source"],
                            "kind": "recorded_definition",
                            "captured_at": deps["captured_at"],
                            "observed_at": deps["captured_at"],
                        }
                        for upstream in deps["upstream"]
                        if upstream != table
                    )
                    observations.append(
                        {
                            "source": deps["source"],
                            "observed_at": deps["captured_at"],
                            "unresolved": deps["unresolved"],
                        }
                    )
                local = definition.get("path")
                if local and definition.get("hash"):
                    candidate = workspace.root / local
                    # Remote references are not evidence about an unrelated local checkout.
                    if (
                        not definition.get("repo")
                        and candidate.is_file()
                        and candidate.resolve().is_relative_to(workspace.root.resolve())
                    ):
                        current_hash = text_hash(candidate.read_text(encoding="utf-8"))
                        if current_hash != definition["hash"]:
                            changes.append(
                                {
                                    "table": table,
                                    "kind": "local_definition",
                                    "source": local,
                                    "before": definition["hash"],
                                    "after": current_hash,
                                    "observed_at": datetime.fromtimestamp(
                                        candidate.stat().st_mtime, UTC
                                    ).isoformat(),
                                }
                            )
            drift = doc.get("structure", {}).get("change_v1")
            if drift:
                if drift.get("format") != 1:
                    errors.append({"file": table, "error": "unsupported schema change"})
                    continue
                changes.append({"table": table, "kind": "schema", **drift})
        except (ValueError, OSError) as e:
            errors.append({"file": table, "error": str(e)})
    # Session metadata drift predates these optional library additions.
    from grayson.knowledge.store import column_drift

    for table, columns in json.loads(session.get_meta("columns_snapshot") or "{}").items():
        if table in docs:
            drift = column_drift(docs[table], columns)
            if any(drift[k] for k in ("added", "dropped", "type_changed")):
                changes.append(
                    {
                        "table": table,
                        "kind": "session_schema",
                        **drift,
                        "source": "session metadata snapshot",
                        "observed_at": session.get_meta("metadata_snapshot_at"),
                    }
                )
    roots = (
        sorted({t.upper() for t in changed_tables})
        if changed_tables is not None
        else sorted({c["table"] for c in changes if c.get("table") in session.scope_tables})
    )
    if any(not is_object_name(t) or t.count(".") != 2 for t in roots):
        raise ValueError("changed tables must be fully qualified table names")
    changes = [c for c in changes if c.get("table") in roots]
    known_changes = {c["table"] for c in changes}
    changes.extend(
        {"table": t, "kind": "user_selected", "source": "plan request", "observed_at": None}
        for t in roots
        if t not in known_changes
    )
    downstream = defaultdict(list)
    for edge in edges:
        downstream[edge["upstream"]].append(edge)
    affected, selected_edges = set(roots), []
    queue = deque(roots)
    while queue:
        for edge in downstream[queue.popleft()]:
            selected_edges.append(
                {**edge, "freshness": _freshness(edge.get("observed_at"), freshness_days)}
            )
            if edge["downstream"] not in affected:
                affected.add(edge["downstream"])
                queue.append(edge["downstream"])
    inventory = (
        RegressionStore(workspace.checks_dir).inventory(sorted(affected))
        if affected
        else {"checks": [], "errors": []}
    )
    active = [c for c in inventory["checks"] if c["review_current"]]
    assumptions, relationships = [], []
    context = StandingContext.build(workspace.records_dir)
    for table in sorted(affected):
        doc = docs.get(table)
        if not doc:
            continue
        for fact in doc["facts"]:
            standing, reason = effective_standing(fact, doc, context)
            if standing != "retired":
                assumptions.append(
                    {
                        "table": table,
                        "id": fact["id"],
                        "fact": fact["fact"],
                        "standing": standing,
                        "reason": reason,
                        "anchors": fact.get("anchors", []),
                        "coverage": "unknown; table overlap does not prove coverage",
                    }
                )
        relationships.extend({"table": table, **r} for r in doc["relationships"])
    from grayson.records import collect_records, get_record

    history = []
    for record in collect_records(workspace):
        if record.get("kind") not in {"finding", "proposal"}:
            continue
        tables = record.get("targets", [])
        if not tables:  # Older published records carry tables on their evidence.
            full = get_record(workspace, record["session_id"], record["kind"], record["id"])
            tables = [t for q in (full or {}).get("evidence_queries", []) for t in q["tables"]]
        if affected.intersection(t.upper() for t in tables):
            history.append({k: v for k, v in record.items() if k != "payload"})
    coverage = [
        {
            "table": t,
            "check_ids": [c["id"] for c in active if t in c["tables"]],
            "status": "checks_available" if any(t in c["tables"] for c in active) else "unknown",
        }
        for t in sorted(affected)
    ]
    plan = {
        "format": 1,
        "source_session": session.id,
        "generated_at": utcnow(),
        "freshness_days": freshness_days,
        "changed_tables": roots,
        "changes": changes,
        "affected_tables": sorted(affected),
        "dependencies": selected_edges,
        "sources": observations,
        "assumptions": assumptions,
        "relationships": relationships,
        "historical_records": history[:100],
        "history_omitted": max(0, len(history) - 100),
        "checks": active,
        "coverage": coverage,
        "errors": [*errors, *inventory["errors"]],
        "steps": [
            "Inspect the recorded changes and dependency freshness.",
            "Replay the listed approved checks and cite the fresh query ids.",
            "Recheck recorded assumptions and investigate unknown coverage with the harness.",
            "Record findings and propose explicit success criteria for any fix.",
        ],
        "coverage_note": "Explicit dependencies only. Missing edges, old observations, and table "
        "overlap cannot establish complete downstream or behavioral coverage.",
    }
    plan["digest"] = digest({k: v for k, v in plan.items() if k != "generated_at"})
    old = json.loads(session.get_meta("impact_plan_v1") or "null")
    if old and old.get("format") != 1:
        raise ValueError("unsupported saved investigation plan; preserved unchanged")
    session.set_meta("impact_plan_v1", json.dumps(plan))
    session.log_event("agent", "impact_plan_built", plan)
    return plan


def show_plan(session: Session) -> dict:
    plan = json.loads(session.get_meta("impact_plan_v1") or "null")
    if not plan or plan.get("format") != 1:
        raise ValueError("no supported investigation plan; build one first")
    return plan


def launch(session: Session, plan_digest: str) -> dict:
    from grayson.core import engine

    plan = show_plan(session)
    if plan["digest"] != plan_digest or not plan["affected_tables"]:
        raise ValueError("review a current plan with at least one affected table")
    current = build_plan(session, plan["changed_tables"], plan["freshness_days"])
    if current["digest"] != plan_digest:
        raise ValueError("dependencies, assumptions or checks changed; review the refreshed plan")
    # A new investigation owns its scope. Never silently widen the source session.
    child = Session.create(
        session.workspace,
        workflow="pipeline-qa",
        targets=plan["affected_tables"],
        guard=session.guard_settings,
        guard_profile=session.get_meta("guard_profile") or "moderate",
        strict_scope=True,
        connection=session.connection,
        title="Investigate changes: " + ", ".join(plan["changed_tables"]),
        actor="agent",
    )
    engine.seed_from_workflow(child, session.workspace.workflows_dir)
    child.set_meta("investigation_plan_v1", json.dumps(plan))
    session.log_event(
        "agent", "impact_plan_launched", {"session_id": child.id, "digest": plan_digest}
    )
    return {
        "session_id": child.id,
        "plan": plan,
        "next": "Resume this session in your harness with session brief, then impact run-checks.",
    }


def run_plan_checks(session: Session, *, executor=None) -> dict:
    plan = json.loads(session.get_meta("investigation_plan_v1") or "null")
    if not plan or plan.get("format") != 1:
        raise ValueError("this session was not launched from a supported investigation plan")
    store = RegressionStore(session.workspace.checks_dir)
    for c in plan["checks"]:
        if store.read(c["id"]).digest() != c["digest"]:
            raise ValueError(f"check '{c['id']}' changed since planning; build a fresh plan")
    return run_checks(session, [c["id"] for c in plan["checks"]], executor=executor)
