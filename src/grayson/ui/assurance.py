"""Console routes sharing the deterministic evidence engines."""

import json
from contextlib import suppress

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool


def register(app, workspace, templates, check, session, redirect):
    from grayson.core import criteria

    def criteria_context(s, pid, items=None, error=None):
        spec = criteria.contract(s, pid)
        draft = items or (spec["criteria"] if spec else [{"id": "criterion_1"}])
        queries = criteria.query_choices(s)["queries"]
        for item in draft:
            sid = item.get("source_session") or s.id
            qid = item.get("source_qid")
            if not qid or any(q["session_id"] == sid and q["qid"] == qid for q in queries):
                continue
            with suppress(ValueError, OSError, HTTPException):
                source = session(sid)
                q = source.query_row(qid)
                if q and q["status"] == "executed":
                    tables = json.loads(q.get("tables_json") or "[]")
                    queries.append(
                        {
                            "qid": qid,
                            "session_id": sid,
                            "session_title": source.summary()["title"] or sid,
                            "current": sid == s.id,
                            "label": q["label"] or q["sql_raw"][:100],
                            "sql": q["sql_raw"],
                            "tables": tables,
                            "ts": q["ts"],
                            "scope_required": sorted({t.upper() for t in tables} - s.scope_tables),
                        }
                    )
        return {
            "nav": "sessions",
            "s": s.summary(),
            "p": s.proposal(pid),
            "contract": spec,
            "draft": draft,
            "queries": queries,
            "query_sessions": criteria.query_sessions(s),
            "error": error,
        }

    @app.get("/session/{sid}/criteria-queries")
    def criteria_query_picker(
        request: Request, sid: str, source_session: str = "", search: str = "", offset: int = 0
    ):
        check(request)
        try:
            return criteria.query_choices(session(sid), source_session, search, offset)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/session/{sid}/criteria/{pid}")
    def criteria_page(request: Request, sid: str, pid: str):
        check(request)
        s = session(sid)
        try:
            context = criteria_context(s, pid)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return templates.TemplateResponse(
            request,
            "criteria.html",
            context,
        )

    @app.post("/session/{sid}/criteria/{pid}/{action}")
    async def criteria_action(request: Request, sid: str, pid: str, action: str):
        check(request)
        s = session(sid)
        form = await request.form()
        items = []
        try:
            if action == "set":
                for idx in range(len(form.getlist("criterion_id"))):

                    def value(key, index=idx):
                        return form.getlist(key)[index]

                    rule = {"kind": value("kind")}
                    if rule["kind"] == "scalar":
                        rule.update(
                            column=value("column"), operator=value("operator"), value=value("value")
                        )
                        if value("relative_percent"):
                            rule.update(operator="eq", value="0")
                        elif rule["operator"] == "between":
                            rule["upper"] = value("upper")
                    item = {
                        "id": value("criterion_id"),
                        "name": value("name"),
                        "source_qid": value("source_qid"),
                        "expectation": rule,
                    }
                    if "::" in item["source_qid"]:
                        item["source_session"], item["source_qid"] = item["source_qid"].split(
                            "::", 1
                        )
                    if rule["kind"] == "scalar" and value("relative_percent"):
                        item["relative_percent"] = value("relative_percent")
                    items.append(item)
                await run_in_threadpool(
                    criteria.set_criteria, s, pid, {"format": 1, "criteria": items}
                )
            elif action == "request-scope":
                if s.stage == "closed" or s.proposal(pid)["status"] != "proposed":
                    raise ValueError("scope requests require a pending fix in an open session")
                await run_in_threadpool(criteria.request_scope, s, pid)
            elif action == "run":
                await run_in_threadpool(criteria.run_verification, s, pid)
            elif action == "promote":
                out = await run_in_threadpool(
                    criteria.promote, s, pid, form["criterion_id"], form["check_id"]
                )
                return redirect(f"/checks/regression/{out['check']['id']}")
            elif action == "applied":
                from grayson.core.proposals import mark_applied

                await run_in_threadpool(mark_applied, s, pid, actor="user")
            else:
                raise ValueError("unknown criteria action")
        except (ValueError, OSError, KeyError, IndexError) as e:
            return templates.TemplateResponse(
                request,
                "criteria.html",
                criteria_context(s, pid, items, str(e)),
                status_code=400,
            )
        return redirect(f"/session/{sid}/criteria/{pid}")

    from grayson.knowledge import impact

    @app.get("/session/{sid}/impact")
    def impact_page(request: Request, sid: str):
        check(request)
        s = session(sid)
        try:
            plan = impact.show_plan(s)
        except ValueError:
            plan = None
        return templates.TemplateResponse(
            request,
            "impact.html",
            {
                "nav": "sessions",
                "s": s.summary(),
                "plan": plan,
            },
        )

    @app.post("/session/{sid}/impact/{action}")
    async def impact_action(request: Request, sid: str, action: str):
        check(request)
        s = session(sid)
        form = await request.form()
        try:
            if action == "plan":
                from grayson.util import parse_table_list

                selected = parse_table_list(str(form.get("tables", ""))) or None
                await run_in_threadpool(
                    impact.build_plan, s, selected, int(form.get("freshness_days", 7))
                )
            elif action == "launch":
                result = await run_in_threadpool(impact.launch, s, str(form.get("digest", "")))
                return redirect(f"/session/{result['session_id']}")
            elif action == "run-checks":
                await run_in_threadpool(impact.run_plan_checks, s)
                return redirect(f"/session/{sid}#regressions")
            else:
                raise ValueError("unknown impact action")
        except (ValueError, KeyError, OSError) as e:
            try:
                plan = impact.show_plan(s)
            except ValueError:
                plan = None
            return templates.TemplateResponse(
                request,
                "impact.html",
                {
                    "nav": "sessions",
                    "s": s.summary(),
                    "plan": plan,
                    "error": str(e),
                },
                status_code=400,
            )
        return redirect(f"/session/{sid}/impact")

    from grayson.core import comparisons

    def comparison_context(s, comparison_id=None, error=None, form=None):
        definition = comparisons.show(s, comparison_id) if comparison_id else None
        report = None
        if definition:
            with suppress(ValueError):
                report = comparisons.latest_report(s, comparison_id)
        sessions = []
        for sid in workspace.list_session_ids():
            try:
                candidate = session(sid)
                if candidate.stage != "closed":
                    sessions.append(candidate.summary())
            except (OSError, ValueError, HTTPException):
                continue
        return {
            "nav": "sessions",
            "s": s.summary(),
            "comparisons": comparisons.inventory(s),
            "definition": definition,
            "report": report,
            "sessions": sessions,
            "error": error,
            "form": form or {},
        }

    @app.get("/session/{sid}/comparisons")
    def comparisons_page(request: Request, sid: str):
        check(request)
        return templates.TemplateResponse(
            request, "comparisons.html", comparison_context(session(sid))
        )

    @app.get("/session/{sid}/comparisons/{comparison_id}")
    def comparison_page(request: Request, sid: str, comparison_id: str):
        check(request)
        try:
            context = comparison_context(session(sid), comparison_id)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from e
        return templates.TemplateResponse(request, "comparison.html", context)

    @app.get("/session/{sid}/comparisons/{comparison_id}/report.json")
    def comparison_download(request: Request, sid: str, comparison_id: str):
        check(request)
        from fastapi.responses import JSONResponse

        try:
            data = comparisons.latest_report(session(sid), comparison_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return JSONResponse(
            data,
            headers={"Content-Disposition": f'attachment; filename="comparison-{data["id"]}.json"'},
        )

    @app.post("/session/{sid}/comparisons/create")
    async def comparison_create(request: Request, sid: str):
        check(request)
        s = session(sid)
        form = await request.form()
        try:
            data = {"format": 1, "id": form["id"], "name": form["name"], "keys": [], "columns": []}
            for side in ("left", "right"):
                data[side] = {
                    k: str(form.get(f"{side}_{k}", ""))
                    for k in ("session_id", "table", "label", "filter", "window")
                }
            for i, left in enumerate(form.getlist("column_left")):
                kind = form.getlist("column_kind")[i]
                mapping = {"left": left, "right": form.getlist("column_right")[i]}
                if kind == "key":
                    data["keys"].append(mapping)
                else:
                    mapping.update(
                        kind="numeric" if kind in {"numeric", "sum"} else "exact",
                        aggregate=kind == "sum",
                        absolute_tolerance=form.getlist("absolute_tolerance")[i] or "0",
                        relative_percent=form.getlist("relative_percent")[i] or "0",
                    )
                    data["columns"].append(mapping)
            data["group_by"] = [
                c.strip() for c in str(form.get("group_by", "")).split(",") if c.strip()
            ]
            for key in (
                "allowed_missing_left",
                "allowed_missing_right",
                "allowed_changed_rows",
                "row_count_tolerance",
                "max_rows",
                "example_limit",
            ):
                data[key] = int(
                    form.get(key)
                    or (100000 if key == "max_rows" else 20 if key == "example_limit" else 0)
                )
            definition = await run_in_threadpool(comparisons.create, s, data)
        except (ValueError, KeyError, OSError, IndexError) as e:
            return templates.TemplateResponse(
                request,
                "comparisons.html",
                comparison_context(s, error=str(e), form=form),
                status_code=400,
            )
        return redirect(f"/session/{sid}/comparisons/{definition['spec']['id']}")

    @app.post("/session/{sid}/comparisons/{comparison_id}/run")
    async def comparison_run(request: Request, sid: str, comparison_id: str):
        check(request)
        s = session(sid)
        try:
            await run_in_threadpool(comparisons.run, s, comparison_id)
        except (ValueError, KeyError, OSError) as e:
            return templates.TemplateResponse(
                request,
                "comparison.html",
                comparison_context(s, comparison_id, error=str(e)),
                status_code=400,
            )
        return redirect(f"/session/{sid}/comparisons/{comparison_id}")
