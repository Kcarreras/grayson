"""The MCP twins of the CLI evidence workflows. Review stays human-owned."""


def register(mcp, session, workspace, err):
    from grayson.core import criteria

    def call(fn, *args):
        try:
            return fn(*args)
        except (ValueError, OSError, KeyError) as e:
            return err(e)

    @mcp.tool(
        description="Draft success criteria for the person to review in the fix UI; "
        "populate them yourself rather than asking the person to fill an empty form. "
        "spec is {format:1, criteria:[{id?,name,source_qid,source_session?,"
        "expectation:{kind:scalar,column,operator,value}, "
        "relative_percent?:0.1}]}. no_rows expectations are supported. Approval binds "
        "the fix, SQL and resolved bounds. IDs are generated when omitted. Use criteria_queries "
        "to find baselines, current session first. Other sessions must use the same connection. "
        "Out-of-scope tables create a scope_request; wait for the person to approve it in the "
        "console before fix approval. This tool never expands scope, approves or applies a fix."
    )
    def criteria_set(session_id: str, pid: str, spec: dict) -> dict:
        return call(criteria.set_criteria, session(session_id), pid, spec)

    @mcp.tool(
        description="Find executed baseline queries for success criteria. Returns sessions "
        "and a paginated query list. Defaults to the current session; source_session='all' "
        "searches all compatible sessions, current first. Search matches SQL, label, ID or "
        "table. Select by source_session and source_qid in criteria_set. Reads history only."
    )
    def criteria_queries(
        session_id: str, source_session: str = "", search: str = "", offset: int = 0
    ) -> dict:
        s = session(session_id)
        result = call(criteria.query_choices, s, source_session, search, offset)
        return {**result, "sessions": criteria.query_sessions(s)}

    @mcp.tool(description="Read the fix's exact SQL, baseline, success criteria and approval.")
    def criteria_show(session_id: str, pid: str) -> dict:
        return call(criteria.contract, session(session_id), pid) or {}

    @mcp.tool(
        description="After the approved fix is marked applied, rerun every criterion "
        "through the guard. Grayson computes pass/fail/unproven; no verdict is supplied."
    )
    def criteria_run(session_id: str, pid: str) -> dict:
        return call(criteria.run_verification, session(session_id), pid)

    @mcp.tool(
        description="Propose a regression check using a passed success criterion's exact "
        "SQL and expectation. The person reviews activation in CLI/console."
    )
    def criteria_promote(session_id: str, pid: str, criterion_id: str, check_id: str) -> dict:
        return call(criteria.promote, session(session_id), pid, criterion_id, check_id)

    from grayson.knowledge import impact

    @mcp.tool(
        description="Connect detected definition/schema changes to explicit downstream "
        "dependencies, assumptions, history and approved checks. Optionally select changed "
        "tables. Reports source freshness and unknown coverage; does not execute SQL."
    )
    def impact_plan(
        session_id: str, changed_tables: list[str] | None = None, freshness_days: int = 7
    ) -> dict:
        return call(impact.build_plan, session(session_id), changed_tables, freshness_days)

    @mcp.tool(description="Read the saved, versioned investigation plan.")
    def impact_show(session_id: str) -> dict:
        return call(impact.show_plan, session(session_id))

    @mcp.tool(
        description="Launch the reviewed impact plan as a new strict-scope investigation. "
        "Pass its digest. The harness reasons over the plan using session_brief."
    )
    def impact_launch(session_id: str, digest: str) -> dict:
        return call(impact.launch, session(session_id), digest)

    @mcp.tool(
        description="Replay the exact approved checks selected in this session's impact "
        "plan. Refuses changed definitions and respects ordinary guard and budget limits."
    )
    def impact_run_checks(session_id: str) -> dict:
        return call(impact.run_plan_checks, session(session_id))

    from grayson.core import comparisons

    @mcp.tool(
        description="Declare a format-1 comparison: id,name,left/right:{session_id,table,"
        "label,filter?,window?}, keys:[{left,right}], columns:[{left,right,kind:exact|numeric,"
        "absolute_tolerance?,relative_percent?,aggregate?}], group_by:[left column names]. "
        "Optional allowed_missing_left/right, allowed_changed_rows and row_count_tolerance "
        "default to zero. Each environment uses its existing session and guard. Definitions "
        "are immutable; use a new id for changes. This only previews SQL; it runs no queries."
    )
    def comparison_create(session_id: str, spec: dict) -> dict:
        return call(comparisons.create, session(session_id), spec)

    @mcp.tool(description="List saved comparison contracts in this session.")
    def comparison_list(session_id: str) -> list[dict] | dict:
        return call(comparisons.inventory, session(session_id))

    @mcp.tool(description="Read the exact mappings, filters, tolerances, connections and SQL.")
    def comparison_show(session_id: str, comparison_id: str) -> dict:
        return call(comparisons.show, session(session_id), comparison_id)

    @mcp.tool(
        description="Run a declared comparison with fresh evidence in both environments. "
        "Six guarded queries: records, full-relation counts/aggregates and duplicate keys "
        "on each side. Reports missing/duplicate keys, changed values, concentrations, "
        "pass/fail/unproven and explicit incomplete coverage."
    )
    def comparison_run(session_id: str, comparison_id: str) -> dict:
        return call(comparisons.run, session(session_id), comparison_id)

    @mcp.tool(description="Read the latest comparison report, including evidence and timestamps.")
    def comparison_report(session_id: str, comparison_id: str) -> dict:
        return call(comparisons.latest_report, session(session_id), comparison_id)
