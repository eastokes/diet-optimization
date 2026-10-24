# Tracking and recommendation policy (v1)

This document records the numerical and compatibility decisions behind
`llmn intake`, `llmn goal`, `llmn activity`, `llmn tdee estimate`, and
`llmn tdee targets`. The values are engineering guardrails, not clinical
prescriptions. They are centralized in `src/llmn/tracking/policy.py` and carry
`POLICY_VERSION = "v1"` in every result.

## Interval and date semantics

- Analysis intervals are inclusive calendar dates: `start <= date <= end`.
- `--as-of` is the analysis date. Defaults to today; future dates are rejected.
- The default window is 28 days. `--window-days` sets the inclusive length.
- Weight history for display uses calendar ranges, not row counts. `--days 30`
  means the 30 calendar days ending on the analysis date.
- Missing intake dates represent unknown intake. They are never converted to
  zero. A stored zero row means confirmed zero intake.
- Weight and actual-intake observations cannot be dated in the future. Planned
  intake may be future-dated.

## Intake semantics

| Kind | Meaning | Used for learning? |
| --- | --- | --- |
| `actual` | Consumed intake | Yes, if complete |
| `planned` | Intended/planned intake | No |
| `legacy_unclassified` | Old `calorie_log` value | No |

Completeness and quality:

- `complete` + `measured` contributes full weight.
- `complete` + `estimated` contributes half weight and is reported separately.
- `partial` is excluded from the measured mean but counted in coverage.
- `unknown` completeness does not contribute to the mean.
- Repeated writes increment `revision` and replace the value for that
  user/date/kind.

Coverage is the weighted number of complete actual-intake days divided by the
number of calendar days in the interval. This makes seven complete days in a
28-day window visibly different from 28 complete days.

## Weight trend and rate

- The stored EMA is the display trend. It uses elapsed-day scaling:
  `alpha_adjusted = 1 - (1 - 0.1) ** days_elapsed`.
- Weight writes rebuild all later stored trends, so corrections and shuffled
  backfills are deterministic.
- The observed rate for recommendations is the least-squares slope of raw
  weights over actual elapsed dates. The EMA is deliberately not used for this
  slope because its initialization lag biases a short first window.
- The current formula weight uses the latest trend if it is at most 14 days old;
  otherwise the profile weight is the documented fallback. A backfilled old
  measurement never overwrites the profile.

## Evidence gates for a learned estimate

An adaptive estimate requires all of the following:

- At least 3 weight observations inside the window.
- A weight span of at least 14 calendar days.
- A latest weight no older than 14 days.
- At least 7 complete actual-intake days.
- Weighted intake coverage of at least 0.4.
- At least one valid 7-day segment with 2 weight observations, a 4-day span, and
  0.5 segment intake coverage.

If a gate fails, the estimate is `formula_only`: it exposes the Mifflin-St Jeor
starting point with ±490 kcal/day (95%) and the reason codes, but the
recommendation does not pretend the value was learned.

## Expenditure estimation

The 28-day window is divided into consecutive 7-calendar-day segments. For each
segment:

1. Compute the raw-weight slope over that segment.
2. Convert it to an implied deficit: `-slope_lbs_per_day * 3500`.
3. Compute the expected deficit from the segment's baseline Mifflin TDEE minus
   the coverage-weighted actual intake.
4. Update a scalar Kalman filter with `days_elapsed` and segment coverage.

The learned adjustment is the filter's bias added to the current Mifflin
baseline. Activity changes inside the window widen variance by `150^2`.
Uncertainty is reported as a 95% interval derived from filter variance; it is
not a validated clinical confidence interval and does not include food-logging
bias, water-weight shifts, or model misspecification.

## Recommendation guardrails

| Parameter | Value | Rationale |
| --- | --- | --- |
| Rate tolerance | 0.2 lb/week | Avoid reacting to noise |
| Energy per lb/week | 500 kcal/day | 3500 kcal/lb approximation |
| Maximum change | 250 kcal/day per review | Bounded, reviewable adjustment |
| Review cadence | 7 days | Matches weekly trend windows |
| Uncertainty hold | >350 kcal/day (95%) | Do not act on weak evidence |
| Calorie floor | 1500 male / 1200 female | Safety guardrail, not a prescription |
| Loss rate range | -2.0 to -0.25 lb/week | Supported product range |
| Gain rate range | 0.25 to 1.0 lb/week | Supported product range |

Assessment states describe progress relative to the requested goal, not proof
of exact intake or metabolism:

- `on_track`: observed rate is within the tolerance band.
- `intake_above_goal`: observed rate is above the goal rate (gaining too fast or
  losing too slowly); the recommendation decreases intake.
- `intake_below_goal`: observed rate is below the goal rate (losing too fast or
  gaining too slowly); the recommendation increases intake.
- `insufficient_data`: explicit hold, or a labelled formula-only initialization
  when there are no observations at all.
- `target_reached`: latest trend reached/crossed the target; transition to a
  maintenance goal rather than continuing to adjust.
- `no_goal`: set a dated goal before expecting a recommendation.

Unsupported goal rates are rejected with `UNSUPPORTED_GOAL_RATE` rather than
silently clamped into an apparently endorsed plan.

## JSON and compatibility

- New tracking payloads use `schema_version: 2`.
- Missing numeric values are `null`, not zero.
- Reason codes are stable uppercase strings.
- `tdee targets` retains flat compatibility fields (`tdee`,
  `recommended_intake`, `protein_min`, `protein_max`) while adding the structured
  `recommendation` object and `recommendation_text`.
- `calories log` remains a planned-intake command and also writes a
  `kind='planned'` intake row. Legacy `calorie_log` rows are copied to
  `legacy_unclassified` intake rows and excluded from learning until
  `intake confirm-legacy --apply` classifies them.

## Sources

- Mifflin MD, St Jeor ST, Hill LA, Scott BJ, Daugherty SA, Koh YO. A new
  predictive equation for resting energy expenditure in healthy individuals.
  *Am J Clin Nutr.* 1990;51(2):241-247.
  <https://doi.org/10.1093/ajcn/51.2.241>
- Frankenfield D, Roth-Yousey L, Compher C. Comparison of predictive equations
  for resting metabolic rate in healthy nonobese and obese adults: a systematic
  review. *J Am Diet Assoc.* 2005;105(5):775-789.
  <https://doi.org/10.1016/j.jada.2005.02.005>
- Wishnofsky M. Caloric equivalents of gained or lost weight. *Am J Clin Nutr.*
  1958;6(5):542-546. <https://doi.org/10.1093/ajcn/6.5.542> — source of the
  3500 kcal/lb approximation used for bounded adjustments; it is an
  approximation, not an exact physiological constant.
- Centers for Disease Control and Prevention. Losing Weight.
  <https://www.cdc.gov/healthy-weight-growth/losing-weight/> — public-health
  context for gradual weight-loss rates (about 1-2 lb/week).
