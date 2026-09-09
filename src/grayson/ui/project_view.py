"""Presentation data for the project variant of the session workspace."""

import difflib

import sqlglot

from grayson.core import engine as checkpoints
from grayson.projects import engine
from grayson.projects.models import Candidate, Contract
from grayson.projects.sql import compile_candidate, sources
from grayson.ui.diffs import diff_rows, review_proposal


def pipeline_graph(p):
    if not p or not p.get("candidate"):
        return {"nodes": [], "edges": [], "width": 720, "height": 180}
    nodes, by_name, links = [], {}, []
    joins = {j["id"]: j for j in p["contract"]["joins"]}
    table_order = []
    for node in p["candidate"]["nodes"]:
        if node["kind"] == "query":
            for ref in sorted(sources(sqlglot.parse_one(node["sql"], read="snowflake"))):
                if ref in p["contract"]["scope"] and ref not in table_order:
                    table_order.append(ref)
    for table in table_order:
        by_name[table] = len(nodes)
        nodes.append(
            {
                "id": "source-" + str(len(nodes)),
                "label": table,
                "kind": "source",
                "rank": 0,
                "parents": [],
            }
        )
    for node in p["candidate"]["nodes"]:
        if node["kind"] == "query":
            refs = sorted(sources(sqlglot.parse_one(node["sql"], read="snowflake")))
        else:
            j = joins[node["id"]]
            refs = [j["left"].upper(), j["right"].upper()]
        parents = [by_name[r] for r in refs]
        index = len(nodes)
        nodes.append(
            {
                **node,
                "label": node["id"],
                "parents": parents,
                "rank": 1 + max(nodes[i]["rank"] for i in parents),
                "output": node["id"] == p["candidate"]["output"],
            }
        )
        by_name[node["id"].upper()] = index
        links.extend((parent, index) for parent in parents)
    used = {parent for parent, _ in links} | {child for _, child in links}
    rows = {}
    for i, node in enumerate(nodes):
        if i not in used:
            continue
        rank = node["rank"]
        row = rows.get(rank, 0)
        rows[rank] = row + 1
        node.update(x=20 + rank * 240, y=20 + row * 100)
    edges = [
        {
            "x1": nodes[a]["x"] + 200,
            "y1": nodes[a]["y"] + 34,
            "x2": nodes[b]["x"],
            "y2": nodes[b]["y"] + 34,
        }
        for a, b in links
    ]
    return {
        "nodes": [n for i, n in enumerate(nodes) if i in used],
        "edges": edges,
        "width": max(720, (max(rows) + 1) * 240),
        "height": max(130, max(rows.values()) * 100 + 20),
    }


def build_context(session, error=None, section="build"):
    view = engine.status(session)
    p = view["project"]
    proposal = session.proposal(p["deployment"]["pid"]) if p and p.get("deployment") else None
    history = []
    if p:
        versions = [*p["history"], {"candidate": p["candidate"], "verification": p["verification"]}]
        for i, old in enumerate(p["history"]):
            new = versions[i + 1]
            old_failed = {
                r["id"]
                for r in (old.get("verification") or {}).get("results", [])
                if r["status"] != "pass"
            }
            new_passed = {
                r["id"]
                for r in (new.get("verification") or {}).get("results", [])
                if r["status"] == "pass"
            }
            diff = ""
            if section == "history":
                old_sql = compile_candidate(
                    Candidate.model_validate(old["candidate"]),
                    Contract.model_validate(p["contract"]),
                )["sql"]
                new_sql = compile_candidate(
                    Candidate.model_validate(new["candidate"]),
                    Contract.model_validate(p["contract"]),
                )["sql"]
                diff = "\n".join(
                    difflib.unified_diff(
                        old_sql.splitlines(),
                        new_sql.splitlines(),
                        fromfile=f"Attempt {i + 1}",
                        tofile=f"Attempt {i + 2}",
                        lineterm="",
                    )
                )
            history.append(
                {
                    **old,
                    "number": i + 1,
                    "resolved": sorted(old_failed & new_passed),
                    "resolved_names": [
                        r["name"]
                        for r in (new.get("verification") or {}).get("results", [])
                        if r["id"] in old_failed & new_passed
                    ],
                    "diagnosis": new["candidate"]["diagnosis"],
                    "diff": diff,
                    "diff_rows": diff_rows(diff),
                }
            )
    import yaml

    queries = session.query_log(100)
    return {
        "nav": "sessions",
        "project_workflow": True,
        "s": session.summary(),
        "view": view,
        "p": p,
        "error": error,
        "deployment_proposal": review_proposal(session, proposal) if proposal else None,
        "draft_yaml": yaml.safe_dump(p["contract"], sort_keys=False) if p else "",
        "interventions": session.interventions(),
        "graph": pipeline_graph(p) if section == "build" else {},
        "attempts": history,
        "queries": queries,
        "qsql": {q["qid"]: q.get("sql_raw") or "" for q in queries},
        "checkpoints": checkpoints.checkpoints_view(session, session.workspace.workflows_dir),
        "readiness": checkpoints.readiness(session, session.workspace.workflows_dir),
    }
