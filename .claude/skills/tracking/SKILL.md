---
name: tracking
description: Weight tracking and TDEE learning. Use when user wants to log weight, track progress, check their trend, see how their diet is working, or get adaptive calorie targets. Triggers on "log my weight", "track weight", "how am I doing", "check my progress", "TDEE", "am I losing weight", "weight trend".
---

# Weight Tracking and Adaptive TDEE Learning

This skill helps users track their weight and learn their personalized TDEE (Total Daily Energy Expenditure) using:
- **Hacker's Diet EMA**: Exponentially smoothed moving average to filter out daily noise (water, gut contents, scale error)
- **Kalman Filter**: Learns the deviation between actual TDEE and Mifflin-St Jeor estimate

## Quick Commands

```bash
# Log today's weight
uv run llmn weight add 184.2

# Log weight for a specific date
uv run llmn weight add 183.8 --date 2025-12-27

# View weight history with EMA trends
uv run llmn weight list --days 30

# Log ACTUAL intake (used for expenditure learning)
uv run llmn intake log 2000

# Log intake for a specific date
uv run llmn intake log 2000 --date 2026-10-22 --quality measured

# Mark a day partial or estimated
uv run llmn intake log 1200 --completeness partial
uv run llmn intake log 2100 --quality estimated

# Set a dated goal and activity context
uv run llmn goal set --mode loss --rate -1.0 --target 165 --effective-from 2026-10-01
uv run llmn activity set --level moderate --effective-from 2026-10-01

# Persist an expenditure estimate (diagnostic snapshot)
uv run llmn tdee estimate --as-of 2026-10-22

# Show comprehensive progress report
uv run llmn tdee progress --as-of 2026-10-22

# Get a bounded recommendation or an explicit hold
uv run llmn tdee targets --as-of 2026-10-22 --json
```

**Planned vs actual:** `llmn calories log` remains a *planned* intake command.
Never treat it as consumed food. Actual intake must be logged with
`llmn intake log`. Missing days are unknown, not zero. Partial days are excluded
from the measured mean, and estimated days are down-weighted.

**Recommendation states:** `on_track`, `intake_above_goal`,
`intake_below_goal`, `insufficient_data`, `target_reached`, and `no_goal`.
An insufficient-evidence result is a successful analysis with
`recommendation.action: "hold"` and a `null` target, not a crash and not a
fabricated number. Formula-only initialization is labelled separately.

## Workflow

### Step 1: Check if User Has a Profile

```bash
uv run llmn user show
```

If no profile exists, create one:

```bash
# Preferred: a birthdate keeps the formula age from going stale
uv run llmn user create --birthdate 1981-11-01 --sex male --height 73 --weight 177
uv run llmn user create --age 38 --sex male --height 72 --weight 185

# Optional: a measured RMR (DEXA, indirect calorimetry) replaces the
# Mifflin-St Jeor resting term; the activity multiplier still applies
uv run llmn user update --rmr 1850 --rmr-date 2026-09-17 --rmr-source dexa
uv run llmn user update --weight 185 --goal "fat_loss:165"
```

`llmn user show` reports `age_source` (`birthdate` or `stored_age`) and
`baseline_source` (`measured_rmr` or `mifflin`). Never state a learned TDEE
without saying which baseline it was learned against.

**Activity levels**: `sedentary`, `lightly_active`, `moderate`, `active`, `very_active`

### Step 2: Log Weight

When the user provides a weight measurement:

```bash
uv run llmn weight add <weight_in_lbs>

# With specific date
uv run llmn weight add <weight> --date YYYY-MM-DD
```

The system automatically computes the EMA trend using the Hacker's Diet formula:
```
trend_n = trend_{n-1} + 0.1 × (weight_n - trend_{n-1})
```

This filters out daily noise with a ~10-day time constant.

### Step 3: Log Actual Intake

When the user reports what they actually ate or an estimated daily total:

```bash
uv run llmn intake log <calories>

# With specific date
uv run llmn intake log <calories> --date YYYY-MM-DD

# Partial or estimated logs
uv run llmn intake log <calories> --completeness partial
uv run llmn intake log <calories> --quality estimated
```

Use `llmn calories log` only when the user is recording a meal *plan*. It is not
consumed intake and is excluded from expenditure learning.

### Step 4: Check Progress

For progress check requests:

```bash
uv run llmn tdee progress
```

This shows one window shared with `llmn tdee targets`:
- Current weight vs EMA trend
- EMA rate (trend endpoints, smoothed) **and** measured rate (least-squares
  over raw weights); recommendations use the measured rate
- Mifflin-St Jeor baseline at the calculation weight and activity level
- Learned TDEE offset (from the Kalman filter) or `formula_only` fallback
- Estimated TDEE with 95% uncertainty and the number of weekly segments used
- Mean logged intake, its coverage, expected deficit, and implied deficit
- Progress toward the dated goal weight at the measured rate

Do not quote the EMA rate as the adaptive rate, and do not mix the intake mean
of one window with the expenditure estimate of another. Report the window.

Write feedback: `intake log` returns the stored row plus `replaced`/`previous`
so a revision is confirmable, and `weight add` reports replacement, the previous
measurement date, trend change, and how many later trends were rebuilt.

### Step 5: Update TDEE Estimate

To update the Kalman filter with recent data:

```bash
uv run llmn tdee estimate
```

This runs the Kalman filter and shows a brief summary:
```
Updated Total Daily Energy Expenditure (TDEE) estimate (4 weeks processed)
  Mifflin-St Jeor baseline: 2528 kcal/day
  Learned bias: -150 kcal/day
  Your TDEE: 2378 ± 60 kcal/day
```

After 2-3 weeks of data, the filter converges on your personalized TDEE.

### Step 6: Get a Bounded Recommendation

For an actionable recommendation based on current progress:

```bash
uv run llmn tdee targets --as-of YYYY-MM-DD --json
```

The command analyses actual intake, dates, weights, activity context, and the
effective goal. It reports the analysis window, coverage, method, uncertainty,
reason codes, and one of the assessment states above. Changes are bounded
(250 kcal/day per review) and held when uncertainty is too high.

Never relay a `null` target as zero. If the assessment is `insufficient_data` or
`no_goal`, ask for more complete intake/weight logs or a goal instead of
inventing a calorie number. Formula-only initialization is explicitly labelled
and is not evidence-backed.

## Example Progress Report

```
Weight Tracking Report (last 30 days)
=============================================
Current weight: 182.4 lbs
Current trend:  183.1 lbs (EMA)
Trend change:   -4.8 lbs (from 187.9)
Rate:           1.2 lbs/week (losing)

TDEE Analysis
=============================================
Mifflin-St Jeor baseline: 2150 kcal/day
Learned adjustment:       -85 kcal/day
Your estimated TDEE:      2065 ± 90 kcal/day

Average planned intake: 1900 kcal/day
  Expected deficit: 250 kcal/day
  Implied deficit:  600 kcal/day (from trend)

Progress toward goal (165 lbs)
---------------------------------------------
  Remaining: 18.1 lbs
  At current rate: ~15 weeks

Notes:
  - Losing faster than expected by ~350 kcal/day.
    Possible causes: actual intake lower than logged,
    activity higher than estimated, or metabolism
    faster than Mifflin-St Jeor predicts.
```

## Interpreting Results

### Trend vs Weight

- **Weight**: Raw daily measurement (noisy)
- **Trend (EMA)**: Filtered signal showing true direction
- Focus on trend direction, not daily fluctuations

### Implied vs Expected Deficit

| Scenario | Meaning |
|----------|---------|
| Implied > Expected | Burning more than predicted (faster loss) |
| Implied < Expected | Burning less than predicted (slower loss) |
| Implied ≈ Expected | Mifflin-St Jeor is accurate for you |

### TDEE Bias

- **Positive bias**: Your TDEE is higher than Mifflin-St Jeor predicts
- **Negative bias**: Your TDEE is lower than predicted
- **Uncertainty (±)**: Decreases as more data is collected

## Daily Workflow

Encourage users to log daily (takes 10 seconds):

```bash
# Morning: log weight
uv run llmn weight add 183.2

# After meal planning: log calories
uv run llmn calories log 1850
```

### Body composition (recomp)

Scale weight cannot separate fat loss from muscle gain. Record DEXA, BodPod,
tape, or scale measurements and compare them:

```bash
uv run llmn bodycomp add --date 2026-09-17 --method dexa \
  --weight 176.9 --fat-mass 44.7 --lean-mass 125.3 --bone-mass 6.9
uv run llmn bodycomp add --date 2026-12-17 --method dexa \
  --weight 173.0 --fat-mass 39.0 --lean-mass 127.0 --bone-mass 6.9
uv run llmn bodycomp trend --json
```

DEXA lean soft tissue excludes bone mineral content; supply `--bone-mass` or
`--fat-free-mass` so fat-free mass is not understated. The comparison classifies
`recomposition`, `fat_loss_only`, `fat_and_lean_loss`, `lean_gain`, `gain`,
`unfavorable`, `within_noise`, `not_comparable`, or `insufficient_data`, and it
warns when the interval is under 4 weeks or the methods differ. Never compare a
DEXA body-fat percentage with a bioimpedance scale percentage.

Weekly: check progress with `uv run llmn tdee progress`

## Connecting to Meal Planning

Once TDEE is learned, use it for better meal planning:

```bash
# Get your personalized targets (uses adjusted TDEE)
uv run llmn tdee estimate

# Then optimize with those targets
uv run llmn optimize --pattern pescatarian --template --goal "fat_loss:183lbs:165lbs" --json
```

The goal flag uses Mifflin-St Jeor by default, but knowing your actual TDEE helps interpret whether you need to adjust calorie targets.

## Example Session

User: "I weighed 183.2 this morning"

1. Log the weight:
   ```bash
   uv run llmn weight add 183.2
   ```

2. Show the result with trend:
   ```
   Logged: 183.2 lbs
   Trend: 183.8 lbs (EMA)
   Change from yesterday: -0.3 lbs (trend)
   ```

3. Offer to show full progress:
   ```bash
   uv run llmn tdee progress
   ```

User: "How am I doing on my diet?"

1. Run progress report:
   ```bash
   uv run llmn tdee progress
   ```

2. Summarize key metrics:
   - "You've lost 4.8 lbs (trend) over the last 30 days"
   - "That's 1.2 lbs/week - right on track for healthy weight loss"
   - "At this rate, you'll reach 165 lbs in about 15 weeks"

3. Note any discrepancies between expected and implied deficit

## Key Insights from The Hacker's Diet

1. **Trust the trend, not the scale**: Daily weight is noisy; EMA shows reality
2. **Direction matters more than magnitude**: If trend is going down, you're in deficit
3. **Patience with the filter**: EMA has ~10-day lag; don't panic at daily fluctuations
4. **Consistency beats precision**: Weighing daily matters more than weighing accurately
