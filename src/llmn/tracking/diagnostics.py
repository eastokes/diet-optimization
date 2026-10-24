"""Diagnostic output for weight tracking and TDEE analysis.

Every report is built from the same :class:`TrackingAnalysis` used by
``llmn tdee targets``. That keeps one window, one intake summary, and one
expenditure estimate in the human output, and it labels the two different
weight rates instead of presenting them as interchangeable:

- the EMA rate is the smoothed Hacker's Diet trend over the window endpoints
- the measured rate is a least-squares slope over raw weights, and is the value
  the recommendation actually uses
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from llmn.tracking import policy
from llmn.tracking.bodycomp import (
    BodyCompDelta,
    compare_compositions,
    latest_scan,
    select_comparison,
)
from llmn.tracking.ema import estimate_daily_calorie_balance, estimate_weekly_change
from llmn.tracking.estimation import AnalysisWindow
from llmn.tracking.formula import mifflin_tdee_at_weight
from llmn.tracking.models import BodyComposition, UserProfile
from llmn.tracking.queries import BodyCompositionQueries, WeightQueries
from llmn.tracking.recommendations import GoalSpec, Recommendation
from llmn.tracking.service import TrackingAnalysis


@dataclass
class WeightReport:
    """Summary report of weight tracking progress."""

    current_weight: float
    current_trend: float
    trend_change: float  # vs period start
    weekly_rate: float  # lbs/week from EMA endpoints (smoothed, lagging)
    implied_daily_deficit: float  # kcal/day implied by the EMA rate
    period_days: int
    start_weight: float
    start_trend: float


@dataclass
class TDEEReport:
    """Expenditure report derived from the shared analysis."""

    method: str
    baseline_kcal: float  # Mifflin-St Jeor or measured RMR x activity
    baseline_source: str  # mifflin | measured_rmr
    resting_kcal: float
    tdee_bias: float | None
    adjusted_tdee: float | None
    uncertainty_95ci: float | None
    segments_used: int
    confidence: str
    calculation_weight_lbs: float
    activity_level: str
    avg_actual_calories: float | None
    complete_intake_days: int
    estimated_intake_days: int
    weighted_coverage: float
    window_days: int
    expected_deficit: float | None  # baseline - mean logged intake
    measured_rate_lbs_per_week: float | None
    implied_deficit_measured: float | None  # from the measured rate
    reason_codes: list[str]


@dataclass
class ProgressReport:
    """Combined progress report built from one analysis."""

    weight: WeightReport
    tdee: TDEEReport
    window: AnalysisWindow
    goal: GoalSpec | None
    goal_weight: float | None
    remaining_lbs: float | None
    weeks_to_goal: float | None
    notes: list[str]
    target_kcal_per_day: int | None = None
    expenditure_target_kcal: int | None = None
    target_basis: str | None = None
    latest_composition: BodyComposition | None = None
    body_comp_delta: BodyCompDelta | None = None


def calculate_mifflin_tdee(profile: UserProfile) -> float:
    """Return the Mifflin-St Jeor TDEE at the profile's current weight.

    Kept as a compatibility helper for callers and tests that only need the
    formula branch; the analysis itself uses
    :func:`llmn.tracking.estimation.estimate_expenditure`.
    """
    return calculate_mifflin_tdee_at_weight(profile, profile.weight_lbs)


def calculate_mifflin_tdee_at_weight(
    profile: UserProfile,
    weight_lbs: float,
    activity_level: str | None = None,
) -> float:
    """Calculate TDEE at a specific weight and optional activity override."""
    return mifflin_tdee_at_weight(profile, weight_lbs, activity_level)


def generate_weight_report(
    conn: sqlite3.Connection,
    user_id: int,
    days: int = 30,
    as_of: date | None = None,
) -> WeightReport | None:
    """Generate a weight tracking report for a calendar interval.

    The rate here is the EMA rate (trend endpoints). Recommendations use the
    measured least-squares rate instead; see :func:`build_progress_report`.
    """
    end_date = as_of or date.today()
    history = WeightQueries.get_weight_history(conn, user_id, days=days, end_date=end_date)

    if len(history) < 2:
        return None

    oldest = history[0]
    latest = history[-1]

    period_days = (latest.measured_at - oldest.measured_at).days
    if period_days == 0:
        period_days = 1  # Avoid division by zero

    trend_change = latest.trend_lbs - oldest.trend_lbs
    weekly_rate = estimate_weekly_change(oldest.trend_lbs, latest.trend_lbs, period_days)
    # estimate_daily_calorie_balance returns negative for deficit, but we want
    # "deficit" to be positive when in deficit (losing weight), so negate it
    implied_deficit = -estimate_daily_calorie_balance(weekly_rate)

    return WeightReport(
        current_weight=latest.weight_lbs,
        current_trend=latest.trend_lbs,
        trend_change=trend_change,
        weekly_rate=weekly_rate,
        implied_daily_deficit=implied_deficit,
        period_days=period_days,
        start_weight=oldest.weight_lbs,
        start_trend=oldest.trend_lbs,
    )


def build_tdee_report(analysis: TrackingAnalysis) -> TDEEReport:
    """Describe the analysis expenditure estimate without re-reading history."""
    estimate = analysis.estimate
    mean_intake = estimate.intake.mean_kcal
    expected_deficit = None
    if mean_intake is not None:
        expenditure = (
            estimate.kcal_per_day if estimate.kcal_per_day is not None else estimate.baseline_kcal
        )
        expected_deficit = expenditure - mean_intake

    measured_rate = estimate.observed_rate_lbs_per_week
    implied_measured = (
        -measured_rate * policy.KCAL_PER_LB_WEEK if measured_rate is not None else None
    )

    return TDEEReport(
        method=estimate.method,
        baseline_kcal=estimate.baseline_kcal,
        baseline_source=estimate.baseline_source,
        resting_kcal=estimate.resting_kcal,
        tdee_bias=estimate.bias,
        adjusted_tdee=estimate.kcal_per_day,
        uncertainty_95ci=estimate.uncertainty_95ci,
        segments_used=estimate.segments_used,
        confidence=estimate.confidence,
        calculation_weight_lbs=estimate.calculation_weight_lbs,
        activity_level=estimate.activity_level,
        avg_actual_calories=mean_intake,
        complete_intake_days=estimate.intake.complete_days,
        estimated_intake_days=estimate.intake.estimated_days,
        weighted_coverage=estimate.intake.weighted_coverage,
        window_days=analysis.window.total_days,
        expected_deficit=expected_deficit,
        measured_rate_lbs_per_week=measured_rate,
        implied_deficit_measured=implied_measured,
        reason_codes=list(estimate.reason_codes),
    )


def build_progress_report(
    conn: sqlite3.Connection,
    analysis: TrackingAnalysis,
    profile: UserProfile | None = None,
) -> ProgressReport | None:
    """Build one progress report from an existing analysis.

    ``profile`` is accepted for compatibility with earlier callers; the goal
    and window come from the analysis so every section describes the same
    interval.
    """
    if analysis.profile.user_id is None:
        return None

    window = analysis.window
    days = window.total_days
    weight_report = generate_weight_report(
        conn,
        analysis.profile.user_id,
        days=days,
        as_of=window.end,
    )
    if weight_report is None:
        return None

    tdee_report = build_tdee_report(analysis)
    goal = analysis.goal
    goal_weight = goal.target_weight_lbs if goal is not None else None
    measured_rate = tdee_report.measured_rate_lbs_per_week

    remaining_lbs = None
    weeks_to_goal = None
    if goal_weight is not None:
        remaining_lbs = weight_report.current_trend - goal_weight
        rate_for_projection = measured_rate
        if rate_for_projection is None or rate_for_projection == 0:
            rate_for_projection = weight_report.weekly_rate
        if rate_for_projection is not None and rate_for_projection != 0:
            if remaining_lbs > 0 and rate_for_projection < 0:
                weeks_to_goal = remaining_lbs / abs(rate_for_projection)
            elif remaining_lbs < 0 and rate_for_projection > 0:
                weeks_to_goal = abs(remaining_lbs) / rate_for_projection

    compositions = BodyCompositionQueries.get_entries(
        conn, analysis.profile.user_id, end_date=window.end
    )
    comparison = select_comparison(compositions)
    latest_composition = latest_scan(compositions)
    body_comp_delta = compare_compositions(*comparison) if comparison is not None else None

    notes = _progress_notes(weight_report, tdee_report, goal, analysis.recommendation)
    if (
        latest_composition is not None
        and latest_composition.weight_lbs is not None
        and abs(latest_composition.weight_lbs - weight_report.current_trend) > 2.0
    ):
        notes.append(
            f"Body-composition scan weight ({latest_composition.weight_lbs:.1f} lbs on "
            f"{latest_composition.measured_at}) differs from the current scale trend "
            f"({weight_report.current_trend:.1f} lbs). Methods differ; do not treat the "
            "difference as a body change."
        )

    return ProgressReport(
        weight=weight_report,
        tdee=tdee_report,
        window=window,
        goal=goal,
        goal_weight=goal_weight,
        remaining_lbs=remaining_lbs,
        weeks_to_goal=weeks_to_goal,
        notes=notes,
        target_kcal_per_day=analysis.recommendation.target_kcal_per_day,
        expenditure_target_kcal=analysis.recommendation.expenditure_target_kcal,
        target_basis=analysis.recommendation.target_basis,
        latest_composition=latest_composition,
        body_comp_delta=body_comp_delta,
    )


def _progress_notes(
    weight_report: WeightReport,
    tdee_report: TDEEReport,
    goal: GoalSpec | None,
    recommendation: Recommendation | None = None,
) -> list[str]:
    notes: list[str] = []
    measured = tdee_report.measured_rate_lbs_per_week
    ema = weight_report.weekly_rate

    if measured is not None and abs(ema - measured) > 0.25:
        notes.append(
            f"The EMA rate ({ema:+.2f} lbs/week) and measured rate "
            f"({measured:+.2f} lbs/week) disagree. The EMA lags in short windows; "
            "recommendations use the measured rate."
        )

    if (
        tdee_report.expected_deficit is not None
        and tdee_report.implied_deficit_measured is not None
    ):
        diff = tdee_report.expected_deficit - tdee_report.implied_deficit_measured
        if abs(diff) > 150:
            direction = "slower" if diff > 0 else "faster"
            comparison = (
                "the formula baseline (no validated segments yet)"
                if tdee_report.method == "formula_only"
                else "the expenditure estimate"
            )
            notes.append(
                f"Losing {direction} than {comparison} predicts by ~{abs(diff):.0f} "
                "kcal/day (expected deficit minus deficit implied by the measured "
                "rate). Possible causes: unlogged intake, activity different from the "
                "assumed level, or a baseline that does not fit you yet."
            )

    if measured is not None and goal is not None:
        difference = measured - goal.rate_lbs_per_week
        if abs(difference) > policy.RATE_TOLERANCE_LBS_PER_WEEK:
            if recommendation is not None and recommendation.action == "hold":
                reason = (
                    "the evidence is not strong enough to justify a change"
                    if recommendation.assessment == "insufficient_data"
                    else "the recommendation is held"
                )
                notes.append(
                    f"Measured rate ({measured:+.2f} lbs/week) is "
                    f"{abs(difference):.2f} lbs/week away from the goal "
                    f"({goal.rate_lbs_per_week:+.2f}), but {reason}. Log intake and "
                    "weigh-ins more densely, then re-run."
                )
            else:
                notes.append(
                    f"Measured rate ({measured:+.2f} lbs/week) is "
                    f"{abs(difference):.2f} lbs/week away from the goal "
                    f"({goal.rate_lbs_per_week:+.2f}). Run 'llmn tdee targets' for the "
                    "bounded intake recommendation."
                )
    elif measured is not None and goal is None:
        notes.append("No dated goal is set; rates are reported without a target.")

    if measured is not None and measured < -2.0:
        notes.append("Warning: losing more than 2 lbs/week. This pace may not be sustainable.")

    if (
        measured is not None
        and goal is not None
        and goal.mode == "loss"
        and goal.target_weight_lbs is not None
        and weight_report.current_trend > goal.target_weight_lbs
        and measured > 0
    ):
        notes.append("Weight is trending up while above the loss goal weight.")

    return notes


def format_weight_report(report: WeightReport) -> str:
    """Format the EMA-based weight report as text."""
    direction = "lost" if report.trend_change < 0 else "gained"
    rate_dir = "losing" if report.weekly_rate < 0 else "gaining"

    lines = [
        f"Weight Tracking Report (last {report.period_days} days)",
        "=" * 45,
        f"Current weight: {report.current_weight:.1f} lbs",
        f"Current trend:  {report.current_trend:.1f} lbs (EMA)",
        f"Trend change:   {abs(report.trend_change):.1f} lbs {direction} "
        f"(from {report.start_trend:.1f})",
        f"EMA rate:       {abs(report.weekly_rate):.1f} lbs/week ({rate_dir}); "
        "smoothed trend endpoints",
    ]

    return "\n".join(lines)


def _baseline_label(report: TDEEReport) -> str:
    if report.baseline_source == "measured_rmr":
        return f"measured RMR {report.resting_kcal:.0f}"
    return f"Mifflin-St Jeor {report.resting_kcal:.0f}"


def format_tdee_report(report: TDEEReport) -> str:
    """Format the expenditure section as text."""
    lines = [
        "",
        f"Energy expenditure ({report.method})",
        "=" * 55,
        f"Baseline:        {report.baseline_kcal:.0f} kcal/day "
        f"({_baseline_label(report)} at {report.calculation_weight_lbs:.1f} lbs, "
        f"{report.activity_level})",
    ]
    if report.tdee_bias is not None:
        lines.append(f"Learned offset:  {report.tdee_bias:+.0f} kcal/day")
    if report.adjusted_tdee is not None:
        uncertainty = (
            f" ± {report.uncertainty_95ci:.0f}" if report.uncertainty_95ci is not None else ""
        )
        lines.append(f"Estimated TDEE:  {report.adjusted_tdee:.0f}{uncertainty} kcal/day (95%)")
    lines.append(
        f"Segments used:   {report.segments_used} weekly segment(s); confidence {report.confidence}"
    )

    lines.append("")
    lines.append("Actual intake (same window)")
    lines.append("-" * 45)
    if report.avg_actual_calories is not None:
        lines.append(
            f"Mean:            {report.avg_actual_calories:.0f} kcal/day over "
            f"{report.complete_intake_days} logged complete day(s)"
        )
    else:
        lines.append("Mean:            none (no complete actual-intake days)")
    if report.estimated_intake_days:
        lines.append(
            f"Estimated days:  {report.estimated_intake_days} "
            "(half weight in the mean and coverage)"
        )
    lines.append(
        f"Coverage:        {report.weighted_coverage:.0%} of {report.window_days} calendar day(s)"
    )
    if report.expected_deficit is not None:
        lines.append(
            f"Expected deficit: {report.expected_deficit:.0f} kcal/day "
            "(estimated TDEE - mean logged intake)"
        )
    if report.implied_deficit_measured is not None:
        lines.append(
            f"Implied deficit:  {report.implied_deficit_measured:.0f} kcal/day "
            "(from measured weight rate)"
        )
    if report.reason_codes:
        lines.append(f"Reasons:         {', '.join(report.reason_codes)}")

    return "\n".join(lines)


def format_progress_report(report: ProgressReport) -> str:
    """Format the full progress report as text."""
    window = report.window
    parts = [
        f"Progress report ({window.start} to {window.end}, {window.total_days} calendar days)",
        "=" * 55,
    ]

    parts.append("")
    parts.append("Weight trend")
    parts.append("-" * 55)
    weight = report.weight
    direction = "lost" if weight.trend_change < 0 else "gained"
    parts.append(f"  Current weight:  {weight.current_weight:.1f} lbs")
    parts.append(f"  Current trend:   {weight.current_trend:.1f} lbs (EMA)")
    parts.append(
        f"  Trend change:    {abs(weight.trend_change):.2f} lbs {direction} "
        f"since {weight.start_trend:.2f} (EMA endpoints)"
    )
    parts.append(
        f"  EMA rate:        {weight.weekly_rate:+.2f} lbs/week (smoothed; lags short windows)"
    )
    if report.tdee.measured_rate_lbs_per_week is not None:
        parts.append(
            f"  Measured rate:   {report.tdee.measured_rate_lbs_per_week:+.2f} lbs/week "
            "(least-squares over raw weights; used by recommendations)"
        )

    parts.append(format_tdee_report(report.tdee))

    if report.goal is not None:
        parts.append("")
        target = (
            f"target {report.goal_weight:.0f} lbs"
            if report.goal_weight is not None
            else "no target weight"
        )
        parts.append(
            f"Goal ({report.goal.mode} at {report.goal.rate_lbs_per_week:+.2f} lbs/week, {target})"
        )
        parts.append("-" * 55)
        if report.remaining_lbs is not None:
            side = "above" if report.remaining_lbs > 0 else "below"
            parts.append(f"  Remaining to target: {abs(report.remaining_lbs):.1f} lbs {side}")
        if report.target_kcal_per_day is not None:
            parts.append(
                f"  Recommended intake: {report.target_kcal_per_day} kcal/day "
                f"({report.target_basis or 'unspecified basis'})"
            )
        if report.expenditure_target_kcal is not None:
            parts.append(
                f"  Energy-balance cross-check: {report.expenditure_target_kcal} kcal/day "
                "(estimated TDEE + goal rate)"
            )
        if report.weeks_to_goal is not None:
            parts.append(f"  At the measured rate: ~{report.weeks_to_goal:.0f} weeks")
        elif report.goal_weight is not None:
            parts.append(
                "  At the measured rate: no projection (rate is flat or moving away "
                "from the target)"
            )

    if report.latest_composition is not None:
        parts.extend(_format_body_comp(report))

    if report.notes:
        parts.append("")
        parts.append("Notes:")
        for note in report.notes:
            parts.append(f"  - {note}")

    return "\n".join(parts)


def _format_body_comp(report: ProgressReport) -> list[str]:
    """Format the body-composition section of a progress report."""
    latest = report.latest_composition
    assert latest is not None
    parts = ["", "Body composition", "-" * 55]
    values = [f"{latest.measured_at} ({latest.method})"]
    if latest.weight_lbs is not None:
        values.append(f"{latest.weight_lbs:.1f} lbs")
    if latest.derived_body_fat_pct is not None:
        values.append(f"{latest.derived_body_fat_pct:.1f}% fat")
    if latest.derived_fat_mass_lbs is not None:
        values.append(f"{latest.derived_fat_mass_lbs:.1f} lbs fat mass")
    if latest.derived_fat_free_mass_lbs is not None:
        values.append(f"{latest.derived_fat_free_mass_lbs:.1f} lbs fat-free mass")
    if latest.waist_inches is not None:
        values.append(f"{latest.waist_inches:.1f} in waist")
    parts.append("  Latest: " + ", ".join(values))

    if report.body_comp_delta is not None:
        delta = report.body_comp_delta
        parts.append(f"  Classification: {delta.classification}")
        parts.append(f"  {delta.summary}")
        for warning in delta.warnings:
            parts.append(f"  Warning: {warning}")
    else:
        parts.append(
            "  Only one comparable measurement; a second scan is needed to separate "
            "fat change from lean change."
        )
    return parts
