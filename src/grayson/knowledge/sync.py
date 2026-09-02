"""`knowledge sync`: the warehouse's own account of a table's structure, merged
into its knowledge doc.

The doc's column list is human-curated and, left alone, silently falls behind
the warehouse. A sync reads DESCRIBE and writes only what the warehouse owns —
names, types, nullability, order — leaving descriptions and every other human
field where they are. It is a dated observation, recorded as such under
`structure`, never an assertion of meaning: nothing here can confirm a fact.

Through a session the DESCRIBE is an ordinary guarded, audited statement and
its query id is the observation's evidence. Without one it is a system query
on the workspace connection, like the metadata snapshot at session start.
Optionally the table's DDL (GET_DDL) is captured beside the doc as a dated
snapshot — the one case where a copy of derived state belongs in the library:
for a view it is the defining SELECT, and a collaborator served the library
without a warehouse has no other way to read it.
"""

from __future__ import annotations

from grayson.core.session import Session
from grayson.executor.snow import Executor, get_executor
from grayson.knowledge.store import KnowledgeStore, columns_from_describe
from grayson.profile.plan import ProfilePlanError, qualify
from grayson.util import utcnow
from grayson.workspace import Workspace


class SyncError(ValueError):
    pass


def _statement(
    sql: str,
    label: str,
    session: Session | None,
    executor: Executor | None,
    workspace: Workspace,
) -> tuple[list[dict], str | None]:
    """Run one statement; rows plus the query id when a session audited it."""
    if session is not None:
        from grayson.core.run import run_statement

        out = run_statement(session, sql, label=label, executor=executor)
        if out.get("status") != "executed":
            raise SyncError(
                f"{label} did not execute ({out.get('status')}): "
                f"{out.get('reason') or out.get('error') or 'unknown'}"
            )
        return session.cache.preview(out["qid"], limit=10000), out["qid"]
    executor = executor or get_executor(workspace.config.connection, workspace.root)
    result = executor.execute(sql, timeout_seconds=60)
    if not result.ok:
        raise SyncError(f"{label} failed ({result.status}): {result.error or 'unknown'}")
    return list(result.rows), None


def sync_table(
    workspace: Workspace,
    table: str,
    session: Session | None = None,
    executor: Executor | None = None,
    ddl: bool = False,
) -> dict:
    """Observe a table's columns (and optionally its DDL) and merge them into
    the knowledge doc. Returns what changed against the recorded descriptor."""
    store = KnowledgeStore(workspace.knowledge_dir)
    fqn = store.read(table)["table"]  # validates the name
    try:
        quoted = qualify(fqn)
    except ProfilePlanError as e:
        raise SyncError(str(e.args[0] if e.args else e)) from e
    rows, qid = _statement(
        f"DESCRIBE TABLE {quoted}", f"knowledge sync: describe {fqn}", session, executor, workspace
    )
    columns = columns_from_describe(rows)
    if not columns:
        raise SyncError(f"DESCRIBE returned no columns for {fqn} — does the table exist?")
    evidence = [qid] if qid else None
    source = f"describe (session {session.id})" if session is not None else "describe"
    result = store.sync_columns(fqn, columns, source=source, evidence=evidence)
    if ddl:
        result["ddl"] = capture_ddl(workspace, fqn, session=session, executor=executor)
    return result


def capture_ddl(
    workspace: Workspace,
    table: str,
    session: Session | None = None,
    executor: Executor | None = None,
) -> dict:
    """Snapshot GET_DDL of a table or view beside its doc, dated and hashed,
    and record it as a `ddl` definition entry."""
    store = KnowledgeStore(workspace.knowledge_dir)
    fqn = store.read(table)["table"]
    rows, qid = _statement(
        f"SELECT GET_DDL('TABLE', '{fqn}')",
        f"knowledge sync: ddl {fqn}",
        session,
        executor,
        workspace,
    )
    text = str(next(iter(rows[0].values()), "") if rows else "").strip()
    if not text:
        raise SyncError(f"GET_DDL returned nothing for {fqn}")
    via = f"session {session.id} {qid}" if session is not None and qid else "system query"
    previous = next((d for d in store.read(fqn)["definitions"] if d.get("kind") == "ddl"), None)
    snap = store.write_snapshot(
        fqn,
        "ddl",
        text,
        header=f"{fqn}\ncaptured by grayson knowledge sync at {utcnow()} via GET_DDL ({via})\n"
        "a dated copy of the warehouse's definition — the warehouse is the authority",
    )
    entry = {"kind": "ddl", "source": "GET_DDL", **snap}
    if qid:
        entry["evidence"] = [qid]
    store.upsert_definition(fqn, entry)
    return {
        **snap,
        "changed_since_last": bool(previous and previous.get("hash") != snap["hash"]),
        "first_capture": previous is None,
    }
