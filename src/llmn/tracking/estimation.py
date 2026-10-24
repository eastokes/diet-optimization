"""Calendar-aware intake coverage and expenditure estimation.

The estimator deliberately separates raw observations from derived values.
Missing intake dates stay unknown, partial days are excluded from measured
intake, and a formula-only fallback is labelled as such.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from llmn.tracking import policy
from llmn.tracking.ema import calculate_trend_from_scratch
from llmn.tracking.formula import mifflin_tdee_at_weight
from llmn.tracking.models import ActivityPeriod, IntakeEntry, UserProfile, WeightEntry
from llmn.tracking.tdee_filter import TDEEFilter


@dataclass(frozen=True)
class AnalysisWindow:
    """Inclusive calendar interval analyzed by the service."""

    start: date
    end: date

    @property
    def total_days(self) -> int:
        return max(1, (self.end - self.start).days + 1)

    def contains(self, value: date) -> bool:
        return self.start <= value <= self.end

    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.total_days,
        }


@dataclass
class IntakeSummary:
    """Coverage-aware summary of actual intake in an interval."""

    mean_kcal: float | None
    complete_days: int
    partial_days: int
    estimated_days: int
    unknown_days: int
    weighted_coverage: float
    latest_date: date | None
    legacy_unclassified_days: int
    window_days: int = 0
    measured_equivalent_days: float = 0.0

    @property
    def coverage_explanation(self) -> str:
        """Plain-language statement of how coverage was computed."""
        if not self.window_days:
            return "Coverage needs a calendar window."
        return (
            f"{self.measured_equivalent_days:.1f} measured-equivalent logged day(s) "
            f"/ {self.window_days} calendar day(s); estimated days count half weight, "
            "partial days count for neither the mean nor the weight"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "mean_kcal_per_day": _round(self.mean_kcal, 0),
            "mean_kcal_basis": "mean of logged complete actual-intake days",
            "complete_days": self.complete_days,
            "partial_days": self.partial_days,
            "estimated_days": self.estimated_days,
            "unknown_days": self.unknown_days,
            "weighted_coverage": round(self.weighted_coverage, 3),
            "measured_equivalent_days": round(self.measured_equivalent_days, 2),
            "window_days": self.window_days,
            "coverage_explanation": self.coverage_explanation,
            "latest_intake_date": self.latest_date.isoformat() if self.latest_date else None,
            "legacy_unclassified_days": self.legacy_unclassified_days,
        }


@dataclass
class WeightSeries:
    """Raw measurements plus rebuilt, gap-aware trend values."""

    entries: list[WeightEntry]
    latest_entry: WeightEntry | None
    trend_by_date: dict[date, float]
    as_of: date

    @property
    def observations(self) -> int:
        return len(self.entries)

    @property
    def span_days(self) -> int:
        if len(self.entries) < 2:
            return 0
        return (self.entries[-1].measured_at - self.entries[0].measured_at).days

    @property
    def latest_age_days(self) -> int | None:
        if self.latest_entry is None:
            return None
        return (self.as_of - self.latest_entry.measured_at).days

    @property
    def current_trend(self) -> float | None:
        if self.latest_entry is None:
            return None
        return self.trend_by_date.get(self.latest_entry.measured_at, self.latest_entry.trend_lbs)

    @property
    def observed_rate_lbs_per_week(self) -> float | None:
        slope = weight_slope_lbs_per_day(self.entries)
        return slope * 7.0 if slope is not None else None

    def to_dict(self, window: AnalysisWindow) -> dict[str, object]:
        return {
            "observations": self.observations,
            "span_days": self.span_days,
            "first_weight_date": (
                self.entries[0].measured_at.isoformat() if self.entries else None
            ),
            "latest_weight_date": (
                self.latest_entry.measured_at.isoformat() if self.latest_entry else None
            ),
            "latest_weight_age_days": self.latest_age_days,
            "current_trend_lbs": _round(self.current_trend, 2),
            "observed_rate_lbs_per_week": _round(self.observed_rate_lbs_per_week, 2),
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
        }


@dataclass
class ExpenditureEstimate:
    """Result of expenditure estimation with explicit evidence state."""

    method: str  # learned | formula_only
    kcal_per_day: float | None
    mifflin_tdee: float
    bias: float | None
    variance: float | None
    uncertainty_95ci: float | None
    segments_used: int
    window: AnalysisWindow
    intake: IntakeSummary
    weights: WeightSeries
    observed_rate_lbs_per_week: float | None
    calculation_weight_lbs: float
    calculation_weight_source: str
    activity_level: str
    reason_codes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fingerprint: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "kcal_per_day": _round(self.kcal_per_day, 0),
            "mifflin_tdee": _round(self.mifflin_tdee, 0),
            "bias": _round(self.bias, 0),
            "variance": _round(self.variance, 1),
            "uncertainty_95ci": _round(self.uncertainty_95ci, 0),
            "segments_used": self.segments_used,
            "calculation_weight_lbs": round(self.calculation_weight_lbs, 1),
            "calculation_weight_source": self.calculation_weight_source,
            "activity_level": self.activity_level,
            "reason_codes": list(self.reason_codes),
        }


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def build_window(as_of: date, window_days: int) -> AnalysisWindow:
    days = max(2, window_days)
    return AnalysisWindow(start=as_of - timedelta(days=days - 1), end=as_of)


def weight_slope_lbs_per_day(entries: list[WeightEntry]) -> float | None:
    """Least-squares slope of raw weights over actual elapsed dates.

    Using raw values avoids the EMA's initialization lag biasing a short first
    window. The stored EMA remains the display/trend value.
    """
    if len(entries) < 2:
        return None
    first_date = entries[0].measured_at
    xs = [float((entry.measured_at - first_date).days) for entry in entries]
    ys = [entry.weight_lbs for entry in entries]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return numerator / denominator


def summarize_intake(
    entries: list[IntakeEntry],
    window: AnalysisWindow,
) -> IntakeSummary:
    """Summarize actual intake without converting missing days to zero."""
    actual = [entry for entry in entries if entry.kind == "actual" and window.contains(entry.date)]

    weighted_total = 0.0
    weight_total = 0.0
    complete_days = 0
    partial_days = 0
    estimated_days = 0
    latest: date | None = None

    for entry in actual:
        latest = entry.date if latest is None else max(latest, entry.date)
        if entry.completeness == "partial":
            partial_days += 1
            continue
        if entry.completeness != "complete":
            continue

        complete_days += 1
        if entry.quality == "measured":
            weight = 1.0
        elif entry.quality == "estimated":
            estimated_days += 1
            weight = 0.5
        else:
            continue

        weighted_total += entry.kcal * weight
        weight_total += weight

    mean = weighted_total / weight_total if weight_total else None
    total_days = window.total_days
    covered = min(total_days, complete_days)
    weighted_coverage = weight_total / total_days if total_days else 0.0

    legacy_days = sum(
        1
        for entry in entries
        if entry.kind == "legacy_unclassified" and window.contains(entry.date)
    )

    return IntakeSummary(
        mean_kcal=mean,
        complete_days=complete_days,
        partial_days=partial_days,
        estimated_days=estimated_days,
        unknown_days=max(0, total_days - covered - partial_days),
        weighted_coverage=weighted_coverage,
        latest_date=latest,
        legacy_unclassified_days=legacy_days,
        window_days=total_days,
        measured_equivalent_days=weight_total,
    )


def build_weight_series(
    history: list[WeightEntry],
    window: AnalysisWindow,
    as_of: date,
) -> WeightSeries:
    """Rebuild gap-aware trends from all history and select window entries."""
    ordered = sorted(history, key=lambda entry: entry.measured_at)
    dated = [(entry.measured_at, entry.weight_lbs) for entry in ordered]
    trends = calculate_trend_from_scratch(dated)
    trend_by_date = {entry.measured_at: trend for entry, trend in zip(ordered, trends)}

    entries = [entry for entry in ordered if window.contains(entry.measured_at)]
    latest_entry = ordered[-1] if ordered else None

    return WeightSeries(
        entries=entries,
        latest_entry=latest_entry,
        trend_by_date=trend_by_date,
        as_of=as_of,
    )


def _resolve_activity_level(
    activities: list[ActivityPeriod],
    fallback: str,
    on_date: date,
) -> str:
    effective = [period for period in activities if period.effective_from <= on_date]
    if not effective:
        return fallback
    return max(effective, key=lambda period: period.effective_from).activity_level


def _fingerprint(
    profile: UserProfile,
    window: AnalysisWindow,
    weights: WeightSeries,
    intake_entries: list[IntakeEntry],
    activities: list[ActivityPeriod],
) -> str:
    payload = {
        "policy": policy.POLICY_VERSION,
        "window": [window.start.isoformat(), window.end.isoformat()],
        "profile": [
            profile.user_id,
            profile.age,
            profile.sex,
            profile.height_inches,
            profile.activity_level,
        ],
        "weights": [[entry.measured_at.isoformat(), entry.weight_lbs] for entry in weights.entries]
        + (
            [
                [
                    weights.latest_entry.measured_at.isoformat(),
                    weights.latest_entry.weight_lbs,
                ]
            ]
            if weights.latest_entry and weights.latest_entry.measured_at < window.start
            else []
        ),
        "intake": [
            [
                entry.date.isoformat(),
                entry.kcal,
                entry.kind,
                entry.completeness,
                entry.quality,
                entry.revision,
            ]
            for entry in intake_entries
        ],
        "activity": [
            [period.effective_from.isoformat(), period.activity_level] for period in activities
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _formula_estimate(
    profile: UserProfile,
    weight_history: list[WeightEntry],
    intake_entries: list[IntakeEntry],
    activities: list[ActivityPeriod],
    fallback_activity: str,
    window: AnalysisWindow,
    as_of: date,
    reasons: list[str],
    warnings: list[str],
) -> ExpenditureEstimate:
    weights = build_weight_series(weight_history, window, as_of)
    intake = summarize_intake(intake_entries, window)
    activity_level = _resolve_activity_level(activities, fallback_activity, as_of)

    latest = weights.latest_entry
    latest_age = weights.latest_age_days
    if (
        latest is not None
        and latest_age is not None
        and latest_age <= policy.STALE_CURRENT_WEIGHT_DAYS
    ):
        calculation_weight = weights.current_trend or latest.trend_lbs
        weight_source = "recent_trend"
    else:
        calculation_weight = profile.weight_lbs
        weight_source = "profile_fallback"
        if "FORMULA_WEIGHT_FALLBACK" not in reasons:
            reasons.append("FORMULA_WEIGHT_FALLBACK")

    mifflin = mifflin_tdee_at_weight(profile, calculation_weight, activity_level)

    return ExpenditureEstimate(
        method="formula_only",
        kcal_per_day=mifflin,
        mifflin_tdee=mifflin,
        bias=None,
        variance=None,
        uncertainty_95ci=policy.FORMULA_UNCERTAINTY_95CI,
        segments_used=0,
        window=window,
        intake=intake,
        weights=weights,
        observed_rate_lbs_per_week=weights.observed_rate_lbs_per_week,
        calculation_weight_lbs=calculation_weight,
        calculation_weight_source=weight_source,
        activity_level=activity_level,
        reason_codes=reasons,
        warnings=warnings,
        fingerprint=_fingerprint(profile, window, weights, intake_entries, activities),
    )


def estimate_expenditure(
    profile: UserProfile,
    weight_history: list[WeightEntry],
    intake_entries: list[IntakeEntry],
    activities: list[ActivityPeriod],
    fallback_activity: str,
    window: AnalysisWindow,
    as_of: date,
) -> ExpenditureEstimate:
    """Estimate expenditure on an explicit calendar window.

    Returns a formula-only estimate, never a fabricated adaptive one, when the
    evidence gates fail.
    """
    weights = build_weight_series(weight_history, window, as_of)
    intake = summarize_intake(intake_entries, window)
    reasons: list[str] = []
    warnings: list[str] = []

    if weights.latest_entry is None:
        reasons.append("NO_WEIGHT_OBSERVATIONS")
    else:
        latest_age = weights.latest_age_days or 0
        if latest_age > policy.MAX_WEIGHT_AGE_DAYS:
            reasons.append("STALE_WEIGHT_DATA")

    if weights.observations < policy.MIN_WEIGHT_OBSERVATIONS:
        reasons.append("FEW_WEIGHT_OBSERVATIONS")
    if weights.span_days < policy.MIN_ANALYSIS_SPAN_DAYS:
        reasons.append("SHORT_ANALYSIS_SPAN")
    if intake.mean_kcal is None:
        reasons.append("NO_ACTUAL_INTAKE")
    if intake.complete_days < policy.MIN_COMPLETE_INTAKE_DAYS:
        reasons.append("INSUFFICIENT_INTAKE_HISTORY")
    if intake.weighted_coverage < policy.MIN_INTAKE_COVERAGE:
        reasons.append("LOW_INTAKE_COVERAGE")
    if intake.legacy_unclassified_days:
        reasons.append("LEGACY_INTAKE_UNCLASSIFIED")
        warnings.append(
            "Legacy calorie rows are unclassified and were excluded from intake learning. "
            "Use 'llmn intake confirm-legacy' after reviewing them."
        )
    if intake.partial_days:
        warnings.append(
            f"{intake.partial_days} partial intake day(s) were excluded from the measured mean."
        )
    if intake.estimated_days:
        warnings.append(f"{intake.estimated_days} estimated intake day(s) were down-weighted.")

    if reasons:
        return _formula_estimate(
            profile,
            weight_history,
            intake_entries,
            activities,
            fallback_activity,
            window,
            as_of,
            reasons,
            warnings,
        )

    calculate_weight = weights.current_trend or profile.weight_lbs
    current_activity = _resolve_activity_level(activities, fallback_activity, as_of)
    mifflin_current = mifflin_tdee_at_weight(profile, calculate_weight, current_activity)

    tdee_filter = TDEEFilter()
    segments_used = 0
    activity_levels: set[str] = set()

    segment_start = window.start
    while segment_start <= window.end:
        segment_end = min(segment_start + timedelta(days=6), window.end)
        segment_weights = [
            entry for entry in weights.entries if segment_start <= entry.measured_at <= segment_end
        ]

        if len(segment_weights) >= 2:
            first = segment_weights[0]
            last = segment_weights[-1]
            elapsed_days = (last.measured_at - first.measured_at).days
            if elapsed_days >= policy.MIN_SEGMENT_WEIGHT_SPAN_DAYS:
                segment_intake = summarize_intake(
                    intake_entries,
                    AnalysisWindow(first.measured_at, last.measured_at),
                )
                if (
                    segment_intake.mean_kcal is not None
                    and segment_intake.weighted_coverage >= policy.MIN_SEGMENT_INTAKE_COVERAGE
                ):
                    seg_activity = _resolve_activity_level(
                        activities, fallback_activity, first.measured_at
                    )
                    trend_start = weights.trend_by_date.get(first.measured_at, first.trend_lbs)
                    segment_mifflin = mifflin_tdee_at_weight(profile, trend_start, seg_activity)
                    segment_slope = weight_slope_lbs_per_day(segment_weights)
                    if segment_slope is None:
                        segment_start = segment_end + timedelta(days=1)
                        continue
                    # Positive slope means gain; a deficit is the negative.
                    implied_deficit = -segment_slope * 3500.0
                    expected_deficit = segment_mifflin - segment_intake.mean_kcal
                    tdee_filter.predict_and_update(
                        implied_deficit=implied_deficit,
                        expected_deficit=expected_deficit,
                        days=elapsed_days,
                        calorie_coverage=segment_intake.weighted_coverage,
                    )
                    activity_levels.add(seg_activity)
                    segments_used += 1

        segment_start = segment_end + timedelta(days=1)

    if segments_used == 0:
        reasons.append("NO_VALID_SEGMENTS")
        return _formula_estimate(
            profile,
            weight_history,
            intake_entries,
            activities,
            fallback_activity,
            window,
            as_of,
            reasons,
            warnings,
        )

    variance = tdee_filter.variance
    if len(activity_levels) > 1:
        variance += policy.ACTIVITY_CHANGE_VARIANCE
        reasons.append("ACTIVITY_CHANGED")
        warnings.append(
            "Activity level changed during the analysis window; uncertainty was widened."
        )

    adjusted = mifflin_current + tdee_filter.bias
    if adjusted < 0.5 * mifflin_current or adjusted > 2.0 * mifflin_current:
        reasons.append("IMPLAUSIBLE_ESTIMATE")
        warnings.append(
            "The learned expenditure is implausible relative to the formula baseline; "
            "recommendations will be held."
        )

    uncertainty = 1.96 * math.sqrt(variance)

    return ExpenditureEstimate(
        method="learned",
        kcal_per_day=adjusted,
        mifflin_tdee=mifflin_current,
        bias=tdee_filter.bias,
        variance=variance,
        uncertainty_95ci=uncertainty,
        segments_used=segments_used,
        window=window,
        intake=intake,
        weights=weights,
        observed_rate_lbs_per_week=weights.observed_rate_lbs_per_week,
        calculation_weight_lbs=calculate_weight,
        calculation_weight_source="recent_trend",
        activity_level=current_activity,
        reason_codes=reasons,
        warnings=warnings,
        fingerprint=_fingerprint(profile, window, weights, intake_entries, activities),
    )
