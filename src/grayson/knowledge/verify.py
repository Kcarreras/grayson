"""`knowledge verify`: re-run what a fact rests on, against the warehouse.

The one anchor that costs a query. A verified fix lands as a fact anchored to
its published record, and the record carries the fix's after-query — the
statement whose result showed the fix worked. Re-running it through a
session (guarded, audited, budgeted, its query id the observation's evidence)
and comparing the row count with what the record holds says whether the fix
still holds. Match: the fact is re-baselined (`verified_at`). Mismatch: it
becomes unverified with the two counts in its reason, until someone looks.

Deterministic on both sides — the verdict is code comparing counts, never a
judgment — so it is not policy-gated: an agent may run it whenever a session
is open. Opt-in per session because it spends budget.
"""

from __future__ import annotations

from grayson.core.session import Session
from grayson.executor.snow import Executor
from grayson.knowledge.store import KnowledgeStore
from grayson.util import utcnow
from grayson.workspace import Workspace


class VerifyError(ValueError):
    pass


def _record_after_query(workspace: Workspace, session_id: str, record_id: str) -> dict | None:
    """The after-query (sql + recorded row count) of a proposal record, from
    the library copy or, failing that, the local session."""
    from grayson.records import get_record

    try:
        rec = get_record(workspace, session_id, "proposal", record_id)
    except ValueError:
        return None
    if rec is None:
        return None
    verification = (rec.get("record") or {}).get("verification") or {}
    after = verification.get("after_qid")
    if not after:
        return None
    for q in rec.get("evidence_queries") or []:
        if q.get("qid") == after and q.get("sql"):
            return q
    return None


def verify_table(
    workspace: Workspace, table: str, session: Session, executor: Executor | None = None
) -> dict:
    """Re-verify every fact on `table` that has a query to re-run."""
    from grayson.core.run import run_statement

    store = KnowledgeStore(workspace.knowledge_dir)
    doc = store.read(table)
    results: list[dict] = []
    changed = False
    for f in doc["facts"]:
        if f.get("standing") == "retired" or f.get("superseded_by"):
            continue
        anchor = next(
            (
                a
                for a in f.get("anchors") or []
                if a.get("kind") == "record" and a.get("record_kind") == "proposal"
            ),
            None,
        )
        if anchor is None:
            continue  # nothing re-runnable: semantics, not an observation
        query = _record_after_query(workspace, str(anchor.get("session")), str(anchor.get("id")))
        if query is None:
            results.append(
                {
                    "fact_id": f["id"],
                    "result": "skipped",
                    "reason": f"record {anchor.get('session')}/{anchor.get('id')} has no "
                    "after-query on file",
                }
            )
            continue
        out = run_statement(
            session, str(query["sql"]), label=f"knowledge verify: {f['id']}", executor=executor
        )
        if out.get("status") != "executed":
            results.append(
                {
                    "fact_id": f["id"],
                    "result": "not_executed",
                    "reason": out.get("reason") or out.get("error") or out.get("status"),
                    "qid": out.get("qid"),
                }
            )
            continue
        row = session.query_row(out["qid"]) or {}
        now_count = row.get("row_count")
        recorded = query.get("row_count")
        if recorded is None or now_count is None:
            results.append(
                {
                    "fact_id": f["id"],
                    "result": "skipped",
                    "reason": "no row count to compare",
                    "qid": out["qid"],
                }
            )
            continue
        stamp = utcnow()
        if int(recorded) == int(now_count):
            f["verified_at"] = stamp
            if f.get("standing") == "unverified" and f.get("standing_by") == "verify":
                for key in ("standing", "standing_reason", "standing_at", "standing_by"):
                    f[key] = None
            results.append(
                {"fact_id": f["id"], "result": "holds", "rows": int(now_count), "qid": out["qid"]}
            )
        else:
            f["standing"] = "unverified"
            f["standing_reason"] = (
                f"re-run returned {int(now_count)} row(s) where the record has "
                f"{int(recorded)} (session {session.id} {out['qid']})"
            )
            f["standing_at"] = stamp
            f["standing_by"] = "verify"
            results.append(
                {
                    "fact_id": f["id"],
                    "result": "differs",
                    "rows": int(now_count),
                    "recorded": int(recorded),
                    "qid": out["qid"],
                }
            )
        changed = True
    if changed:
        store.save(doc["table"], doc)
    return {
        "table": doc["table"],
        "results": results,
        "holds": sum(1 for r in results if r["result"] == "holds"),
        "differs": sum(1 for r in results if r["result"] == "differs"),
        "skipped": sum(1 for r in results if r["result"] in ("skipped", "not_executed")),
    }
