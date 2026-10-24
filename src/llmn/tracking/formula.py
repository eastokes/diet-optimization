"""Formula-based energy expenditure calculations shared by tracking code."""

from __future__ import annotations

from llmn.profiles.body_calc import calculate_bmr, calculate_tdee
from llmn.tracking.models import UserProfile


def mifflin_tdee_at_weight(
    profile: UserProfile,
    weight_lbs: float,
    activity_level: str | None = None,
) -> float:
    """Return Mifflin-St Jeor BMR times an activity multiplier.

    Sex and activity normalization happen in ``profiles.body_calc`` so a raw
    profile string can never silently select the female branch.
    """
    bmr = calculate_bmr(
        profile.age,
        profile.sex,
        profile.height_inches,
        weight_lbs,
    )
    return calculate_tdee(bmr, activity_level or profile.activity_level)
