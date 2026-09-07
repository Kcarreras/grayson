"""Optional, versioned success contracts evaluated by the regression rule engine."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from decimal import Decimal, DecimalException, localcontext
from typing import Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlglot import exp

from grayson.checks.regression import Expectation, evaluate, propose_check
from grayson.core.run import run_statement
from grayson.core.session import Session
from grayson.util import utcnow


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=160)
    source_qid: str
    source_session: str | None = None
    expectation: Expectation
    # Percent, not fraction: 0.1 means within 0.1% of the source observation.
    relative_percent: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class Criteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal[1] = 1
    criteria: list[Criterion] = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def generate_ids(cls, value):
        if not isinstance(value, dict) or not isinstance(value.get("criteria"), list):
            return value
        items = [dict(c) if isinstance(c, dict) else c for c in value["criteria"]]
        used = {c["id"] for c in items if isinstance(c, dict) and isinstance(c.get("id"), str)}
        for item in items:
            if not isinstance(item, dict) or item.get("id") not in (None, ""):
                continue
            base = re.sub(r"[^a-z0-9]+", "_", str(item.get("name", "")).lower()).strip("_")
            if not base or not base[0].isalpha():
                base = "criterion_" + base
            base = base[:56]
            candidate, n = base, 2
            while candidate in used:
                candidate = f"{base}_{n}"
                n += 1
            item["id"] = candidate
            used.add(candidate)
        return {**value, "criteria": items}

    @model_validator(mode="after")
    def unique_ids(self):
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("criterion ids must be unique")
        return self


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def query_sessions(session: Session) -> list[dict]:
    """Current session first, then recent sessions; never execute warehouse SQL."""
    sources = []
    for sid in [session.id, *reversed(session.workspace.list_session_ids())]:
        if any(s["id"] == sid for s in sources):
            continue
        try:
            source = session if sid == session.id else Session(session.workspace, sid)
            summary = source.summary()
            sources.append(
                {
                    "id": sid,
                    "title": summary["title"] or sid,
                    "connection": source.connection,
                    "queries": summary["queries_executed"],
                    "current": sid == session.id,
                    "compatible": source.connection == session.connection,
                }
            )
        except (ValueError, OSError, sqlite3.Error):
            continue
    return sources


def query_choices(
    session: Session,
    source_session: str = "",
    search: str = "",
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """Search executed baselines, grouped by session, with bounded pages."""
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("query page requires offset >= 0 and limit between 1 and 100")
    sources = query_sessions(session)
    selected = source_session or session.id
    if selected != "all" and selected not in {s["id"] for s in sources}:
        raise ValueError("unknown source session")
    choices, skipped = [], offset
    for info in sources:
        if not info["compatible"] or selected not in {"all", info["id"]}:
            continue
        source = session if info["current"] else Session(session.workspace, info["id"])
        con = source._con()
        where = (
            "status='executed' AND instr(lower(qid || ' ' || coalesce(label, '') || ' ' || "
            "sql_raw || ' ' || coalesce(tables_json, '')), ?) > 0"
        )
        try:
            count = con.execute(
                f"SELECT COUNT(*) FROM queries WHERE {where}", (search.lower(),)
            ).fetchone()[0]
            if skipped >= count:
                skipped -= count
                continue
            con.row_factory = sqlite3.Row
            rows = con.execute(
                f"SELECT * FROM queries WHERE {where} ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (search.lower(), limit + 1 - len(choices), skipped),
            ).fetchall()
        finally:
            con.close()
        skipped = 0
        for raw in rows:
            q = dict(raw)
            tables = json.loads(q.get("tables_json") or "[]")
            choices.append(
                {
                    "qid": q["qid"],
                    "session_id": source.id,
                    "session_title": info["title"],
                    "current": info["current"],
                    "label": q["label"] or q["sql_raw"][:100],
                    "sql": q["sql_raw"],
                    "tables": tables,
                    "ts": q["ts"],
                    "scope_required": sorted({t.upper() for t in tables} - session.scope_tables),
                }
            )
        if len(choices) > limit:
            break
    return {
        "queries": choices[:limit],
        "offset": offset,
        "next_offset": offset + limit if len(choices) > limit else None,
    }


def decimal_precision(*values: Decimal) -> int:
    """Retain tiny differences beside large values, without unbounded allocations."""
    precision = max(
        50,
        max(v.adjusted() for v in values)
        - min(v.as_tuple().exponent for v in values)
        + sum(len(v.as_tuple().digits) for v in values)
        + 20,
    )
    if precision > 10000:
        raise ValueError("numeric precision exceeds the supported 10,000-digit calculation limit")
    return precision


def prepare(session: Session, spec: dict, *, for_fix: bool = False) -> dict:
    """Resolve baseline-relative bounds once, before the person approves them."""
    parsed = Criteria.model_validate(spec)
    criteria = []
    for item in parsed.criteria:
        source = session
        if item.source_session and item.source_session != session.id:
            if not for_fix:
                raise ValueError("finding claims require evidence from the current session")
            source = Session(session.workspace, item.source_session)
            if source.connection != session.connection:
                raise ValueError("baseline queries must use the same connection as the fix session")
        query = source.query_row(item.source_qid)
        if not query or query["status"] != "executed":
            raise ValueError("criteria must cite successfully executed source queries")
        tables = json.loads(query.get("tables_json") or "[]")
        if not tables or (
            not for_fix
            and not {t.upper() for t in tables}.intersection(t.upper() for t in session.targets)
        ):
            raise ValueError("each criterion must read a table under investigation")
        if not for_fix and not {t.upper() for t in tables}.issubset(session.scope_tables):
            raise ValueError("criterion source tables must be in the approved session scope")
        tree = sqlglot.parse_one(query["sql_raw"], read="snowflake")
        if not isinstance(tree, exp.Select | exp.Union | exp.Intersect | exp.Except):
            raise ValueError("criteria require SELECT evidence")
        if any(tree.find_all(exp.Limit, exp.Offset, exp.TableSample)):
            raise ValueError("success criteria cannot certify explicitly limited or sampled SQL")
        rule = item.expectation
        observation = evaluate(source, item.source_qid, rule)
        if item.relative_percent is not None:
            if rule.kind != "scalar":
                raise ValueError("relative_percent requires a scalar expectation")
            baseline = Decimal(observation["observed"])
            try:
                with localcontext() as context:
                    context.prec = decimal_precision(baseline, item.relative_percent, Decimal(100))
                    delta = abs(baseline) * item.relative_percent / 100
                    rule = Expectation(
                        kind="scalar",
                        column=rule.column,
                        operator="between",
                        value=baseline - delta,
                        upper=baseline + delta,
                    )
            except DecimalException as e:
                raise ValueError("relative bounds exceed supported numeric precision") from e
            observation = evaluate(source, item.source_qid, rule)
        criteria.append(
            {
                **item.model_dump(mode="json"),
                "source_session": source.id,
                "expectation": rule.model_dump(mode="json"),
                "expectation_text": rule.label(),
                "sql": query["sql_raw"],
                "tables": tables,
                "baseline": {**observation, "observed_at": query["ts"]},
            }
        )
    return {"format": 1, "criteria": criteria, "connection": session.connection}


def missing_scope(session: Session, spec: dict) -> list[str]:
    return sorted({t.upper() for c in spec["criteria"] for t in c["tables"]} - session.scope_tables)


def request_scope(session: Session, pid: str) -> dict | None:
    """Request human approval without expanding scope or approving the fix."""
    spec = contract(session, pid)
    if not spec:
        return None
    context = f"Success criteria for fix {pid}."
    for item in session.interventions("open"):
        if item["kind"] == "scope_request" and item["request"].get("context") == context:
            if item["request"].get("tables") == spec["scope_required"]:
                return item
            session.cancel_intervention(item["iid"], actor="system")
    if not spec["scope_required"]:
        return None
    iid = session.add_intervention(
        "scope_request",
        f"Expand scope for {pid} success criteria",
        "Review the additional tables needed by this fix's success criteria. "
        "Granting scope does not approve or apply the fix.",
        {
            "tables": spec["scope_required"],
            "reason": "Check these proposed outcomes: "
            + "; ".join(
                c["name"]
                for c in spec["criteria"]
                if set(t.upper() for t in c["tables"]).intersection(spec["scope_required"])
            ),
            "context": context,
            "criteria_pid": pid,
        },
    )
    return session.intervention(iid)


def contract(session: Session, pid: str) -> dict | None:
    proposal = session.proposal(pid)
    if proposal is None:
        raise ValueError(f"no proposal '{pid}'")
    spec = proposal["payload"].get("success_criteria")
    if spec is None:
        return None
    if spec.get("format") != 1:
        raise ValueError("unsupported success criteria format; upgrade before modifying this fix")
    bound = digest({"kind": proposal["kind"], "payload": proposal["payload"]})
    approval = json.loads(session.get_meta(f"criteria_approval:{pid}") or "null")
    required = missing_scope(session, spec)
    scope_requests = [
        i
        for i in session.interventions()
        if i["kind"] == "scope_request"
        and i["request"].get("context") == f"Success criteria for fix {pid}."
    ]
    return {
        **spec,
        "digest": bound,
        "approval": approval,
        "scope_required": required,
        "scope_request": scope_requests[-1] if scope_requests else None,
        "review_current": bool(
            approval
            and approval["digest"] == bound
            and not required
            and spec["connection"] == session.connection
        ),
    }


def set_criteria(session: Session, pid: str, spec: dict) -> dict:
    if session.stage == "closed":
        raise ValueError("success criteria require an open session")
    prepared = prepare(session, spec, for_fix=True)
    con = session._con()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT payload, status FROM proposals WHERE pid=?", (pid,)).fetchone()
        if not row or row[1] != "proposed":
            raise ValueError("set criteria on a pending proposal, before approval or application")
        payload = json.loads(row[0])
        if payload.get("success_criteria", {}).get("format", 1) != 1:
            raise ValueError("unsupported success criteria format")
        payload["success_criteria"] = prepared
        con.execute("UPDATE proposals SET payload=? WHERE pid=?", (json.dumps(payload), pid))
        con.commit()
    finally:
        con.close()
    session.log_event("agent", "criteria_proposed", {"pid": pid, **prepared})
    request_scope(session, pid)
    return contract(session, pid)


def approve(session: Session, pid: str, reviewed_digest: str, actor: str) -> None:
    if actor != "user":
        raise ValueError("approving success criteria is a user action")
    con = session._con()
    try:
        con.execute("BEGIN IMMEDIATE")
        current = contract(session, pid)
        if not current or current["digest"] != reviewed_digest:
            raise ValueError("the fix or criteria changed since review; reload and review again")
        if current["scope_required"]:
            raise ValueError("approve the scope expansion before approving this fix's criteria")
        if current["connection"] != session.connection:
            raise ValueError("the session connection changed; prepare the criteria again")
        stamp = utcnow()
        changed = con.execute(
            "UPDATE proposals SET status='approved', decided_by=?, decided_at=? "
            "WHERE pid=? AND status='proposed'",
            (actor, stamp, pid),
        )
        if not changed.rowcount:
            raise ValueError("proposal is no longer pending")
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (
                f"criteria_approval:{pid}",
                json.dumps({"digest": reviewed_digest, "approved_at": stamp, "actor": actor}),
            ),
        )
        con.commit()
    finally:
        con.close()
    session.log_event(actor, "proposal_approved", {"pid": pid, "criteria_digest": reviewed_digest})


def run_verification(session: Session, pid: str, *, executor=None) -> dict:
    spec = contract(session, pid)
    p = session.proposal(pid)
    if not spec or not spec["review_current"]:
        raise ValueError("verification requires currently approved success criteria")
    if (
        session.stage == "closed"
        or p["status"]
        not in {
            "applied",
            "verified",
            "verification_failed",
        }
        or not session.get_meta(f"criteria_applied:{pid}")
    ):
        raise ValueError("mark the approved fix applied before running fresh verification")
    if spec["connection"] != session.connection:
        raise ValueError("the session connection changed since criteria approval")
    results = []
    for criterion in spec["criteria"]:
        output = run_statement(
            session, criterion["sql"], label=f"Verify {pid}: {criterion['name']}", executor=executor
        )
        qid = output["qid"]
        try:
            if output["status"] != "executed" or output.get("cache_error"):
                raise ValueError(
                    output.get("reason")
                    or output.get("error")
                    or output.get("cache_error")
                    or output["status"]
                )
            outcome = evaluate(session, qid, Expectation.model_validate(criterion["expectation"]))
        except (ValueError, OSError) as e:
            outcome = {"status": "unproven", "observed": None, "details": str(e)}
        results.append(
            {
                **criterion,
                **outcome,
                "qid": qid,
                "observed_at": utcnow(),
                "connection": session.connection,
            }
        )
    counts = {s: sum(r["status"] == s for r in results) for s in ("pass", "fail", "unproven")}
    verdict = "fail" if counts["fail"] else "unproven" if counts["unproven"] else "pass"
    refs = list(
        dict.fromkeys(
            ref
            for r in results
            for ref in (
                (r.get("source_session") or session.id, r["source_qid"]),
                (session.id, r["qid"]),
            )
        )
    )
    verification = {
        "format": 1,
        "mode": "criteria",
        "verdict": verdict,
        "criteria_digest": spec["digest"],
        "results": results,
        "counts": counts,
        "verified_at": utcnow(),
        "before_qid": results[0]["source_qid"],
        "before_session": results[0].get("source_session") or session.id,
        "after_qid": results[0]["qid"],
        "evidence": [qid for sid, qid in refs if sid == session.id],
        "evidence_refs": [{"session_id": sid, "qid": qid} for sid, qid in refs],
    }
    session.attach_verification(pid, verification, actor="system")
    session.log_event("system", "criteria_evaluated", {"pid": pid, **verification})
    from grayson.records import publish_proposal

    publish_proposal(session, pid)
    if verdict == "pass":
        from grayson.core.proposals import _record_verified_fix

        _record_verified_fix(
            session,
            p,
            verification["before_qid"],
            verification["after_qid"],
            "system",
            evidence=verification["evidence"],
            evidence_refs=verification["evidence_refs"],
        )
    return verification


def promote(session: Session, pid: str, criterion_id: str, check_id: str) -> dict:
    spec = contract(session, pid)
    verification = (session.proposal(pid) or {}).get("verification") or {}
    if (
        not spec
        or not spec["review_current"]
        or verification.get("mode") != "criteria"
        or verification.get("criteria_digest") != spec["digest"]
    ):
        raise ValueError("promote a criterion from a current machine verification")
    result = next((r for r in verification["results"] if r["id"] == criterion_id), None)
    if not result or result["status"] != "pass":
        raise ValueError("only a passed criterion can become a regression check")
    return propose_check(
        session,
        result["qid"],
        check_id,
        result["name"],
        f"Success criterion {criterion_id} from fix {session.id}/{pid}",
        result["expectation"],
    )
