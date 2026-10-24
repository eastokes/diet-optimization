"""Formula-based energy expenditure calculations shared by tracking code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from llmn.profiles.body_calc import calculate_bmr, calculate_tdee
from llmn.tracking.models import UserProfile

BASELINE_MIFFLIN = "mifflin"
BASELINE_MEASURED_RMR = "measured_rmr"


@dataclass(frozen=True)
class BaselineExpenditure:
    """A baseline TDEE and where its resting component came from."""

    kcal_per_day: float
    source: str  # BASELINE_MIFFLIN | BASELINE_MEASURED_RMR
    resting_kcal: float
    resting_source: str  # "mifflin_st_jeor" | "measured_rmr"

    def to_dict(self) -> dict[str, object]:
        return {
            "kcal_per_day": round(self.kcal_per_day, 0),
            "source": self.source,
            "resting_kcal": round(self.resting_kcal, 0),
            "resting_source": self.resting_source,
        }


def resting_metabolic_rate_at_weight(
    profile: UserProfile,
    weight_lbs: float,
    on_date: date | None = None,
) -> float:
    """Return the resting component used by the baseline.

    A recorded measured RMR (for example a DEXA resting metabolic rate) wins
    over Mifflin-St Jeor, because the formula mis-predicts for real people by
    ±10-15% in either direction.
    """
    if profile.has_measured_rmr:
        assert profile.resting_metabolic_rate is not None
        return profile.resting_metabolic_rate
    return calculate_bmr(
        profile.age_on(on_date),
        profile.sex,
        profile.height_inches,
        weight_lbs,
    )


def baseline_expenditure(
    profile: UserProfile,
    weight_lbs: float,
    activity_level: str | None = None,
    on_date: date | None = None,
) -> BaselineExpenditure:
    """Return baseline TDEE from a measured RMR or Mifflin-St Jeor.

    The activity multiplier always applies: a measured RMR says nothing about
    how much the person moves, so only the resting component is replaced.
    """
    resting = resting_metabolic_rate_at_weight(profile, weight_lbs, on_date)
    multiplier_source = profile.has_measured_rmr
    return BaselineExpenditure(
        kcal_per_day=calculate_tdee(resting, activity_level or profile.activity_level),
        source=BASELINE_MEASURED_RMR if multiplier_source else BASELINE_MIFFLIN,
        resting_kcal=resting,
        resting_source="measured_rmr" if multiplier_source else "mifflin_st_jeor",
    )


def mifflin_tdee_at_weight(
    profile: UserProfile,
    weight_lbs: float,
    activity_level: str | None = None,
    on_date: date | None = None,
) -> float:
    """Return the baseline TDEE (measured RMR when recorded, else Mifflin).

    Kept under its historical name for callers that only need the number; see
    :func:`baseline_expenditure` when the provenance matters.
    """
    return baseline_expenditure(profile, weight_lbs, activity_level, on_date).kcal_per_day
