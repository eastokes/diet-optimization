"""Pure recommendation policy for standalone calorie targets."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from llmn.tracking import policy
from llmn.tracking.errors import GoalValidationError
from llmn.tracking.estimation import ExpenditureEstimate
from llmn.tracking.models import UserProfile


@dataclass(frozen=True)
class GoalSpec:
    """A resolved goal effective on the analysis date."""

    mode: str
    rate_lbs_per_week: float
    target_weight_lbs: float | None = None
    effective_from: date | None = None
    source: str = "goal_period"
    activity_level: str | None = None
    training_priority: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "rate_lbs_per_week": self.rate_lbs_per_week,
            "target_weight_lbs": self.target_weight_lbs,
            "effective_from": (self.effective_from.isoformat() if self.effective_from else None),
            "source": self.source,
            "activity_level": self.activity_level,
            "training_priority": self.training_priority,
        }


@dataclass
class Recommendation:
    """A bounded recommendation or an explicit abstention."""

    assessment: str
    action: str
    target_kcal_per_day: int | None
    change_kcal_per_day: int | None
    reason_codes: list[str]
    warnings: list[str]
    recommendation_text: str
    next_review_date: date | None
    protein_min_g: int | None
    protein_max_g: int | None
    observed_rate_lbs_per_week: float | None = None
    goal_rate_lbs_per_week: float | None = None
    candidate_target_kcal_per_day: int | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    # How the target was derived. ``rate_correction`` adjusts the measured mean
    # intake by the observed-vs-goal rate gap; it does not re-anchor on the
    # expenditure estimate, whose level is less certain than the measured gap.
    target_basis: str | None = None
    # Energy-balance cross-check: estimated TDEE plus the goal rate.
    expenditure_target_kcal: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "assessment": self.assessment,
            "action": self.action,
            "target_kcal_per_day": self.target_kcal_per_day,
            "change_kcal_per_day": self.change_kcal_per_day,
            "candidate_target_kcal_per_day": self.candidate_target_kcal_per_day,
            "reason_codes": self.reason_codes,
            "warnings": self.warnings,
            "recommendation_text": self.recommendation_text,
            "next_review_date": (
                self.next_review_date.isoformat() if self.next_review_date else None
            ),
            "protein_min_g": self.protein_min_g,
            "protein_max_g": self.protein_max_g,
            "observed_rate_lbs_per_week": (
                round(self.observed_rate_lbs_per_week, 2)
                if self.observed_rate_lbs_per_week is not None
                else None
            ),
            "goal_rate_lbs_per_week": self.goal_rate_lbs_per_week,
            "target_basis": self.target_basis,
            "expenditure_target_kcal": self.expenditure_target_kcal,
            "confidence": self.evidence.get("confidence"),
            "evidence": self.evidence,
        }


def validate_goal(goal: GoalSpec) -> None:
    """Reject unsupported goal configurations before they become advice."""
    if goal.mode not in ("loss", "maintenance", "gain"):
        raise GoalValidationError(
            "INVALID_GOAL_MODE",
            f"Goal mode must be loss, maintenance, or gain, got '{goal.mode}'.",
        )
    if not math.isfinite(goal.rate_lbs_per_week):
        raise GoalValidationError("INVALID_GOAL_RATE", "Goal rate must be a finite number.")
    if goal.mode == "maintenance":
        if goal.rate_lbs_per_week != 0:
            raise GoalValidationError(
                "INVALID_GOAL_RATE",
                "Maintenance goals must use a rate of 0 lbs/week.",
            )
    elif goal.mode == "loss":
        if not (
            policy.MIN_LOSS_RATE_LBS_PER_WEEK
            <= goal.rate_lbs_per_week
            <= policy.MAX_LOSS_RATE_LBS_PER_WEEK
        ):
            raise GoalValidationError(
                "UNSUPPORTED_GOAL_RATE",
                (
                    "Loss rate must be between "
                    f"{abs(policy.MIN_LOSS_RATE_LBS_PER_WEEK):.2f} and "
                    f"{abs(policy.MAX_LOSS_RATE_LBS_PER_WEEK):.2f} lbs/week."
                ),
                ["Set a supported rate with: llmn goal set --mode loss --rate -1.0"],
            )
    else:
        if not (
            policy.MIN_GAIN_RATE_LBS_PER_WEEK
            <= goal.rate_lbs_per_week
            <= policy.MAX_GAIN_RATE_LBS_PER_WEEK
        ):
            raise GoalValidationError(
                "UNSUPPORTED_GOAL_RATE",
                (
                    "Gain rate must be between "
                    f"{policy.MIN_GAIN_RATE_LBS_PER_WEEK:.2f} and "
                    f"{policy.MAX_GAIN_RATE_LBS_PER_WEEK:.2f} lbs/week."
                ),
                ["Set a supported rate with: llmn goal set --mode gain --rate 0.5"],
            )

    if goal.target_weight_lbs is not None and (
        not math.isfinite(goal.target_weight_lbs) or goal.target_weight_lbs <= 0
    ):
        raise GoalValidationError(
            "INVALID_TARGET_WEIGHT",
            "Target weight must be a finite, positive number of pounds.",
        )


def _safety_floor(profile: UserProfile) -> int:
    return policy.MIN_CALORIES_MALE if profile.sex == "male" else policy.MIN_CALORIES_FEMALE


def _protein_range(weight_lbs: float, mode: str) -> tuple[int, int]:
    if mode == "loss":
        return int(weight_lbs * 0.8), int(weight_lbs * 1.0)
    return int(weight_lbs * 0.7), int(weight_lbs * 0.9)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _formula_target(expenditure_kcal: float, rate_lbs_per_week: float) -> float:
    """Target intake for a requested rate using the 3500 kcal/lb approximation."""
    return expenditure_kcal + rate_lbs_per_week * policy.KCAL_PER_LB_WEEK


def _target_reached(goal: GoalSpec, current_trend_lbs: float | None) -> bool:
    if goal.target_weight_lbs is None or current_trend_lbs is None:
        return False
    if goal.mode == "loss":
        return current_trend_lbs <= goal.target_weight_lbs
    if goal.mode == "gain":
        return current_trend_lbs >= goal.target_weight_lbs
    return False


def recommend(
    estimate: ExpenditureEstimate,
    goal: GoalSpec | None,
    profile: UserProfile,
    as_of: date,
) -> Recommendation:
    """Recommend a bounded intake target or abstain explicitly."""
    reasons = list(estimate.reason_codes)
    warnings = list(estimate.warnings)
    current_trend = estimate.weights.current_trend
    weight_for_protein = current_trend or estimate.calculation_weight_lbs

    if goal is None:
        protein_min, protein_max = _protein_range(weight_for_protein, "maintenance")
        return Recommendation(
            assessment="no_goal",
            action="set_goal",
            target_kcal_per_day=None,
            change_kcal_per_day=None,
            reason_codes=_dedupe(reasons + ["NO_GOAL_SET"]),
            warnings=warnings,
            recommendation_text=(
                "No goal is set. Set a dated loss, maintenance, or gain goal before "
                "expecting an adaptive calorie recommendation."
            ),
            next_review_date=None,
            protein_min_g=protein_min,
            protein_max_g=protein_max,
            observed_rate_lbs_per_week=estimate.observed_rate_lbs_per_week,
            goal_rate_lbs_per_week=None,
            evidence=_evidence(estimate),
        )

    validate_goal(goal)
    protein_min, protein_max = _protein_range(weight_for_protein, goal.mode)
    expenditure_target = _expenditure_target(estimate, goal)

    latest_weight_age = estimate.weights.latest_age_days
    if _target_reached(goal, current_trend) and (
        latest_weight_age is not None and latest_weight_age <= policy.MAX_WEIGHT_AGE_DAYS
    ):
        return Recommendation(
            assessment="target_reached",
            action="transition_to_maintenance",
            target_kcal_per_day=None,
            change_kcal_per_day=None,
            reason_codes=_dedupe(reasons + ["TARGET_REACHED"]),
            warnings=warnings,
            recommendation_text=(
                f"Your latest trend ({current_trend:.1f} lbs) has reached the goal "
                f"weight ({goal.target_weight_lbs:.1f} lbs). Set a maintenance goal "
                "before continuing to adjust intake."
            ),
            next_review_date=None,
            protein_min_g=protein_min,
            protein_max_g=protein_max,
            observed_rate_lbs_per_week=estimate.observed_rate_lbs_per_week,
            goal_rate_lbs_per_week=goal.rate_lbs_per_week,
            evidence=_evidence(estimate),
        )

    if estimate.method != "learned" or estimate.kcal_per_day is None:
        no_observations = (
            estimate.weights.latest_entry is None and estimate.intake.complete_days == 0
        )
        if no_observations:
            candidate = _formula_target(estimate.baseline_kcal, goal.rate_lbs_per_week)
            target = int(round(candidate))
            floor = _safety_floor(profile)
            if target < floor:
                target = floor
                reasons.append("SAFETY_FLOOR_APPLIED")
            warnings.append(
                "This is a formula-only starting target, not an adaptive "
                "recommendation. Log weight and actual intake for at least two weeks."
            )
            return Recommendation(
                assessment="insufficient_data",
                action="initialize_formula_only",
                target_kcal_per_day=target,
                change_kcal_per_day=None,
                reason_codes=_dedupe(reasons + ["FORMULA_ONLY_INITIALIZATION"]),
                warnings=warnings,
                recommendation_text=(
                    f"No measured history is available yet. A formula-only starting "
                    f"target of {target} kcal/day is provided for {goal.mode}; "
                    "it is not yet backed by your observed data."
                ),
                next_review_date=as_of + timedelta(days=policy.REVIEW_CADENCE_DAYS),
                protein_min_g=protein_min,
                protein_max_g=protein_max,
                observed_rate_lbs_per_week=None,
                goal_rate_lbs_per_week=goal.rate_lbs_per_week,
                candidate_target_kcal_per_day=target,
                evidence=_evidence(estimate),
                target_basis="formula_only",
                expenditure_target_kcal=expenditure_target,
            )

        return Recommendation(
            assessment="insufficient_data",
            action="hold",
            target_kcal_per_day=None,
            change_kcal_per_day=None,
            reason_codes=_dedupe(reasons + ["EVIDENCE_HOLD"]),
            warnings=warnings,
            recommendation_text=(
                "There is some history, but not enough complete actual-intake and "
                "weight evidence to justify changing intake. Keep logging and re-check "
                "after the review date."
            ),
            next_review_date=as_of + timedelta(days=policy.REVIEW_CADENCE_DAYS),
            protein_min_g=protein_min,
            protein_max_g=protein_max,
            observed_rate_lbs_per_week=estimate.observed_rate_lbs_per_week,
            goal_rate_lbs_per_week=goal.rate_lbs_per_week,
            evidence=_evidence(estimate),
        )

    if estimate.segments_used < policy.MIN_SEGMENTS_FOR_RECOMMENDATION:
        uncertainty = estimate.uncertainty_95ci or 0.0
        return Recommendation(
            assessment="insufficient_data",
            action="hold",
            target_kcal_per_day=None,
            change_kcal_per_day=None,
            reason_codes=_dedupe(reasons + ["SINGLE_SEGMENT_EVIDENCE"]),
            warnings=warnings,
            recommendation_text=(
                f"Only {estimate.segments_used} weekly segment(s) met the evidence "
                "requirements, so the estimate "
                f"({estimate.kcal_per_day:.0f} ± {uncertainty:.0f} kcal/day) is too "
                f"weakly supported to change intake. At least "
                f"{policy.MIN_SEGMENTS_FOR_RECOMMENDATION} valid segments are needed: "
                "log intake daily and weigh in most days, then re-run. No target is "
                "held against your current intake."
            ),
            next_review_date=as_of + timedelta(days=policy.REVIEW_CADENCE_DAYS),
            protein_min_g=protein_min,
            protein_max_g=protein_max,
            observed_rate_lbs_per_week=estimate.observed_rate_lbs_per_week,
            goal_rate_lbs_per_week=goal.rate_lbs_per_week,
            expenditure_target_kcal=expenditure_target,
            evidence=_evidence(estimate),
        )

    observed = estimate.observed_rate_lbs_per_week
    if observed is None:
        return Recommendation(
            assessment="insufficient_data",
            action="hold",
            target_kcal_per_day=None,
            change_kcal_per_day=None,
            reason_codes=_dedupe(reasons + ["MISSING_OBSERVED_RATE"]),
            warnings=warnings,
            recommendation_text=(
                "The analysis has no observed weight rate, so intake cannot be adapted safely."
            ),
            next_review_date=as_of + timedelta(days=policy.REVIEW_CADENCE_DAYS),
            protein_min_g=protein_min,
            protein_max_g=protein_max,
            observed_rate_lbs_per_week=None,
            goal_rate_lbs_per_week=goal.rate_lbs_per_week,
            evidence=_evidence(estimate),
        )

    difference = observed - goal.rate_lbs_per_week
    tolerance = policy.RATE_TOLERANCE_LBS_PER_WEEK
    evidence = _evidence(estimate)

    if abs(difference) <= tolerance:
        on_track_target = (
            int(round(estimate.intake.mean_kcal)) if estimate.intake.mean_kcal is not None else None
        )
        floor = _safety_floor(profile)
        if on_track_target is not None and on_track_target < floor:
            on_track_target = floor
            reasons.append("SAFETY_FLOOR_APPLIED")
            warnings.append(f"The measured mean intake is below the {floor} kcal/day safety floor.")
        return Recommendation(
            assessment="on_track",
            action="keep",
            target_kcal_per_day=on_track_target,
            change_kcal_per_day=0,
            reason_codes=_dedupe(reasons + ["ON_TRACK"]),
            warnings=warnings,
            recommendation_text=(
                f"Observed rate ({observed:+.2f} lbs/week) is within "
                f"{tolerance:.1f} lbs/week of the goal "
                f"({goal.rate_lbs_per_week:+.2f}). Keep the current intake."
            ),
            next_review_date=as_of + timedelta(days=policy.REVIEW_CADENCE_DAYS),
            protein_min_g=protein_min,
            protein_max_g=protein_max,
            observed_rate_lbs_per_week=observed,
            goal_rate_lbs_per_week=goal.rate_lbs_per_week,
            candidate_target_kcal_per_day=on_track_target,
            evidence=evidence,
            target_basis="measured_mean_intake",
            expenditure_target_kcal=expenditure_target,
        )

    assessment = "intake_above_goal" if difference > 0 else "intake_below_goal"
    if "IMPLAUSIBLE_ESTIMATE" in reasons:
        return Recommendation(
            assessment=assessment,
            action="hold",
            target_kcal_per_day=None,
            change_kcal_per_day=None,
            reason_codes=_dedupe(reasons + ["IMPLAUSIBLE_ESTIMATE"]),
            warnings=warnings,
            recommendation_text=(
                "The learned expenditure is implausible, so the recommendation is held."
            ),
            next_review_date=as_of + timedelta(days=policy.REVIEW_CADENCE_DAYS),
            protein_min_g=protein_min,
            protein_max_g=protein_max,
            observed_rate_lbs_per_week=observed,
            goal_rate_lbs_per_week=goal.rate_lbs_per_week,
            evidence=evidence,
        )

    uncertainty = estimate.uncertainty_95ci or 0.0
    if uncertainty > policy.MAX_RECOMMENDATION_UNCERTAINTY_95CI:
        return Recommendation(
            assessment=assessment,
            action="hold",
            target_kcal_per_day=None,
            change_kcal_per_day=None,
            reason_codes=_dedupe(reasons + ["HIGH_UNCERTAINTY"]),
            warnings=warnings,
            recommendation_text=(
                f"Uncertainty (±{uncertainty:.0f} kcal/day) is too high to justify "
                "a bounded intake change. Keep logging and re-check after the review date."
            ),
            next_review_date=as_of + timedelta(days=policy.REVIEW_CADENCE_DAYS),
            protein_min_g=protein_min,
            protein_max_g=protein_max,
            observed_rate_lbs_per_week=observed,
            goal_rate_lbs_per_week=goal.rate_lbs_per_week,
            evidence=evidence,
        )

    if estimate.intake.mean_kcal is None:
        return Recommendation(
            assessment=assessment,
            action="hold",
            target_kcal_per_day=None,
            change_kcal_per_day=None,
            reason_codes=_dedupe(reasons + ["MISSING_MEAN_INTAKE"]),
            warnings=warnings,
            recommendation_text=(
                "There is no measured mean intake to adjust from, so the recommendation is held."
            ),
            next_review_date=as_of + timedelta(days=policy.REVIEW_CADENCE_DAYS),
            protein_min_g=protein_min,
            protein_max_g=protein_max,
            observed_rate_lbs_per_week=observed,
            goal_rate_lbs_per_week=goal.rate_lbs_per_week,
            evidence=evidence,
        )

    raw_change = (goal.rate_lbs_per_week - observed) * policy.KCAL_PER_LB_WEEK
    bounded_change = max(
        -policy.MAX_TARGET_CHANGE_KCAL_PER_DAY,
        min(policy.MAX_TARGET_CHANGE_KCAL_PER_DAY, raw_change),
    )
    change = int(round(bounded_change))
    candidate = estimate.intake.mean_kcal + change
    target = int(round(candidate))
    floor = _safety_floor(profile)
    if target < floor:
        target = floor
        change = target - int(round(estimate.intake.mean_kcal))
        reasons.append("SAFETY_FLOOR_APPLIED")
        warnings.append(f"The bounded target was raised to the {floor} kcal/day safety floor.")
    if abs(change) < abs(raw_change):
        reasons.append("CHANGE_BOUNDED")

    action = "decrease_intake" if change < 0 else "increase_intake"
    direction = "reduce" if change < 0 else "increase"
    return Recommendation(
        assessment=assessment,
        action=action,
        target_kcal_per_day=target,
        change_kcal_per_day=change,
        reason_codes=_dedupe(reasons + [assessment.upper()]),
        warnings=warnings,
        recommendation_text=(
            f"Observed rate ({observed:+.2f} lbs/week) differs from the goal "
            f"({goal.rate_lbs_per_week:+.2f}) by {difference:+.2f} lbs/week. "
            f"{direction.capitalize()} intake by about {abs(change)} kcal/day "
            f"to {target} kcal/day, then review in "
            f"{policy.REVIEW_CADENCE_DAYS} days. This corrects from your measured "
            "mean intake by the observed rate gap; the expenditure estimate alone "
            "is too uncertain to set intake directly"
            + (
                f" (energy-balance cross-check: {expenditure_target} kcal/day)."
                if expenditure_target is not None
                else "."
            )
        ),
        next_review_date=as_of + timedelta(days=policy.REVIEW_CADENCE_DAYS),
        protein_min_g=protein_min,
        protein_max_g=protein_max,
        observed_rate_lbs_per_week=observed,
        goal_rate_lbs_per_week=goal.rate_lbs_per_week,
        candidate_target_kcal_per_day=int(round(candidate)),
        evidence=evidence,
        target_basis="rate_correction",
        expenditure_target_kcal=expenditure_target,
    )


def _expenditure_target(
    estimate: ExpenditureEstimate,
    goal: GoalSpec | None,
) -> int | None:
    """Energy-balance target from the expenditure estimate and goal rate."""
    if goal is None or estimate.kcal_per_day is None:
        return None
    return int(round(estimate.kcal_per_day + goal.rate_lbs_per_week * policy.KCAL_PER_LB_WEEK))


def _evidence(estimate: ExpenditureEstimate) -> dict[str, object]:
    return {
        "segments_used": estimate.segments_used,
        "confidence": estimate.confidence,
        "complete_intake_days": estimate.intake.complete_days,
        "partial_intake_days": estimate.intake.partial_days,
        "estimated_intake_days": estimate.intake.estimated_days,
        "unknown_intake_days": estimate.intake.unknown_days,
        "intake_coverage": round(estimate.intake.weighted_coverage, 3),
        "weight_observations": estimate.weights.observations,
        "latest_weight_date": (
            estimate.weights.latest_entry.measured_at.isoformat()
            if estimate.weights.latest_entry
            else None
        ),
        "latest_intake_date": (
            estimate.intake.latest_date.isoformat() if estimate.intake.latest_date else None
        ),
    }
