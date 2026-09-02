"""dbt definitions: the transformation behind a table, from the project's manifest.

For a base table, the warehouse's DDL says what columns exist and nothing about
why. The dbt model is the actual definition — and a team that runs dbt already
has it in a manifest.json, beside the run_results grayson ingests for checks.
This reads models, seeds, and snapshots out of that manifest and records, per
table the library documents:

- a `definitions` entry: the model's path in the dbt project (a pointer to the
  repo that owns it), its kind, unique id, package, and a hash of its text, so a
  later ingest can say "the definition changed since this was recorded";
- optionally a dated snapshot of the compiled SQL beside the doc (the copy a
  collaborator served the library without the dbt repo can still read);
- column descriptions from the model's schema.yml, filled only where the doc has
  none — dbt's descriptions are a human's words in another repo, and a grayson
  description already written takes precedence.

Nothing here is confirmed: a definition is a pointer plus a dated observation.
"""

from __future__ import annotations

from typing import Any

from grayson.knowledge.store import KnowledgeStore, text_hash
from grayson.util import utcnow

_RESOURCE_KINDS = {"model": "dbt_model", "seed": "dbt_seed", "snapshot": "dbt_snapshot"}


def looks_like_dbt_manifest(data: object) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("metadata"), dict)
        and "dbt_version" in data["metadata"]
        and isinstance(data.get("nodes"), dict)
    )


def _node_fqn(node: dict) -> str:
    parts = [node.get("database"), node.get("schema"), node.get("alias") or node.get("name")]
    return ".".join(str(p) for p in parts if p).upper()


def definition_nodes(manifest: dict) -> dict[str, dict]:
    """Fully-qualified table -> the manifest node that defines it (models, seeds,
    snapshots; tests and analyses are not definitions)."""
    out: dict[str, dict] = {}
    for unique_id, node in (manifest.get("nodes") or {}).items():
        if not isinstance(node, dict) or node.get("resource_type") not in _RESOURCE_KINDS:
            continue
        fqn = _node_fqn(node)
        if fqn.count(".") == 2:
            out[fqn] = {**node, "unique_id": unique_id}
    return out


def ingest_dbt_definitions(
    store: KnowledgeStore,
    manifest: dict,
    tables: list[str] | None = None,
    everything: bool = False,
    repo: str | None = None,
    snapshot: bool = True,
    fill_descriptions: bool = True,
) -> dict[str, Any]:
    """Record dbt definitions for the tables the library documents (plus any
    named in `tables`), or for every model when `everything` — a manifest can
    name hundreds of models the team never investigates, and a library doc per
    model would bury the ones that matter."""
    nodes = definition_nodes(manifest)
    wanted: set[str]
    if everything:
        wanted = set(nodes)
    else:
        wanted = set(store.all_tables()) | {t.upper() for t in tables or []}
    version = str((manifest.get("metadata") or {}).get("dbt_version") or "")
    captured_at = utcnow()
    updated: list[str] = []
    changed: list[str] = []
    not_in_manifest = sorted(t for t in (tables or []) if t.upper() not in nodes)
    filled = 0
    snapshots = 0
    for fqn in sorted(wanted):
        node = nodes.get(fqn)
        if node is None:
            continue
        text = str(node.get("compiled_code") or node.get("raw_code") or "")
        entry: dict[str, Any] = {
            "path": str(node.get("original_file_path") or node.get("path") or node["unique_id"]),
            "kind": _RESOURCE_KINDS[node["resource_type"]],
            "unique_id": node["unique_id"],
            "captured_at": captured_at,
        }
        if node.get("package_name"):
            entry["package"] = str(node["package_name"])
        if repo:
            entry["repo"] = repo
        if version:
            entry["dbt_version"] = version
        materialized = (node.get("config") or {}).get("materialized")
        if materialized:
            entry["materialized"] = str(materialized)
        if node.get("description"):
            entry["description"] = str(node["description"])[:500]
        if text:
            entry["hash"] = text_hash(text)
            if snapshot:
                snap = store.write_snapshot(
                    fqn,
                    "dbt",
                    text,
                    header=f"{fqn}\n{entry['unique_id']} ({entry['path']})\n"
                    f"captured by grayson knowledge ingest at {captured_at} from dbt manifest"
                    f"{' ' + version if version else ''} — "
                    f"{'compiled' if node.get('compiled_code') else 'raw'} code; "
                    "the dbt repo is the authority",
                )
                entry["snapshot"] = snap["snapshot"]
                entry["snapshot_of"] = "compiled" if node.get("compiled_code") else "raw"
                snapshots += 1
        previous = next(
            (d for d in store.read(fqn)["definitions"] if d.get("path") == entry["path"]), None
        )
        if previous and previous.get("hash") and previous.get("hash") != entry.get("hash"):
            changed.append(fqn)
        store.upsert_definition(fqn, entry)
        if fill_descriptions:
            filled += _fill_column_descriptions(store, fqn, node.get("columns") or {})
        updated.append(fqn)
    return {
        "updated": updated,
        "changed_since_last": changed,
        "descriptions_filled": filled,
        "snapshots": snapshots,
        "not_in_manifest": not_in_manifest,
        "models_in_manifest": len(nodes),
    }


def _fill_column_descriptions(store: KnowledgeStore, fqn: str, dbt_columns: dict) -> int:
    """dbt schema.yml descriptions land only where the doc has none; a dbt
    column the doc does not list is added when it carries a description."""
    described = {
        str(name).upper(): (str(spec.get("description") or "").strip(), spec)
        for name, spec in dbt_columns.items()
        if isinstance(spec, dict) and str(spec.get("description") or "").strip()
    }
    if not described:
        return 0
    doc = store.read(fqn)
    columns = [dict(c) for c in doc["columns"]]
    filled = 0
    seen = set()
    for col in columns:
        key = str(col["name"]).upper()
        seen.add(key)
        if key in described and not col.get("description"):
            col["description"] = described[key][0]
            col["description_source"] = "dbt"
            filled += 1
    for key, (description, spec) in described.items():
        if key in seen:
            continue
        new = {"name": str(spec.get("name") or key), "description": description}
        if spec.get("data_type"):
            new["type"] = str(spec["data_type"])
        new["description_source"] = "dbt"
        columns.append(new)
        filled += 1
    if filled:
        store.set_profile(fqn, {"columns": columns})
    return filled
