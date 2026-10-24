"""Documented numerical policy for standalone calorie recommendations.

These values are engineering guardrails, not clinical prescriptions. See
``docs/tracking-policy.md`` for rationale and sources.
"""

from __future__ import annotations

POLICY_VERSION = "v1"

# Analysis interval (inclusive calendar dates).
DEFAULT_WINDOW_DAYS = 28
MIN_ANALYSIS_SPAN_DAYS = 14
MAX_WEIGHT_AGE_DAYS = 14
STALE_CURRENT_WEIGHT_DAYS = 14

# Evidence gates for an adaptive (learned) expenditure estimate.
MIN_WEIGHT_OBSERVATIONS = 3
MIN_COMPLETE_INTAKE_DAYS = 7
MIN_INTAKE_COVERAGE = 0.4
MIN_SEGMENT_WEIGHT_SPAN_DAYS = 4
MIN_SEGMENT_INTAKE_COVERAGE = 0.5

# Formula-only fallback uncertainty (kcal/day standard deviation).
FORMULA_UNCERTAINTY_KCAL = 250.0
FORMULA_UNCERTAINTY_95CI = 1.96 * FORMULA_UNCERTAINTY_KCAL

# Recommendation guardrails.
RATE_TOLERANCE_LBS_PER_WEEK = 0.2
KCAL_PER_LB_WEEK = 500.0  # 3500 kcal/lb over 7 days
MAX_TARGET_CHANGE_KCAL_PER_DAY = 250
REVIEW_CADENCE_DAYS = 7
MAX_RECOMMENDATION_UNCERTAINTY_95CI = 350.0
MIN_CALORIES_MALE = 1500
MIN_CALORIES_FEMALE = 1200

# Supported requested rates (signed lbs/week). Requests outside these bounds
# are rejected rather than silently clamped into an endorsed plan.
MIN_LOSS_RATE_LBS_PER_WEEK = -2.0
MAX_LOSS_RATE_LBS_PER_WEEK = -0.25
MAX_GAIN_RATE_LBS_PER_WEEK = 1.0
MIN_GAIN_RATE_LBS_PER_WEEK = 0.25
DEFAULT_LOSS_RATE_LBS_PER_WEEK = -1.0
DEFAULT_GAIN_RATE_LBS_PER_WEEK = 0.5

# Activity changes mid-window inflate uncertainty by this variance (150^2).
ACTIVITY_CHANGE_VARIANCE = 22500.0
