"""Agent project tools. No human approval endpoints are exposed."""


def register(mcp, session, workspace, err):
    from grayson.projects import engine
    from grayson.projects.models import Candidate, Contract, Review

    def call(fn, *args):
        try:
            return fn(*args)
        except (ValueError, OSError, KeyError) as e:
            return err(e)

    @mcp.tool(
        description="Read schemas for approved project briefs, candidate graphs and reviews. "
        "Start a pipeline-development or goal-analysis session first. Interview the user "
        "about grain, population, semantics, source boundaries and acceptance criteria."
    )
    def project_schema() -> dict:
        return {
            "contract": Contract.model_json_schema(),
            "candidate": Candidate.model_json_schema(),
            "review": Review.model_json_schema(),
        }

    @mcp.tool(
        description="Draft a complete project brief for human approval. Revision 0 creates it. "
        "Changing a brief invalidates approvals and candidates. Never relax tests just to pass."
    )
    def project_draft(session_id: str, spec: dict, revision: int = 0) -> dict:
        return call(engine.draft, session(session_id), spec, revision)

    @mcp.tool(
        description="Read durable project state, generated verification SQL, effective "
        "permissions, budgets and the next action. Revision protects against stale writes."
    )
    def project_status(session_id: str) -> dict:
        return call(engine.status, session(session_id))

    @mcp.tool(
        description="Submit a revised candidate DAG. Query nodes have one input and no "
        "joins/subqueries. Join nodes use approved join contracts and columns over l/r. "
        "Include diagnosis and addressed_checks for repairs. Invalidates earlier evidence."
    )
    def project_candidate(session_id: str, spec: dict, revision: int) -> dict:
        return call(engine.submit_candidate, session(session_id), spec, revision)

    @mcp.tool(
        description="Execute generated full-relation join, grain, population and measure "
        "checks. Errors are unproven; failed checks carry repair guidance. Never supply a verdict."
    )
    def project_verify(session_id: str, revision: int) -> dict:
        return call(engine.verify, session(session_id), revision)

    @mcp.tool(
        description="Inspect bounded violating-row examples from a failed generated probe. "
        "This helps diagnose fanout, missing keys and wrong values. Samples cannot satisfy "
        "acceptance checks; repair the candidate and rerun full project_verify."
    )
    def project_diagnose(session_id: str, check_id: str, max_rows: int = 20) -> dict:
        return call(engine.diagnose, session(session_id), check_id, max_rows)

    @mcp.tool(
        description="Record a critical review after passing verification. Answer every "
        "approved review question in order; cite fresh query IDs, issues and limitations."
    )
    def project_review(session_id: str, spec: dict, revision: int) -> dict:
        return call(engine.record_review, session(session_id), spec, revision)

    @mcp.tool(
        description="Update a working plan inside the approved brief. Completed steps "
        "require evidence; prerequisites must occur earlier. Does not change acceptance gates."
    )
    def project_plan(session_id: str, steps: list[dict], revision: int) -> dict:
        return call(engine.plan, session(session_id), steps, revision)

    @mcp.tool(
        description="Finish a project under bounded autonomy after fresh passing checks, "
        "review and workflow checkpoints. Other approval levels require the human console."
    )
    def project_finish(session_id: str, revision: int) -> dict:
        return call(engine.finish, session(session_id), revision)

    @mcp.tool(
        description="Prepare exact CREATE SQL for a verified pipeline. Human approval "
        "and execution remain mandatory; existing objects are never replaced automatically."
    )
    def project_deployment(session_id: str, revision: int) -> dict:
        return call(engine.deployment_package, session(session_id), revision)

    @mcp.tool(
        description="After the approved DDL is reported applied, test the actual deployed "
        "relation in a separate audit session within the approved destination scope."
    )
    def project_deployment_check(session_id: str, revision: int) -> dict:
        return call(engine.deployment_check, session(session_id), revision)

    @mcp.tool(
        description="Complete deployed work after fresh passing deployment checks. "
        "Only bounded autonomy permits this agent action; otherwise human acceptance is required."
    )
    def project_accept_deployment(session_id: str, revision: int) -> dict:
        return call(engine.accept_deployment, session(session_id), revision)

    @mcp.tool(
        description="Pause or block a project with a specific reason. Resume and cancel "
        "are human controls. File structured interventions for missing human decisions."
    )
    def project_pause(session_id: str, reason: str, revision: int, blocked: bool = False) -> dict:
        return call(
            engine.control,
            session(session_id),
            "block" if blocked else "pause",
            reason,
            revision,
            "agent",
        )

    @mcp.tool(
        description="Replay unchanged SQL checks in a separate session, consuming one "
        "explicitly preapproved max_revalidations allowance. Use a stable request_id on "
        "retries. Prior results stay historical; no candidate edits are permitted."
    )
    def project_revalidate(session_id: str, request_id: str, revision: int) -> dict:
        from grayson.projects.reuse import revalidate

        return call(revalidate, session(session_id), request_id, revision)

    @mcp.tool(
        description="Propose a passed project criterion as a regression check. Human "
        "activation remains mandatory; deployed evidence is preferred when available."
    )
    def project_promote(session_id: str, criterion_id: str, check_id: str) -> dict:
        from grayson.projects.reuse import propose_regression

        return call(propose_regression, session(session_id), criterion_id, check_id)

    @mcp.tool(
        description="Prepare a reusable workflow recipe with a historical example. "
        "Returns YAML and preview; save via the normal workflow review/authoring flow."
    )
    def project_recipe(session_id: str, name: str) -> dict:
        from grayson.projects.reuse import recipe

        return call(recipe, session(session_id), name)
