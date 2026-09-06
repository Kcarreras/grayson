"""Thin command surfaces for optional evidence workflows."""

from pathlib import Path
from typing import Annotated

import typer
import yaml


def register(app, session, workspace, emit, fail):
    from grayson.core import criteria

    group = typer.Typer(
        help="Approve explicit success criteria before a fix.", no_args_is_help=True
    )
    app.add_typer(group, name="criteria")

    def call(fn, *args):
        try:
            result = fn(*args)
            emit(result)
            if isinstance(result, dict) and result.get("verdict") in {"fail", "unproven"}:
                raise typer.Exit(1)
        except (ValueError, OSError, KeyError) as e:
            fail(str(e))

    @group.command("set")
    def criteria_set(session_id: str, pid: str, file: Path):
        """Attach a format-1 JSON/YAML criteria file to a pending fix."""
        try:
            spec = yaml.safe_load(file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            fail(str(e))
        call(criteria.set_criteria, session(session_id), pid, spec)

    @group.command("show")
    def criteria_show(session_id: str, pid: str):
        call(criteria.contract, session(session_id), pid)

    @group.command("run")
    def criteria_run(session_id: str, pid: str):
        """Execute fresh evidence and compute pass/fail/unproven after application."""
        call(criteria.run_verification, session(session_id), pid)

    @group.command("promote")
    def criteria_promote(session_id: str, pid: str, criterion_id: str, check_id: str):
        call(criteria.promote, session(session_id), pid, criterion_id, check_id)

    from grayson.knowledge import impact

    impact_group = typer.Typer(
        help="Plan a scoped investigation from detected changes.", no_args_is_help=True
    )
    app.add_typer(impact_group, name="impact")

    @impact_group.command("plan")
    def impact_plan(
        session_id: str,
        table: Annotated[list[str] | None, typer.Option("--table")] = None,
        freshness_days: int = 7,
    ):
        call(impact.build_plan, session(session_id), table, freshness_days)

    @impact_group.command("show")
    def impact_show(session_id: str):
        call(impact.show_plan, session(session_id))

    @impact_group.command("launch")
    def impact_launch(session_id: str, digest: str):
        call(impact.launch, session(session_id), digest)

    @impact_group.command("run-checks")
    def impact_run_checks(session_id: str):
        call(impact.run_plan_checks, session(session_id))

    from grayson.core import comparisons

    compare_group = typer.Typer(
        help="Compare releases, pipelines or equivalent time windows.", no_args_is_help=True
    )
    app.add_typer(compare_group, name="comparison")

    @compare_group.command("create")
    def comparison_create(session_id: str, file: Path):
        """Save an immutable format-1 comparison contract from JSON/YAML."""
        try:
            spec = yaml.safe_load(file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as e:
            fail(str(e))
        call(comparisons.create, session(session_id), spec)

    @compare_group.command("list")
    def comparison_list(session_id: str):
        call(comparisons.inventory, session(session_id))

    @compare_group.command("show")
    def comparison_show(session_id: str, comparison_id: str):
        call(comparisons.show, session(session_id), comparison_id)

    @compare_group.command("run")
    def comparison_run(session_id: str, comparison_id: str):
        call(comparisons.run, session(session_id), comparison_id)

    @compare_group.command("report")
    def comparison_report(
        session_id: str,
        comparison_id: str,
        output: Annotated[Path | None, typer.Option("--output")] = None,
    ):
        """Read the evidence-linked report; optionally export JSON to a file."""
        import json

        from grayson.util import atomic_write_text

        try:
            report = comparisons.latest_report(session(session_id), comparison_id)
            if output:
                atomic_write_text(output, json.dumps(report, indent=2, default=str))
            emit(report)
        except (ValueError, OSError) as e:
            fail(str(e))
