"""Optional, versioned success contracts evaluated by the regression rule engine."""

from __future__ import annotations

import hashlib
import json
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
    expectation: Expectation
    # Percent, not fraction: 0.1 means within 0.1% of the source observation.
    relative_percent: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)


class Criteria(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal[1] = 1
    criteria: list[Criterion] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("criterion ids must be unique")
        return self


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


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


def prepare(session: Session, spec: dict) -> dict:
    """Resolve baseline-relative bounds once, before the person approves them."""
    parsed = Criteria.model_validate(spec)
    criteria = []
    for item in parsed.criteria:
        query = session.query_row(item.source_qid)
        if not query or query["status"] != "executed":
            raise ValueError("criteria must cite successfully executed source queries")
        tables = json.loads(query.get("tables_json") or "[]")
        if not tables or not {t.upper() for t in tables}.intersection(
            t.upper() for t in session.targets
        ):
            raise ValueError("each criterion must read a table under investigation")
        if not {t.upper() for t in tables}.issubset(session.scope_tables):
            raise ValueError("criterion source tables must be in the approved session scope")
        tree = sqlglot.parse_one(query["sql_raw"], read="snowflake")
        if not isinstance(tree, exp.Select | exp.Union | exp.Intersect | exp.Except):
            raise ValueError("criteria require SELECT evidence")
        if any(tree.find_all(exp.Limit, exp.Offset, exp.TableSample)):
            raise ValueError("success criteria cannot certify explicitly limited or sampled SQL")
        rule = item.expectation
        observation = evaluate(session, item.source_qid, rule)
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
            observation = evaluate(session, item.source_qid, rule)
        criteria.append(
            {
                **item.model_dump(mode="json"),
                "expectation": rule.model_dump(mode="json"),
                "expectation_text": rule.label(),
                "sql": query["sql_raw"],
                "tables": tables,
                "baseline": {**observation, "observed_at": query["ts"]},
            }
        )
    return {"format": 1, "criteria": criteria, "connection": session.connection}


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
    return {
        **spec,
        "digest": bound,
        "approval": approval,
        "review_current": bool(approval and approval["digest"] == bound),
    }


def set_criteria(session: Session, pid: str, spec: dict) -> dict:
    if session.stage == "closed":
        raise ValueError("success criteria require an open session")
    prepared = prepare(session, spec)
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
    verification = {
        "format": 1,
        "mode": "criteria",
        "verdict": verdict,
        "criteria_digest": spec["digest"],
        "results": results,
        "counts": counts,
        "verified_at": utcnow(),
        "before_qid": results[0]["source_qid"],
        "after_qid": results[0]["qid"],
        "evidence": list(dict.fromkeys(q for r in results for q in (r["source_qid"], r["qid"]))),
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
