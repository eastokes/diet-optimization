"""CLI commands for actual intake, planned intake, goals, and activity context."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import typer
from rich.console import Console

from llmn.db.connection import get_db
from llmn.tracking.errors import TrackingError
from llmn.tracking.estimation import AnalysisWindow, summarize_intake
from llmn.tracking.models import ActivityPeriod, GoalPeriod, IntakeEntry
from llmn.tracking.queries import ActivityQueries, GoalQueries, IntakeQueries
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
            entry = IntakeQueries.upsert(
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

    _emit(
        command,
        json_output=json_output,
        data={"entry": _intake_to_dict(entry)},
        human_summary=f"Logged {calories:.0f} kcal ({kind}, {completeness}) for {log_date}.",
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
    _emit(
        command,
        json_output=json_output,
        data=data,
        human_summary=(
            f"{len(entries)} intake entries; actual coverage "
            f"{summary.weighted_coverage:.0%} with "
            f"{summary.complete_days} complete day(s)."
        ),
    )


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
