"""Data models for weight tracking and TDEE learning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


# Valid diet types and styles for validation
VALID_DIET_TYPES = ("omnivore", "pescatarian", "vegetarian", "vegan")
VALID_DIET_STYLES = ("standard", "slow_carb", "low_carb", "keto", "mediterranean", "paleo")

VALID_INTAKE_KINDS = ("actual", "planned", "legacy_unclassified")
VALID_COMPLETENESS = ("complete", "partial", "unknown")
VALID_QUALITY = ("measured", "estimated", "unknown")
VALID_GOAL_MODES = ("loss", "maintenance", "gain")
VALID_ACTIVITY_LEVELS = ("sedentary", "lightly_active", "moderate", "active", "very_active")


@dataclass
class UserProfile:
    """User profile for weight tracking and TDEE calculations."""

    user_id: Optional[int]
    age: int
    sex: str  # 'male' or 'female'
    height_inches: float
    weight_lbs: float
    activity_level: str  # 'sedentary', 'lightly_active', 'moderate', 'active', 'very_active'
    goal: Optional[str] = None  # e.g., 'fat_loss:185:165'
    target_weight_lbs: Optional[float] = None
    diet_type: Optional[str] = None  # 'omnivore', 'pescatarian', 'vegetarian', 'vegan'
    diet_style: Optional[str] = None  # 'standard', 'slow_carb', 'low_carb', etc.
    created_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.sex not in ("male", "female"):
            raise ValueError(f"sex must be 'male' or 'female', got '{self.sex}'")
        valid_levels = (
            "sedentary",
            "lightly_active",
            "moderate",
            "active",
            "very_active",
        )
        if self.activity_level not in valid_levels:
            raise ValueError(
                f"activity_level must be one of {valid_levels}, got '{self.activity_level}'"
            )
        if self.diet_type is not None and self.diet_type not in VALID_DIET_TYPES:
            raise ValueError(
                f"diet_type must be one of {VALID_DIET_TYPES}, got '{self.diet_type}'"
            )
        if self.diet_style is not None and self.diet_style not in VALID_DIET_STYLES:
            raise ValueError(
                f"diet_style must be one of {VALID_DIET_STYLES}, got '{self.diet_style}'"
            )


@dataclass
class WeightEntry:
    """A single weight log entry with EMA trend."""

    log_id: Optional[int]
    user_id: int
    weight_lbs: float
    trend_lbs: float
    measured_at: date
    notes: Optional[str] = None


@dataclass
class CalorieEntry:
    """A legacy planned-calorie log entry."""

    log_id: Optional[int]
    user_id: int
    date: date
    planned_calories: float
    notes: Optional[str] = None


@dataclass
class IntakeEntry:
    """A dated intake observation.

    Missing dates are represented by missing rows, never by a zero row.
    ``legacy_unclassified`` rows come from the old planned-calorie table and
    are excluded from measured-intake learning until deliberately classified.
    """

    intake_id: Optional[int]
    user_id: int
    date: date
    kcal: float
    kind: str = "actual"
    completeness: str = "complete"
    quality: str = "measured"
    provenance: str = "manual"
    notes: Optional[str] = None
    revision: int = 1

    def __post_init__(self) -> None:
        if not math.isfinite(self.kcal) or self.kcal < 0:
            raise ValueError("kcal must be a finite, non-negative number")
        if self.kind not in VALID_INTAKE_KINDS:
            raise ValueError(
                f"kind must be one of {VALID_INTAKE_KINDS}, got '{self.kind}'"
            )
        if self.completeness not in VALID_COMPLETENESS:
            raise ValueError(
                f"completeness must be one of {VALID_COMPLETENESS}, got '{self.completeness}'"
            )
        if self.quality not in VALID_QUALITY:
            raise ValueError(f"quality must be one of {VALID_QUALITY}, got '{self.quality}'")


@dataclass
class GoalPeriod:
    """An effective-dated weight-change goal.

    ``rate_lbs_per_week`` is signed: negative for loss, zero for maintenance,
    and positive for gain.
    """

    goal_id: Optional[int]
    user_id: int
    effective_from: date
    mode: str
    rate_lbs_per_week: float
    target_weight_lbs: Optional[float] = None
    training_priority: Optional[str] = None
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        if self.mode not in VALID_GOAL_MODES:
            raise ValueError(f"mode must be one of {VALID_GOAL_MODES}, got '{self.mode}'")
        if not math.isfinite(self.rate_lbs_per_week):
            raise ValueError("rate_lbs_per_week must be finite")
        if self.mode == "maintenance" and self.rate_lbs_per_week != 0:
            raise ValueError("maintenance goals must have rate_lbs_per_week = 0")
        if self.mode == "loss" and self.rate_lbs_per_week >= 0:
            raise ValueError("loss goals must have a negative rate_lbs_per_week")
        if self.mode == "gain" and self.rate_lbs_per_week <= 0:
            raise ValueError("gain goals must have a positive rate_lbs_per_week")
        if self.target_weight_lbs is not None and (
            not math.isfinite(self.target_weight_lbs) or self.target_weight_lbs <= 0
        ):
            raise ValueError("target_weight_lbs must be a finite, positive number")


@dataclass
class ActivityPeriod:
    """An effective-dated activity/training context period."""

    activity_id: Optional[int]
    user_id: int
    effective_from: date
    activity_level: str
    training_priority: Optional[str] = None
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        if self.activity_level not in VALID_ACTIVITY_LEVELS:
            raise ValueError(
                f"activity_level must be one of {VALID_ACTIVITY_LEVELS}, "
                f"got '{self.activity_level}'"
            )


@dataclass
class TDEEEstimate:
    """TDEE estimate from the expenditure filter."""

    estimate_id: Optional[int]
    user_id: int
    estimated_at: date
    mifflin_tdee: float
    tdee_bias: float
    variance: float
    adjusted_tdee: float
    method: Optional[str] = None
    policy_version: Optional[str] = None
    interval_start: Optional[date] = None
    interval_end: Optional[date] = None
    weight_observations: Optional[int] = None
    complete_intake_days: Optional[int] = None
    partial_intake_days: Optional[int] = None
    input_fingerprint: Optional[str] = None

    @property
    def uncertainty_95ci(self) -> float:
        """Return 95% confidence interval half-width."""
        import math

        return 1.96 * math.sqrt(self.variance)
