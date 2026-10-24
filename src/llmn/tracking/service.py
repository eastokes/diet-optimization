"""Application service for standalone calorie analysis and recommendations.

Dependency direction: CLI -> service -> queries/calculation helpers. The service
never imports meal optimization, food loading, or recipe code.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from llmn.tracking import policy
from llmn.tracking.errors import TrackingError
from llmn.tracking.estimation import (
    AnalysisWindow,
    ExpenditureEstimate,
    build_window,
    estimate_expenditure,
)
from llmn.tracking.models import TDEEEstimate, UserProfile
from llmn.tracking.queries import (
    ActivityQueries,
    GoalQueries,
    IntakeQueries,
    TDEEQueries,
    UserQueries,
    WeightQueries,
)
from llmn.tracking.recommendations import (
    GoalSpec,
    Recommendation,
    recommend,
)


@dataclass
class TrackingAnalysis:
    """A complete, serializable analysis result."""

    as_of: date
    window: AnalysisWindow
    profile: UserProfile
    goal: GoalSpec | None
    estimate: ExpenditureEstimate
    recommendation: Recommendation
    reason_codes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def assessment(self) -> str:
        return self.recommendation.assessment

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "as_of": self.as_of.isoformat(),
            "policy_version": policy.POLICY_VERSION,
            "analysis_window": self.window.to_dict(),
            "goal": self.goal.to_dict() if self.goal else None,
            "assessment": self.assessment,
            "reason_codes": self.reason_codes,
            "evidence": self.recommendation.evidence,
            "expenditure": self.estimate.to_dict(),
            "observed_rate_lbs_per_week": (
                round(self.estimate.observed_rate_lbs_per_week, 2)
                if self.estimate.observed_rate_lbs_per_week is not None
                else None
            ),
            "intake": self.estimate.intake.to_dict(),
            "weight": self.estimate.weights.to_dict(self.window),
            "recommendation": self.recommendation.to_dict(),
            "human_summary": self.recommendation.recommendation_text,
        }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def get_profile(conn: sqlite3.Connection, user_id: int | None) -> UserProfile:
    profile = (
        UserQueries.get_user(conn, user_id)
        if user_id is not None
        else UserQueries.get_default_user(conn)
    )
    if profile is None:
        raise TrackingError(
            "NO_USER_PROFILE",
            "No user profile found.",
            ["Create one with: llmn user create --age 35 --sex male --height 70 --weight 180"],
        )
    return profile


def resolve_goal(
    conn: sqlite3.Connection,
    profile: UserProfile,
    as_of: date,
) -> GoalSpec | None:
    """Resolve a dated goal period, falling back to the legacy profile field."""
    if profile.user_id is None:
        return None

    period = GoalQueries.get_effective_goal(conn, profile.user_id, as_of)
    if period is not None:
        return GoalSpec(
            mode=period.mode,
            rate_lbs_per_week=period.rate_lbs_per_week,
            target_weight_lbs=period.target_weight_lbs,
            effective_from=period.effective_from,
            source="goal_period",
            training_priority=period.training_priority,
            notes=period.notes,
        )

    return goal_from_profile(profile)


def goal_from_profile(profile: UserProfile) -> GoalSpec | None:
    """Translate the old ``goal`` string without pretending it was dated."""
    raw = (profile.goal or "").strip().lower()
    mode: str | None = None
    rate: float | None = None
    if raw.startswith("fat_loss"):
        mode, rate = "loss", -1.0
    elif raw.startswith("aggressive"):
        mode, rate = "loss", -1.5
    elif raw.startswith("lean_gain"):
        mode, rate = "gain", 0.5
    elif raw.startswith("muscle_gain"):
        mode, rate = "gain", 1.0
    elif raw.startswith("maintenance"):
        mode, rate = "maintenance", 0.0

    if mode is None or rate is None:
        return None

    return GoalSpec(
        mode=mode,
        rate_lbs_per_week=rate,
        target_weight_lbs=profile.target_weight_lbs,
        effective_from=None,
        source="profile_legacy",
    )


def resolve_activity(
    conn: sqlite3.Connection,
    profile: UserProfile,
    as_of: date,
) -> str:
    if profile.user_id is None:
        return profile.activity_level
    period = ActivityQueries.get_effective_activity(conn, profile.user_id, as_of)
    return period.activity_level if period is not None else profile.activity_level


def analyze(
    conn: sqlite3.Connection,
    user_id: int | None,
    as_of: date,
    window_days: int = policy.DEFAULT_WINDOW_DAYS,
    goal: GoalSpec | None = None,
) -> TrackingAnalysis:
    """Analyze current inputs on an explicit date without writing anything."""
    profile = get_profile(conn, user_id)
    if profile.user_id is None:
        raise TrackingError("NO_USER_PROFILE", "User profile has no ID.")

    window = build_window(as_of, window_days)
    weight_history = WeightQueries.get_weight_history(conn, profile.user_id, end_date=as_of)
    intake_entries = IntakeQueries.get_entries(
        conn,
        profile.user_id,
        start_date=window.start,
        end_date=window.end,
    )
    activities = ActivityQueries.get_history(conn, profile.user_id, as_of)
    resolved_goal = goal if goal is not None else resolve_goal(conn, profile, as_of)

    estimate = estimate_expenditure(
        profile=profile,
        weight_history=weight_history,
        intake_entries=intake_entries,
        activities=activities,
        fallback_activity=profile.activity_level,
        window=window,
        as_of=as_of,
    )
    recommendation = recommend(estimate, resolved_goal, profile, as_of)

    reasons = _dedupe(estimate.reason_codes + recommendation.reason_codes)
    warnings = _dedupe(estimate.warnings + recommendation.warnings)

    return TrackingAnalysis(
        as_of=as_of,
        window=window,
        profile=profile,
        goal=resolved_goal,
        estimate=estimate,
        recommendation=recommendation,
        reason_codes=reasons,
        warnings=warnings,
    )


def persist_estimate(conn: sqlite3.Connection, analysis: TrackingAnalysis) -> int:
    """Persist an estimate snapshot for the explicit analysis date."""
    estimate = analysis.estimate
    if estimate.kcal_per_day is None:
        raise TrackingError(
            "NO_EXPENDITURE_ESTIMATE",
            "No expenditure estimate is available to persist.",
        )

    bias = estimate.bias if estimate.bias is not None else 0.0
    variance = (
        estimate.variance if estimate.variance is not None else policy.FORMULA_UNCERTAINTY_KCAL**2
    )
    snapshot = TDEEEstimate(
        estimate_id=None,
        user_id=analysis.profile.user_id or 0,
        estimated_at=analysis.as_of,
        mifflin_tdee=estimate.mifflin_tdee,
        tdee_bias=bias,
        variance=variance,
        adjusted_tdee=estimate.kcal_per_day,
        method=estimate.method,
        policy_version=policy.POLICY_VERSION,
        interval_start=analysis.window.start,
        interval_end=analysis.window.end,
        weight_observations=estimate.weights.observations,
        complete_intake_days=estimate.intake.complete_days,
        partial_intake_days=estimate.intake.partial_days,
        input_fingerprint=estimate.fingerprint,
    )
    return TDEEQueries.save_estimate(conn, snapshot)


def analyze_and_persist(
    conn: sqlite3.Connection,
    user_id: int | None,
    as_of: date,
    window_days: int = policy.DEFAULT_WINDOW_DAYS,
) -> TrackingAnalysis:
    analysis = analyze(conn, user_id, as_of, window_days)
    persist_estimate(conn, analysis)
    return analysis


def current_calculation_weight(
    conn: sqlite3.Connection,
    user_id: int,
    as_of: date,
    max_age_days: int = policy.STALE_CURRENT_WEIGHT_DAYS,
) -> tuple[float, str, date | None]:
    """Return the weight to use for current formulas and its provenance.

    A recent observation wins; otherwise the profile value is the documented
    fallback. A backfilled older weight never overwrites the profile.
    """
    profile = UserQueries.get_user(conn, user_id)
    if profile is None:
        raise TrackingError("NO_USER_PROFILE", "No user profile found.")

    latest = WeightQueries.get_latest_weight_on_or_before(conn, user_id, as_of)
    if latest is not None:
        age = (as_of - latest.measured_at).days
        if age <= max_age_days:
            return latest.trend_lbs, "recent_trend", latest.measured_at
        return profile.weight_lbs, "profile_fallback", latest.measured_at
    return profile.weight_lbs, "profile_fallback", None


def finite_or_raise(value: float, field_name: str) -> float:
    """Reject non-finite numeric input at the service boundary."""
    if not math.isfinite(value):
        raise TrackingError(
            "NONFINITE_INPUT",
            f"{field_name} must be a finite number.",
        )
    return value
