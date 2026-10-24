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
# Segments are 7-day blocks, so the largest observable span inside one block is
# 6 days. Requiring a shorter span (previously 4) let a handful of noisy
# weigh-ins masquerade as a precise energy-balance observation.
SEGMENT_LENGTH_DAYS = 7
MIN_SEGMENT_WEIGHT_SPAN_DAYS = 6
MIN_SEGMENT_WEIGHT_OBSERVATIONS = 3
MIN_SEGMENT_INTAKE_COVERAGE = 0.5
MIN_SEGMENTS_FOR_RECOMMENDATION = 2
# Below this many valid segments the learned level is still partly anchored to
# the baseline (Mifflin x activity); the rate-based target is unaffected.
MIN_SEGMENTS_FOR_STABLE_LEVEL = 6

# Assumed day-to-day weight noise (water, glycogen, gut contents) used to derive
# the standard error of a segment slope. The estimator must not treat a 4-day
# slope as a precise measurement of energy balance.
WEIGHT_NOISE_LBS = 0.7

# Baseline prior uncertainty for the learned bias. Mifflin x activity is not
# known to +/-100 kcal/day, so a tighter prior made the filter ignore data.
BASELINE_PRIOR_STD_KCAL = 250.0
MEASURED_BASELINE_PRIOR_STD_KCAL = 150.0

# Warn when intake coverage sits just under the gate so a verdict flip on one
# extra logged day is visible.
NEAR_GATE_MARGIN = 0.05

# Formula-only fallback uncertainty (kcal/day standard deviation).
FORMULA_UNCERTAINTY_KCAL = 250.0
FORMULA_UNCERTAINTY_95CI = 1.96 * FORMULA_UNCERTAINTY_KCAL

# Baseline uncertainty when the resting component is measured rather than
# predicted. A measured RMR removes the Mifflin-St Jeor error term but leaves
# real activity-multiplier uncertainty, so this is smaller, not tiny.
MEASURED_RMR_UNCERTAINTY_KCAL = 200.0
MEASURED_RMR_UNCERTAINTY_95CI = 1.96 * MEASURED_RMR_UNCERTAINTY_KCAL

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
