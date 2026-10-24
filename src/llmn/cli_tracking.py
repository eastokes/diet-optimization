"""CLI commands for actual intake, planned intake, goals, and activity context."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from llmn.db.connection import get_db
from llmn.tracking.bodycomp import compare_compositions, select_comparison
from llmn.tracking.errors import TrackingError
from llmn.tracking.estimation import AnalysisWindow, IntakeSummary, summarize_intake
from llmn.tracking.models import (
    ActivityPeriod,
    BodyComposition,
    GoalPeriod,
    IntakeEntry,
    IntakeWriteResult,
)
from llmn.tracking.queries import (
    ActivityQueries,
    BodyCompositionQueries,
    GoalQueries,
    IntakeQueries,
)
from llmn.tracking.recommendations import GoalSpec, validate_goal
from llmn.tracking.service import finite_or_raise, get_profile, resolve_goal

console = Console()

intake_app = typer.Typer(help="Log actual or planned calorie intake")
goal_app = typer.Typer(help="Set effective-dated weight-change goals")
activity_app = typer.Typer(help="Record effective-dated activity context")


def _ensure_tables() -> None:
    get_db().initialize_schema()


@intake_app.callback()
def intake_callback() -> None:
    _ensure_tables()


@goal_app.callback()
def goal_callback() -> None:
    _ensure_tables()


@activity_app.callback()
def activity_callback() -> None:
    _ensure_tables()


def _emit(
    command: str,
    *,
    json_output: bool,
    data: dict[str, Any],
    human_summary: str,
    human_lines: list[str] | None = None,
    warnings: list[str] | None = None,
    suggestions: list[str] | None = None,
) -> None:
    if json_output:
        payload = {
            "success": True,
            "command": command,
            "data": data,
            "errors": [],
            "warnings": warnings or [],
            "suggestions": suggestions or [],
            "human_summary": human_summary,
            "schema_version": 2,
        }
        print(json.dumps(payload, indent=2))
        return

    for line in human_lines or [human_summary]:
        console.print(line)
    for warning in warnings or []:
        console.print(f"[yellow]{warning}[/yellow]")
    for suggestion in suggestions or []:
        console.print(f"[dim]Hint: {suggestion}[/dim]")


def _fail(
    command: str,
    error: TrackingError,
    json_output: bool,
) -> None:
    if json_output:
        payload = {
            "success": False,
            "command": command,
            "data": {},
            "errors": [error.message],
            "error_codes": [error.code],
            "warnings": [],
            "suggestions": error.suggestions,
            "human_summary": f"Error: {error.message}",
            "schema_version": 2,
        }
        print(json.dumps(payload, indent=2))
    else:
        console.print(f"[red]{error.message}[/red]")
        for suggestion in error.suggestions:
            console.print(f"  {suggestion}")
    raise typer.Exit(1)


def _parse_date(
    value: str | None,
    *,
    default: date,
    allow_future: bool,
    field_name: str,
) -> date:
    if value is None:
        parsed = default
    else:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise TrackingError(
                "INVALID_DATE",
                f"{field_name} must be an ISO date (YYYY-MM-DD), got '{value}'.",
            ) from exc

    if not allow_future and parsed > date.today():
        raise TrackingError(
            "FUTURE_DATE",
            f"{field_name} cannot be in the future ({parsed.isoformat()}).",
        )
    return parsed


def _intake_to_dict(entry: IntakeEntry) -> dict[str, object]:
    return {
        "date": entry.date.isoformat(),
        "kcal": entry.kcal,
        "kind": entry.kind,
        "completeness": entry.completeness,
        "quality": entry.quality,
        "provenance": entry.provenance,
        "revision": entry.revision,
        "notes": entry.notes,
    }


def _intake_write_to_dict(result: IntakeWriteResult) -> dict[str, object]:
    previous: dict[str, object] | None = None
    if result.replaced:
        previous = {
            "kcal": result.previous_kcal,
            "quality": result.previous_quality,
            "completeness": result.previous_completeness,
            "revision": result.previous_revision,
        }
    return {
        "entry": _intake_to_dict(result.entry),
        "replaced": result.replaced,
        "previous": previous,
    }


def _intake_log_feedback(result: IntakeWriteResult) -> list[str]:
    entry = result.entry
    lines = [
        f"Logged {entry.kcal:.0f} kcal for {entry.date} "
        f"({entry.kind}, {entry.completeness}, {entry.quality}; "
        f"revision {entry.revision})."
    ]
    if result.replaced and result.previous_kcal is not None:
        lines.append(
            f"Replaced the earlier {result.previous_kcal:.0f} kcal entry "
            f"({result.previous_quality}, revision {result.previous_revision})."
        )
    else:
        lines.append(f"First {entry.kind} entry stored for {entry.date}.")
    return lines


def _coverage_lines(summary: IntakeSummary, entries: list[IntakeEntry]) -> list[str]:
    actual = sum(1 for entry in entries if entry.kind == "actual")
    planned = sum(1 for entry in entries if entry.kind == "planned")
    legacy = sum(1 for entry in entries if entry.kind == "legacy_unclassified")
    lines = [
        f"Actual intake: {summary.complete_days} complete day(s), "
        f"{summary.partial_days} partial, {summary.estimated_days} estimated "
        f"({actual} actual entry(ies), {planned} planned, {legacy} legacy).",
        f"Coverage: {summary.weighted_coverage:.0%} of {summary.window_days} calendar day(s).",
        f"  {summary.measured_equivalent_days:.1f} measured-equivalent day(s) / "
        f"{summary.window_days}; estimated days count half weight, partial days count "
        "neither toward the mean nor the weight.",
        f"  Unknown: {summary.unknown_days} day(s) have no actual intake and are not "
        "counted as zero.",
    ]
    if summary.mean_kcal is not None:
        lines.append(
            f"Mean over logged complete days: {summary.mean_kcal:.0f} kcal/day "
            "(not a whole-window average)."
        )
    else:
        lines.append("Mean over logged complete days: none (no complete actual-intake days).")
    return lines


def _goal_to_dict(goal: GoalSpec) -> dict[str, object]:
    return goal.to_dict()


@intake_app.command("log")
def intake_log(
    calories: float = typer.Argument(..., help="Calories for the day"),
    date_str: str | None = typer.Option(
        None, "--date", "-d", help="Date (YYYY-MM-DD, default: today)"
    ),
    kind: str = typer.Option(
        "actual", "--kind", help="actual or planned (planned is not consumed intake)"
    ),
    completeness: str = typer.Option("complete", "--completeness", help="complete or partial"),
    quality: str = typer.Option("measured", "--quality", help="measured or estimated"),
    notes: str | None = typer.Option(None, "--notes", "-n"),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Log a dated actual-intake observation (or an explicitly planned value)."""
    command = "intake log"
    try:
        if kind not in ("actual", "planned"):
            raise TrackingError("INVALID_INTAKE_KIND", "--kind must be actual or planned.")
        log_date = _parse_date(
            date_str,
            default=date.today(),
            allow_future=(kind == "planned"),
            field_name="--date",
        )
        finite_or_raise(calories, "calories")
        if calories < 0:
            raise TrackingError("NEGATIVE_INTAKE", "Calories must be zero or greater.")

        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            result = IntakeQueries.upsert(
                conn,
                profile.user_id,
                log_date,
                calories,
                kind=kind,
                completeness=completeness,
                quality=quality,
                notes=notes,
            )
    except TrackingError as error:
        _fail(command, error, json_output)
        return
    except ValueError as error:
        _fail(
            command,
            TrackingError("INVALID_INTAKE", str(error)),
            json_output,
        )
        return

    warnings: list[str] = []
    if kind == "planned":
        warnings.append(
            "Planned intake is not consumed intake and is excluded from expenditure learning."
        )
    if quality == "estimated":
        warnings.append(
            "Estimated intake contributes half weight to coverage and expenditure learning."
        )
    feedback = _intake_log_feedback(result)
    _emit(
        command,
        json_output=json_output,
        data=_intake_write_to_dict(result),
        human_summary=feedback[0],
        human_lines=feedback,
        warnings=warnings,
    )


@intake_app.command("list")
def intake_list(
    days: int = typer.Option(30, "--days", "-d", help="Calendar days to show"),
    as_of_str: str | None = typer.Option(None, "--as-of", help="Analysis date (default: today)"),
    kind: str | None = typer.Option(
        None, "--kind", help="Filter to actual, planned, or legacy_unclassified"
    ),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List intake observations and actual-intake coverage."""
    command = "intake list"
    try:
        as_of = _parse_date(
            as_of_str,
            default=date.today(),
            allow_future=False,
            field_name="--as-of",
        )
        window = AnalysisWindow(
            start=as_of - timedelta(days=max(1, days) - 1),
            end=as_of,
        )
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            entries = IntakeQueries.get_entries(
                conn,
                profile.user_id,
                start_date=window.start,
                end_date=window.end,
                kind=kind,
            )
            summary = summarize_intake(entries, window)
    except TrackingError as error:
        _fail(command, error, json_output)
        return

    data = {
        "window": window.to_dict(),
        "entries": [_intake_to_dict(entry) for entry in entries],
        "coverage": summary.to_dict(),
    }
    if json_output:
        _emit(
            command,
            json_output=True,
            data=data,
            human_summary=(
                f"{len(entries)} intake entries; actual coverage "
                f"{summary.weighted_coverage:.0%} "
                f"({summary.complete_days} complete day(s))."
            ),
        )
        return

    console.print(
        f"[bold]Intake entries[/bold] {window.start} \u2192 {window.end} "
        f"({summary.window_days} calendar days)"
    )
    if entries:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Date", style="cyan")
        table.add_column("kcal", justify="right")
        table.add_column("Kind")
        table.add_column("Completeness")
        table.add_column("Quality")
        table.add_column("Rev", justify="right")
        for entry in entries:
            table.add_row(
                entry.date.isoformat(),
                f"{entry.kcal:.0f}",
                entry.kind,
                entry.completeness,
                entry.quality,
                str(entry.revision),
            )
        console.print(table)
    else:
        console.print("No intake entries in this window.")
    console.print()
    for line in _coverage_lines(summary, entries):
        console.print(line, soft_wrap=True)


@intake_app.command("delete")
def intake_delete(
    date_str: str = typer.Option(..., "--date", "-d", help="Date (YYYY-MM-DD)"),
    kind: str = typer.Option("actual", "--kind", help="actual or planned"),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Delete a dated intake observation."""
    command = "intake delete"
    try:
        log_date = _parse_date(
            date_str,
            default=date.today(),
            allow_future=True,
            field_name="--date",
        )
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            deleted = IntakeQueries.delete(conn, profile.user_id, log_date, kind)
        if not deleted:
            raise TrackingError(
                "INTAKE_NOT_FOUND",
                f"No {kind} intake entry found on {log_date}.",
            )
    except TrackingError as error:
        _fail(command, error, json_output)
        return

    _emit(
        command,
        json_output=json_output,
        data={"date": log_date.isoformat(), "kind": kind, "deleted": True},
        human_summary=f"Deleted {kind} intake for {log_date}.",
    )


@intake_app.command("confirm-legacy")
def intake_confirm_legacy(
    from_str: str | None = typer.Option(None, "--from", help="First date"),
    to_str: str | None = typer.Option(None, "--to", help="Last date"),
    all_dates: bool = typer.Option(False, "--all", help="Include every legacy row"),
    as_kind: str = typer.Option("actual", "--as-kind", help="Reclassify as actual or planned"),
    completeness: str = typer.Option("complete", "--completeness", help="complete or partial"),
    quality: str = typer.Option(
        "estimated",
        "--quality",
        help="measured or estimated (conservative default for old plans)",
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Apply the change; without it this is a preview"
    ),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Preview or confirm legacy planned-calorie rows as measured intake."""
    command = "intake confirm-legacy"
    try:
        if not all_dates and not from_str and not to_str:
            raise TrackingError(
                "MISSING_LEGACY_SCOPE",
                "Provide --all or a --from/--to date range.",
            )
        if as_kind not in ("actual", "planned"):
            raise TrackingError("INVALID_INTAKE_KIND", "--as-kind must be actual or planned.")
        if completeness not in ("complete", "partial"):
            raise TrackingError(
                "INVALID_COMPLETENESS", "--completeness must be complete or partial."
            )
        if quality not in ("measured", "estimated"):
            raise TrackingError("INVALID_QUALITY", "--quality must be measured or estimated.")

        start = (
            _parse_date(
                from_str,
                default=date.min,
                allow_future=True,
                field_name="--from",
            )
            if from_str
            else None
        )
        end = (
            _parse_date(
                to_str,
                default=date.today(),
                allow_future=True,
                field_name="--to",
            )
            if to_str
            else None
        )

        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            preview = IntakeQueries.preview_legacy(conn, profile.user_id, start, end)
            updated: list[date] = []
            skipped: list[date] = []
            if apply:
                updated, skipped = IntakeQueries.classify_legacy(
                    conn,
                    profile.user_id,
                    as_kind=as_kind,
                    completeness=completeness,
                    quality=quality,
                    start_date=start,
                    end_date=end,
                )
    except TrackingError as error:
        _fail(command, error, json_output)
        return

    data = {
        "applied": apply,
        "preview_dates": [value.isoformat() for value in preview],
        "updated_dates": [value.isoformat() for value in updated],
        "skipped_conflict_dates": [value.isoformat() for value in skipped],
    }
    summary = (
        f"Preview: {len(preview)} legacy row(s)."
        if not apply
        else f"Classified {len(updated)} legacy row(s); skipped {len(skipped)} conflict(s)."
    )
    _emit(
        command,
        json_output=json_output,
        data=data,
        human_summary=summary,
        warnings=(
            ["No change applied. Re-run with --apply after reviewing the preview."]
            if not apply and preview
            else []
        ),
    )


@goal_app.command("set")
def goal_set(
    mode: str = typer.Option(..., "--mode", help="loss, maintenance, or gain"),
    rate: float | None = typer.Option(
        None,
        "--rate",
        help="Signed lbs/week (loss negative, gain positive); defaults by mode",
    ),
    target: float | None = typer.Option(None, "--target", help="Optional target weight in lbs"),
    effective_from_str: str | None = typer.Option(
        None, "--effective-from", help="Date the goal starts (default: today)"
    ),
    activity: str | None = typer.Option(
        None,
        "--activity",
        help=(
            "Also record activity level for this date "
            "(sedentary/lightly_active/moderate/active/very_active)"
        ),
    ),
    training_priority: str | None = typer.Option(None, "--training-priority"),
    notes: str | None = typer.Option(None, "--notes", "-n"),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Create or replace the goal effective on a date."""
    command = "goal set"
    try:
        effective_from = _parse_date(
            effective_from_str,
            default=date.today(),
            allow_future=False,
            field_name="--effective-from",
        )
        if mode == "maintenance":
            goal_rate = 0.0 if rate is None else rate
        elif mode == "loss":
            goal_rate = -1.0 if rate is None else rate
        elif mode == "gain":
            goal_rate = 0.5 if rate is None else rate
        else:
            raise TrackingError(
                "INVALID_GOAL_MODE",
                "--mode must be loss, maintenance, or gain.",
            )

        spec = GoalSpec(
            mode=mode,
            rate_lbs_per_week=goal_rate,
            target_weight_lbs=target,
            effective_from=effective_from,
            source="goal_period",
            activity_level=activity,
        )
        validate_goal(spec)

        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            activity_period = (
                ActivityPeriod(
                    activity_id=None,
                    user_id=profile.user_id,
                    effective_from=effective_from,
                    activity_level=activity,
                    training_priority=training_priority,
                    notes=notes,
                )
                if activity is not None
                else None
            )
            stored = GoalQueries.set_goal(
                conn,
                GoalPeriod(
                    goal_id=None,
                    user_id=profile.user_id,
                    effective_from=effective_from,
                    mode=mode,
                    rate_lbs_per_week=goal_rate,
                    target_weight_lbs=target,
                    training_priority=training_priority,
                    notes=notes,
                ),
            )
            if activity_period is not None:
                ActivityQueries.set_activity(conn, activity_period)
    except TrackingError as error:
        _fail(command, error, json_output)
        return
    except ValueError as error:
        _fail(
            command,
            TrackingError("INVALID_GOAL", str(error)),
            json_output,
        )
        return

    _emit(
        command,
        json_output=json_output,
        data={
            "goal": {
                "mode": stored.mode,
                "rate_lbs_per_week": stored.rate_lbs_per_week,
                "target_weight_lbs": stored.target_weight_lbs,
                "effective_from": stored.effective_from.isoformat(),
            },
            "activity": activity,
        },
        human_summary=(
            f"Goal set: {stored.mode} at {stored.rate_lbs_per_week:+.2f} lbs/week "
            f"from {stored.effective_from}."
        ),
    )


@goal_app.command("show")
def goal_show(
    as_of_str: str | None = typer.Option(None, "--as-of", help="Date to resolve (default: today)"),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show the goal effective on a date."""
    command = "goal show"
    try:
        as_of = _parse_date(
            as_of_str,
            default=date.today(),
            allow_future=False,
            field_name="--as-of",
        )
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            goal = resolve_goal(conn, profile, as_of)
    except TrackingError as error:
        _fail(command, error, json_output)
        return

    data = {
        "as_of": as_of.isoformat(),
        "goal": _goal_to_dict(goal) if goal else None,
    }
    human = f"{goal.mode} at {goal.rate_lbs_per_week:+.2f} lbs/week" if goal else "No goal set."
    _emit(
        command,
        json_output=json_output,
        data=data,
        human_summary=human,
    )


@goal_app.command("history")
def goal_history(
    as_of_str: str | None = typer.Option(
        None, "--as-of", help="Only include goals effective by this date"
    ),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List dated goal periods."""
    command = "goal history"
    try:
        as_of = (
            _parse_date(
                as_of_str,
                default=date.today(),
                allow_future=False,
                field_name="--as-of",
            )
            if as_of_str
            else None
        )
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            periods = GoalQueries.get_history(conn, profile.user_id, as_of)
    except TrackingError as error:
        _fail(command, error, json_output)
        return

    data = {
        "goals": [
            {
                "effective_from": period.effective_from.isoformat(),
                "mode": period.mode,
                "rate_lbs_per_week": period.rate_lbs_per_week,
                "target_weight_lbs": period.target_weight_lbs,
                "training_priority": period.training_priority,
                "notes": period.notes,
            }
            for period in periods
        ]
    }
    _emit(
        command,
        json_output=json_output,
        data=data,
        human_summary=f"{len(periods)} goal period(s).",
    )


@activity_app.command("set")
def activity_set(
    level: str = typer.Option(..., "--level", help="Activity level"),
    effective_from_str: str | None = typer.Option(
        None, "--effective-from", help="Date the context starts (default: today)"
    ),
    training_priority: str | None = typer.Option(None, "--training-priority"),
    notes: str | None = typer.Option(None, "--notes", "-n"),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Record effective-dated activity context."""
    command = "activity set"
    try:
        effective_from = _parse_date(
            effective_from_str,
            default=date.today(),
            allow_future=False,
            field_name="--effective-from",
        )
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            period = ActivityQueries.set_activity(
                conn,
                ActivityPeriod(
                    activity_id=None,
                    user_id=profile.user_id,
                    effective_from=effective_from,
                    activity_level=level,
                    training_priority=training_priority,
                    notes=notes,
                ),
            )
    except TrackingError as error:
        _fail(command, error, json_output)
        return
    except ValueError as error:
        _fail(
            command,
            TrackingError("INVALID_ACTIVITY", str(error)),
            json_output,
        )
        return

    _emit(
        command,
        json_output=json_output,
        data={
            "effective_from": period.effective_from.isoformat(),
            "activity_level": period.activity_level,
            "training_priority": period.training_priority,
            "notes": period.notes,
        },
        human_summary=(f"Activity set to {period.activity_level} from {period.effective_from}."),
    )


@activity_app.command("show")
def activity_show(
    as_of_str: str | None = typer.Option(None, "--as-of", help="Date to resolve (default: today)"),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Show the activity context effective on a date."""
    command = "activity show"
    try:
        as_of = _parse_date(
            as_of_str,
            default=date.today(),
            allow_future=False,
            field_name="--as-of",
        )
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            period = ActivityQueries.get_effective_activity(conn, profile.user_id, as_of)
    except TrackingError as error:
        _fail(command, error, json_output)
        return

    level = period.activity_level if period else profile.activity_level
    _emit(
        command,
        json_output=json_output,
        data={
            "as_of": as_of.isoformat(),
            "activity_level": level,
            "source": "activity_period" if period else "profile_default",
            "training_priority": period.training_priority if period else None,
        },
        human_summary=f"Activity level: {level}.",
    )


@activity_app.command("history")
def activity_history(
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List dated activity periods."""
    command = "activity history"
    try:
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            periods = ActivityQueries.get_history(conn, profile.user_id)
    except TrackingError as error:
        _fail(command, error, json_output)
        return

    _emit(
        command,
        json_output=json_output,
        data={
            "periods": [
                {
                    "effective_from": period.effective_from.isoformat(),
                    "activity_level": period.activity_level,
                    "training_priority": period.training_priority,
                    "notes": period.notes,
                }
                for period in periods
            ]
        },
        human_summary=f"{len(periods)} activity period(s).",
    )


# ---------------------------------------------------------------------------
# Body composition
# ---------------------------------------------------------------------------

bodycomp_app = typer.Typer(
    help="Track body composition (DEXA, tape, scale) and get recomp feedback"
)


@bodycomp_app.callback()
def bodycomp_callback() -> None:
    _ensure_tables()


def _bodycomp_feedback(
    entry: BodyComposition,
    previous: BodyComposition | None,
) -> tuple[list[str], list[str]]:
    """Build human feedback lines and warnings for a saved measurement."""
    lines = [
        f"Saved {entry.method} measurement for {entry.measured_at}: "
        f"{_fmt(entry.weight_lbs, 1, 'lbs')} weight, "
        f"{_fmt(entry.derived_body_fat_pct, 1, '%')} body fat, "
        f"{_fmt(entry.derived_fat_mass_lbs, 1, 'lbs')} fat mass, "
        f"{_fmt(entry.derived_fat_free_mass_lbs, 1, 'lbs')} fat-free mass."
    ]
    warnings: list[str] = []
    if previous is None:
        lines.append(
            "This is the first comparable measurement; add a second scan to measure "
            "recomposition rather than inferring it from scale weight."
        )
        return lines, warnings

    delta = compare_compositions(previous, entry)
    lines.append(f"Change since {previous.measured_at}: {delta.summary}")
    warnings.extend(delta.warnings)
    return lines, warnings


def _fmt(value: float | None, digits: int, unit: str) -> str:
    return f"{value:.{digits}f} {unit}" if value is not None else "no"


@bodycomp_app.command("add")
def bodycomp_add(
    date_str: str | None = typer.Option(
        None, "--date", "-d", help="Measurement date (YYYY-MM-DD, default: today)"
    ),
    method: str = typer.Option(
        "dexa", "--method", help="dexa, bodpod, caliper, bia_scale, tape, or other"
    ),
    weight: float | None = typer.Option(None, "--weight", help="Scale or scan weight in lbs"),
    body_fat: float | None = typer.Option(None, "--body-fat", help="Body-fat percentage"),
    fat_mass: float | None = typer.Option(None, "--fat-mass", help="Fat mass in lbs"),
    lean_mass: float | None = typer.Option(
        None, "--lean-mass", help="Lean soft-tissue mass in lbs (DEXA lean, excludes bone)"
    ),
    bone_mass: float | None = typer.Option(None, "--bone-mass", help="Bone mineral content in lbs"),
    fat_free_mass: float | None = typer.Option(
        None, "--fat-free-mass", help="Fat-free mass in lbs, if reported directly"
    ),
    waist: float | None = typer.Option(None, "--waist", help="Waist circumference in inches"),
    rmr: float | None = typer.Option(
        None, "--rmr", help="Resting metabolic rate in kcal/day, if reported"
    ),
    notes: str | None = typer.Option(None, "--notes", "-n"),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Record a dated body-composition measurement."""
    command = "bodycomp add"
    provided = [
        weight,
        body_fat,
        fat_mass,
        lean_mass,
        bone_mass,
        fat_free_mass,
        waist,
        rmr,
    ]
    try:
        if all(value is None for value in provided):
            raise TrackingError(
                "MISSING_BODYCOMP_VALUES",
                "Provide at least one measurement (weight, body-fat, mass, waist, or RMR).",
            )
        measured_at = _parse_date(
            date_str, default=date.today(), allow_future=False, field_name="--date"
        )
        if weight is not None:
            finite_or_raise(weight, "--weight")
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            entry = BodyComposition(
                composition_id=None,
                user_id=profile.user_id,
                measured_at=measured_at,
                method=method,
                weight_lbs=weight,
                body_fat_pct=body_fat,
                fat_mass_lbs=fat_mass,
                lean_soft_tissue_lbs=lean_mass,
                bone_mineral_lbs=bone_mass,
                fat_free_mass_lbs=fat_free_mass,
                waist_inches=waist,
                resting_metabolic_rate=rmr,
                notes=notes,
            )
            previous_rows = BodyCompositionQueries.get_entries(
                conn, profile.user_id, end_date=measured_at - timedelta(days=1)
            )
            previous_candidates = [
                row for row in previous_rows if row.derived_fat_mass_lbs is not None
            ]
            previous = previous_candidates[-1] if previous_candidates else None
            stored = BodyCompositionQueries.upsert(conn, entry)
            suggestions: list[str] = []
            if stored.resting_metabolic_rate is not None and not profile.has_measured_rmr:
                suggestions.append(
                    "If this RMR was measured rather than estimated, apply it with: "
                    f"llmn user update --rmr {stored.resting_metabolic_rate:.0f} "
                    f"--rmr-date {stored.measured_at} --rmr-source {stored.method}"
                )
    except TrackingError as error:
        _fail(command, error, json_output)
        return
    except ValueError as error:
        _fail(command, TrackingError("INVALID_BODYCOMP", str(error)), json_output)
        return

    lines, warnings = _bodycomp_feedback(stored, previous)
    data: dict[str, Any] = {
        "entry": stored.to_dict(),
        "previous": previous.to_dict() if previous is not None else None,
        "comparison": (
            compare_compositions(previous, stored).to_dict() if previous is not None else None
        ),
    }
    _emit(
        command,
        json_output=json_output,
        data=data,
        human_summary=" ".join(lines),
        human_lines=lines,
        warnings=warnings,
        suggestions=suggestions,
    )


@bodycomp_app.command("list")
def bodycomp_list(
    days: int = typer.Option(3650, "--days", "-d", help="Calendar days to show"),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """List body-composition measurements."""
    command = "bodycomp list"
    as_of = date.today()
    start = as_of - timedelta(days=max(1, days) - 1)
    try:
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            entries = BodyCompositionQueries.get_entries(
                conn, profile.user_id, start_date=start, end_date=as_of
            )
    except TrackingError as error:
        _fail(command, error, json_output)
        return

    if json_output:
        _emit(
            command,
            json_output=True,
            data={
                "window": {"start": start.isoformat(), "end": as_of.isoformat(), "days": days},
                "entries": [entry.to_dict() for entry in entries],
            },
            human_summary=f"{len(entries)} body-composition measurement(s)",
        )
        return

    if not entries:
        console.print("No body-composition measurements found.")
        console.print(
            "Add one with: llmn bodycomp add --date 2026-09-17 --method dexa "
            "--weight 176.9 --body-fat 25.2 --fat-mass 44.7 --lean-mass 125.3 --bone-mass 6.9"
        )
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Date", style="cyan")
    table.add_column("Method")
    table.add_column("Weight", justify="right")
    table.add_column("BF%", justify="right")
    table.add_column("Fat", justify="right")
    table.add_column("FFM", justify="right")
    table.add_column("Waist", justify="right")
    for entry in entries:
        table.add_row(
            entry.measured_at.isoformat(),
            entry.method,
            _fmt(entry.weight_lbs, 1, ""),
            _fmt(entry.derived_body_fat_pct, 1, ""),
            _fmt(entry.derived_fat_mass_lbs, 1, ""),
            _fmt(entry.derived_fat_free_mass_lbs, 1, ""),
            _fmt(entry.waist_inches, 1, ""),
        )
    console.print(table)
    comparison = select_comparison(entries)
    if comparison is None:
        console.print(
            "\nOnly one comparable measurement so far; a second one enables "
            "recomposition analysis (fat mass vs fat-free mass)."
        )
    else:
        delta = compare_compositions(*comparison)
        console.print(f"\n{delta.summary}")
        for warning in delta.warnings:
            console.print(f"[yellow]{warning}[/yellow]")


@bodycomp_app.command("trend")
def bodycomp_trend(
    from_str: str | None = typer.Option(None, "--from", help="First date to compare"),
    to_str: str | None = typer.Option(None, "--to", help="Last date to compare"),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Compare body-composition measurements for recomp feedback."""
    command = "bodycomp trend"
    try:
        start = _parse_date(from_str, default=date.min, allow_future=False, field_name="--from")
        end = _parse_date(to_str, default=date.today(), allow_future=False, field_name="--to")
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            entries = BodyCompositionQueries.get_entries(
                conn, profile.user_id, start_date=start, end_date=end
            )
    except TrackingError as error:
        _fail(command, error, json_output)
        return

    comparison = select_comparison(entries, start_date=None, end_date=None)
    if comparison is None:
        _fail(
            command,
            TrackingError(
                "INSUFFICIENT_BODYCOMP",
                "Recomp analysis needs two comparable measurements with weight and fat mass.",
                [
                    "Add scans with: llmn bodycomp add --date <YYYY-MM-DD> --method dexa "
                    "--weight <lbs> --fat-mass <lbs>"
                ],
            ),
            json_output,
        )
        return

    delta = compare_compositions(*comparison)
    if json_output:
        _emit(
            command,
            json_output=True,
            data=delta.to_dict(),
            human_summary=delta.summary,
            warnings=delta.warnings,
        )
        return

    console.print("[bold]Body-composition trend[/bold]")
    console.print(
        f"  {delta.start.measured_at} ({delta.start.method}) -> "
        f"{delta.end.measured_at} ({delta.end.method})"
    )
    console.print(f"  Interval: {delta.weeks:.1f} weeks")
    console.print(f"  Classification: {delta.classification}")
    if delta.fat_mass_change_lbs is not None:
        console.print(f"  Fat mass: {delta.fat_mass_change_lbs:+.1f} lbs")
    if delta.fat_free_mass_change_lbs is not None:
        console.print(f"  Fat-free mass: {delta.fat_free_mass_change_lbs:+.1f} lbs")
    if delta.body_fat_pct_change is not None:
        console.print(f"  Body fat: {delta.body_fat_pct_change:+.1f} pts")
    if delta.waist_change_inches is not None:
        console.print(f"  Waist: {delta.waist_change_inches:+.1f} in")
    console.print(f"\n{delta.summary}")
    for warning in delta.warnings:
        console.print(f"[yellow]{warning}[/yellow]")


@bodycomp_app.command("delete")
def bodycomp_delete(
    date_str: str = typer.Option(..., "--date", "-d", help="Measurement date (YYYY-MM-DD)"),
    method: str | None = typer.Option(None, "--method", help="Only delete this method"),
    user_id: int | None = typer.Option(None, "--user", help="User ID"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Delete a body-composition measurement."""
    command = "bodycomp delete"
    try:
        measured_at = _parse_date(
            date_str, default=date.today(), allow_future=True, field_name="--date"
        )
        db = get_db()
        with db.get_connection() as conn:
            profile = get_profile(conn, user_id)
            assert profile.user_id is not None
            deleted = BodyCompositionQueries.delete(conn, profile.user_id, measured_at, method)
        if not deleted:
            raise TrackingError(
                "BODYCOMP_NOT_FOUND",
                f"No body-composition entry found on {measured_at}.",
            )
    except TrackingError as error:
        _fail(command, error, json_output)
        return

    _emit(
        command,
        json_output=json_output,
        data={"date": measured_at.isoformat(), "method": method, "deleted": deleted},
        human_summary=f"Deleted {deleted} body-composition entry(ies) for {measured_at}.",
    )
