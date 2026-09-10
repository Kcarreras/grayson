"""Project CLI, including human-only review and a provider-neutral runner."""

import json
from pathlib import Path

import typer

from grayson.cli_input import read_spec, read_text


def register(app, session, emit, fail, require_interactive, default_actor):
    from grayson.projects import engine
    from grayson.projects.models import Candidate, Contract, Review

    group = typer.Typer(help="Develop bounded, verified SQL projects.", no_args_is_help=True)
    app.add_typer(group, name="project")

    def call(fn, *args):
        try:
            result = fn(*args)
            emit(result)
        except (ValueError, OSError, KeyError) as e:
            fail(str(e))

    def read(file, *, expected=dict):
        try:
            return read_spec(file, expected=expected)
        except ValueError as e:
            fail(str(e))

    @group.command("schema")
    def schema():
        emit(
            {
                "contract": Contract.model_json_schema(),
                "candidate": Candidate.model_json_schema(),
                "review": Review.model_json_schema(),
            }
        )

    @group.command("status")
    def status(session_id: str):
        call(engine.status, session(session_id))

    @group.command("draft")
    def draft(session_id: str, file: Path, revision: int = 0):
        call(engine.draft, session(session_id), read(file), revision)

    @group.command("approve")
    def approve(session_id: str, revision: int, digest: str):
        require_interactive("approve a project brief")
        call(engine.approve, session(session_id), revision, digest, "user")

    @group.command("candidate")
    def candidate(session_id: str, file: Path, revision: int):
        call(engine.submit_candidate, session(session_id), read(file), revision)

    @group.command("approve-candidate")
    def approve_candidate(session_id: str, revision: int, digest: str):
        require_interactive("approve a project candidate")
        call(engine.approve_candidate, session(session_id), revision, digest, "user")

    @group.command("verify")
    def verify(session_id: str, revision: int):
        call(engine.verify, session(session_id), revision)

    @group.command("review")
    def review(session_id: str, file: Path, revision: int):
        call(engine.record_review, session(session_id), read(file), revision)

    @group.command("diagnose")
    def diagnose(session_id: str, check_id: str, max_rows: int = 20):
        call(engine.diagnose, session(session_id), check_id, max_rows)

    @group.command("plan")
    def plan(session_id: str, file: Path, revision: int):
        call(engine.plan, session(session_id), read(file, expected=list), revision)

    @group.command("finish")
    def finish(session_id: str, revision: int):
        call(engine.finish, session(session_id), revision, default_actor())

    @group.command("control")
    def control(session_id: str, action: str, reason: str, revision: int):
        if action in {"resume", "cancel"}:
            require_interactive(action + " a project")
        call(engine.control, session(session_id), action, reason, revision, default_actor())

    @group.command("approval")
    def approval(session_id: str, level: str, revision: int):
        require_interactive("change project approval level")
        call(engine.set_approval, session(session_id), level, revision, "user")

    @group.command("deployment")
    def deployment(session_id: str, revision: int):
        call(engine.deployment_package, session(session_id), revision)

    @group.command("deployment-check")
    def deployment_check(session_id: str, revision: int):
        call(engine.deployment_check, session(session_id), revision)

    @group.command("accept-deployment")
    def accept_deployment(session_id: str, revision: int):
        call(engine.accept_deployment, session(session_id), revision, default_actor())

    @group.command("run")
    def run(session_id: str, provider_file: Path, max_steps: int = 100, watch: bool = False):
        """Resume with a human-configured JSON argv provider; never executes DDL."""
        from grayson.projects.runner import command_provider, drive
        from grayson.projects.runner import watch as watch_project

        try:
            argv = json.loads(read_text(provider_file))
            if watch:
                emit(
                    watch_project(
                        session(session_id),
                        command_provider(argv),
                        max_steps=max_steps,
                        on_change=emit,
                    )
                )
            else:
                emit(drive(session(session_id), command_provider(argv), max_steps=max_steps))
        except (ValueError, OSError) as e:
            fail(str(e))

    @group.command("revalidate")
    def revalidate(session_id: str, request_id: str, revision: int):
        """Replay unchanged checks using an explicitly preapproved finite allowance."""
        from grayson.projects.reuse import revalidate

        call(revalidate, session(session_id), request_id, revision)

    @group.command("promote")
    def promote(session_id: str, criterion_id: str, check_id: str):
        """Propose a passed check for the regression library; activation stays human."""
        from grayson.projects.reuse import propose_regression

        call(propose_regression, session(session_id), criterion_id, check_id)

    @group.command("recipe")
    def recipe(session_id: str, name: str):
        """Return a reusable workflow draft for normal review and authoring."""
        from grayson.projects.reuse import recipe

        call(recipe, session(session_id), name)
