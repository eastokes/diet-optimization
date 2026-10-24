---
plan_id: standalone-calorie-recommendations
plan_version: 1
status: in_progress
implementation_authorized: true
created: 2026-10-23
project: llmn
repository_directory: diet-optimization
---

# Standalone calorie tracking and recommendations

## Execution contract

This is a saved implementation plan, not an instruction to start implementation.
Wait for the owner to explicitly resume implementation. No phase is complete.
After authorization, use task IDs below to report progress and record evidence.
Revalidate this plan against the current checkout before changing code; source
line numbers and behavior may have changed since the assessment.

Read the workspace instructions, repository instructions (currently `CLAUDE.md`),
and `.claude/skills/tracking/SKILL.md` before work. Inspect deeper instructions
and relevant skill catalogs. Check Git status and preserve unrelated edits.
At planning time, `CLAUDE.md`, `README.md`, and `pyproject.toml` have uncommitted
mise-related edits, and `mise.toml` is untracked on `feature/mise-tasks`. These
changes are earlier work, not part of this plan. Establish a clean implementation
branch without discarding or silently committing them. Do not commit or push
without authorization.

Keep phase status in this document as work proceeds. Record commands actually
run, outcomes, limitations, and any deliberate design changes. A prepared test
or intended behavior is not a passing result.

## Outcome and user requirements

The user supplies measured calorie intake, measured weight, workout context,
and weight/workout goals through an agent. They log most days, with gaps. The
CLI must summarize trends, assess whether intake is above, below, or consistent
with the stated goal, and recommend future intake when the evidence supports it.

Calorie recommendations must work independently of food imports, food search,
recipes, meal plans, and optimization. Preserve those features. Later, meal
planning may consume an explicit recommendation from the same service.

Required behavior:

- Dated actual-intake and weight records, including corrections and backfills.
- Separate actual intake, planned intake, and recommended targets.
- Explicit handling of missing, estimated, incomplete, and confirmed-zero days.
- Calendar-aware trends and aligned intake/weight analysis intervals.
- Configurable weight-change goals and dated activity/training context.
- Evidence and freshness indicators, explicit abstention, and understandable
  explanations for an agent to relay.
- Local storage, noninteractive commands, predictable JSON, reproducible output,
  and no requirement to load USDA data or run a solver.

Not in the first release: food-photo recognition, wearable integrations,
automatic exercise-calorie reimbursement, new UI/MCP services, deadline-based
goal optimization, physiological simulation, or rewriting the meal optimizer.
A new estimation algorithm is optional only if comparison tests justify it.

## Assessment baseline to reproduce

These findings were verified by source inspection; items B1, B3, and B6 also
have dependency-free reproductions from the assessment. Treat these as regression
requirements, not a complete audit.

| ID | Existing behavior | Source entry point |
| --- | --- | --- |
| B1 | Tracking passes a sex string into `calculate_bmr`, which compares against `Sex.MALE`. A synthetic male profile (age 38, 72 inches, 185 lb, moderate activity) gives 2528.28 instead of 2785.58 kcal/day. | `tracking/diagnostics.py::calculate_mifflin_tdee_at_weight`, `profiles/body_calc.py::calculate_bmr` |
| B2 | Estimation groups seven measurements, assumes seven elapsed days, and skips complete trailing groups: seven entries yield zero updates; fourteen yield one. Calorie coverage is never passed to the filter. | `cli.py::tdee_estimate`, `tracking/tdee_filter.py` |
| B3 | Weight persistence calls the EMA helper without elapsed days. A seven-day gap from 185 to 181 lb stores 184.6 instead of the helper's approximately 182.9132. Historical edits do not recompute subsequent trends. | `tracking/queries.py::WeightQueries.add_weight`, `tracking/ema.py` |
| B4 | Calorie records have only `planned_calories`; averages omit missing days without reporting coverage. Partial/estimated/actual intake cannot be distinguished. | `tracking/models.py::CalorieEntry`, `tracking/queries.py::CalorieQueries`, `db/schema.py` |
| B5 | Goals use fixed rates; gain feedback lacks an explicit too-fast branch; maintenance lacks equivalent corrective feedback. Recommendation adjustments are not bounded and uncertainty does not gate advice. | `cli.py::tdee_targets` |
| B6 | Weight reports select a number of rows rather than a date interval. A 30-day request can include two entries 90 days apart. Calorie reports use calendar dates. | `tracking/diagnostics.py::generate_weight_report`, `tracking/queries.py::get_weight_history` |
| B7 | Targets reuse the latest saved estimate without checking freshness. Weight logs do not update the profile weight used for current formula calculations. Historical activity and goals are not recorded. | `cli.py::weight_add`, `cli.py::tdee_targets`, `tracking/models.py::UserProfile` |

All source paths in this table are relative to `src/llmn/`. Existing gap/coverage
unit tests exercise helpers; CLI tracking paths need integration tests.

## Architecture and invariants

Retain SQLite and the existing CLI command groups. Extract estimation and
recommendation policy from `cli.py` into a tracking application service, with
typed request/result models and pure calculation functions where practical.
Suggested modules are `tracking/service.py`, `tracking/estimation.py`, and
`tracking/recommendations.py`; confirm names after inspecting the current tree.

The dependency direction must be CLI -> tracking service -> tracking queries /
calculation helpers. Meal optimization may consume tracking results through an
adapter; tracking must not import optimization solvers or meal-planning logic.
Sharing validated body-calculation primitives is acceptable.

Invariants:

1. Raw observations remain distinguishable from derived data. Recomputing a
   trend or recommendation never rewrites the user's measurements or invents
   measured intake for unknown days.
2. Use actual elapsed dates. Specify interval boundaries and whether intake on
   a weigh-in date represents the period before or after that measurement.
   Counts of measurements are never interchangeable with elapsed days.
3. Unknown intake is not zero. Confirmed fasting/zero intake is valid data.
   Incomplete days are not complete days with unusually low intake.
4. Corrections invalidate downstream derived data. Identical inputs, policy
   version, and analysis date yield identical results. Repeated reads do not
   compound filter updates.
5. Forecasts, assessments, and recommendations are distinct. A formula-only
   starting target can exist while the assessment remains `insufficient_data`.
6. Every result identifies the analyzed interval, data coverage, latest input
   dates, estimation method, applicable goal, and reasons for its decision.
7. Reject nonfinite numbers, invalid dates, negative intake, nonpositive weight,
   and invalid goal configurations before writing. Define future-date behavior.
8. Protect user data: tests use temporary databases and explicit analysis dates;
   no tests or examples write to the user's real `~/.llmn` database.

## Data and compatibility design

Use versioned, transactional, repeatable schema migrations. Test migration from
the existing schema and an already migrated database. Preserve IDs, observations,
notes, user profiles, and unrelated food/optimization tables. Establish a backup
and recovery procedure before migrating a real database.

Proposed records (final field names may change with a documented rationale):

- Intake observation: user, local date, kcal, kind (`actual` or `planned`),
  completeness (`complete` or `partial`), measurement quality (`measured` or
  `estimated`), provenance, notes, and update/revision information. A missing
  date represents unknown intake; do not require synthetic daily rows.
- Legacy intake: retain the original `planned_calories` value with an explicit
  `legacy_unclassified` provenance/kind until confirmed. Do not silently relabel
  historical plans as measured consumption. Provide a deliberate confirmation
  path, preview affected dates, and avoid per-day interactive prompts for agents.
- Weight observation: existing dated measurement plus explicit units at the CLI
  boundary. Keep the internal unit consistent with existing calculations for the
  first implementation. Derived trend storage must be rebuildable/versioned.
- Goal period: effective dates, loss/maintenance/gain mode, signed requested
  rate, optional target weight, and optional training priority/context. Preserve
  past goal periods rather than overwriting history.
- Activity context period: effective dates, activity level and optional notes
  about training changes, travel, illness, or a logging break. Unstructured
  context must not automatically generate numerical burn estimates.
- Derived estimate/recommendation: optional persisted snapshot with input
  revision/fingerprint, analysis date, policy/algorithm version, method, evidence,
  and status. Cached values must be invalidated or clearly marked stale.

Keep legacy commands available where feasible, but never silently change a
command documented as logging planned calories into an actual-intake command.
Propose an explicit command such as `llmn intake log` for actual intake, retain
`calories log` compatibility, and document its meaning. Schema/JSON changes need
versioning or a clearly documented compatibility strategy.

## Estimation and recommendation policy

Start by repairing and evaluating the existing EMA and scalar TDEE-bias filter.
Do not assume that adding a Kalman filter makes missing intake identifiable.
Weight changes alone cannot uniquely determine both unknown intake and expenditure.

Use a common analysis interval and explicit analysis date (`as_of`) throughout.
Form observations from elapsed-time windows or actual measurement intervals;
document boundary rules and avoid treating overlapping observations as independent.
Define minimum span, observation count, coverage, and recency requirements.
Missing weights can be handled by elapsed-time-aware smoothing; interpolated
values must be labeled and cannot inflate the count of real observations.

For missing intake, prefer holding/abstaining on insufficient evidence in the
first release. If imputation is introduced, expose its assumptions and sensitivity,
keep it separate from raw data, and test nonrandom gaps (e.g. unlogged weekends).
Weight intake estimates by documented quality/coverage and pass that information
through the actual service path. Do not present filter variance as a validated
clinical confidence interval; identify what the interval includes and omits.

Resolve the current formula baseline from a recent observation/trend with a
documented profile-weight fallback. Handle activity changes and long gaps by
widening uncertainty, segmenting history, or holding learning according to policy.
Do not reuse estimates blindly after input or goal changes.

Recommendation policy must cover both directions for loss, maintenance, and gain.
Compare observed rate with the effective requested rate using a tolerance band.
Use uncertainty and evidence sufficiency to distinguish `on_track`,
`intake_above_goal`, `intake_below_goal`, and `insufficient_data`. These labels
describe progress relative to the goal, not proof of exact intake or metabolism.
Specify behavior when a target weight has been reached or crossed.

Separate a candidate calorie target from the final suggested change. Define
bounded changes, a review cadence, uncertainty-related holds, and unsupported
goal handling. Select numerical defaults and applicable population limits using
primary evidence during implementation; cite sources in the policy documentation.
These are unresolved engineering/product parameters, not approved nutrition rules.
Do not silently clamp an unsupported request into an apparently endorsed plan.
Daily training/rest targets and deadline-based rates are deferred; avoid adding
workout calories on top of an expenditure estimate that already reflects training.

## Agent-facing result contract

Use typed result models for both text and JSON rendering. Retain the existing
response envelope where practical. JSON must distinguish missing values from
zero, identify units, use stable reason codes, and contain no NaN/Infinity.
Failures return structured JSON and a nonzero exit code; insufficient evidence
is a successful analysis with an explicit assessment state, not a crash.

Illustrative shape only; this is not an implemented API:

```json
{
  "success": true,
  "schema_version": 1,
  "command": "tdee targets",
  "data": {
    "as_of": "2026-10-23",
    "policy_version": "v1",
    "analysis_window": {"start": "2026-09-25", "end": "2026-10-23"},
    "goal": {"mode": "maintenance", "rate_lbs_per_week": 0},
    "assessment": "insufficient_data",
    "reason_codes": ["LOW_INTAKE_COVERAGE"],
    "evidence": {
      "complete_intake_days": 3,
      "partial_intake_days": 1,
      "weight_observations": 2,
      "latest_weight_date": "2026-10-22"
    },
    "expenditure": {"method": "formula_only", "kcal_per_day": null},
    "observed_rate_lbs_per_week": null,
    "recommendation": {
      "action": "hold",
      "target_kcal_per_day": null,
      "change_kcal_per_day": null
    }
  },
  "warnings": ["More complete intake history is needed before adapting targets."],
  "human_summary": "Insufficient evidence to recommend a calorie adjustment."
}
```

Validate existing `--user`/`--id` conventions and provide consistent selection
and date/unit options for new commands. Invalid input under `--json` must not
fall through to tracebacks or human-only output. Make `targets` calculate from
current inputs without requiring users to remember a separate refresh command;
retain `estimate` for explicit diagnostic/snapshot workflows. Document any
one-time schema initialization separately from read-only analysis behavior.

## Implementation phases

Each task starts unchecked. Complete dependencies before dependent work. Keep
phases independently reviewable; do not bundle unrelated formatting cleanup.

### P0 — Establish baseline and policy specification

Depends on: explicit implementation authorization.

- [x] P0.1 Re-read instructions, inspect Git state, and establish an isolated branch.
- [x] P0.2 Review/trust the individual mise config; install pinned tools and sync
  locked dependencies. Record the upstream test/lint/typecheck baseline.
- [x] P0.3 Reproduce B1–B7 using temporary fixtures, including CLI-level cases.
- [x] P0.4 Specify interval semantics, eligibility thresholds, goal tolerance,
  change limits, population scope, legacy migration semantics, and JSON versioning.
  Research/cite primary sources for nutrition-related numerical policy.

Acceptance: baseline failures are distinguished from new regressions; a short
policy decision record resolves the numerical and compatibility questions above.
Ask the owner only for unresolved preferences that materially change behavior.

### P1 — Correct measurements, dates, and formula calculations

Depends on: P0.

- [x] P1.1 Normalize sex/activity types at the boundary and test both formula branches.
- [x] P1.2 Use calendar ranges consistently, with defined inclusive/exclusive endpoints.
- [x] P1.3 Apply elapsed-day EMA smoothing in persistence; rebuild affected
  subsequent trends after inserts, replacements, and supported deletions.
- [x] P1.4 Obtain current calculation weight from recent observations and expose
  fallback/staleness; do not overwrite a current profile with an older backfill.

Acceptance: B1, B3, and B6 regressions pass; chronological import and shuffled
backfill yield equivalent final trends; per-user records remain isolated.

### P2 — Separate intake semantics and introduce dated goals/context

Depends on: P0 and P1.

- [x] P2.1 Implement/version transactional migrations and legacy-intake handling.
- [x] P2.2 Add actual-intake CRUD/upsert commands and completeness/quality fields.
  Define repeated-write behavior and expose corrections to derived-data invalidation.
- [x] P2.3 Add effective-dated goal/activity records with validated units/rates,
  deterministic same-date updates, and explicit overlap rules.
- [ ] P2.4 Preserve existing food data and command compatibility; document migration
  preview, backup, confirmation, and recovery procedures.

Acceptance: legacy ambiguous calories are excluded from measured-intake learning
until classified; partial/estimated/zero/unknown cases remain distinguishable;
repeated migration does not change data; failed migration rolls back cleanly.

### P3 — Extract and repair expenditure estimation

Depends on: P1 and P2.

- [x] P3.1 Move estimation out of CLI handlers into a shared, testable service.
- [x] P3.2 Build elapsed-time observations; fix terminal-window/count errors and
  align intake, weight, and activity periods.
- [x] P3.3 Enforce evidence gates and feed coverage/quality into the estimator.
  Mark formula-only, held, insufficient, and learned estimates distinctly.
- [x] P3.4 Invalidate derived state after edits; handle stale estimates, long gaps,
  activity changes, and repeated invocations deterministically.
- [ ] P3.5 Compare results on synthetic histories with known expenditure and on
  realistic missingness/noise patterns. Document bias and uncertainty limitations.

Acceptance: B2, B4, and B7 regressions pass; recommendation reads use current
inputs; sparse data cannot acquire the same confidence as dense data; no double
learning from repeated reads or duplicate/correlated observations.

### P4 — Standalone recommendation policy and CLI contract

Depends on: P3.

- [x] P4.1 Implement a pure recommendation policy using the shared analysis result.
- [x] P4.2 Support configurable loss/maintenance/gain rates, both correction
  directions, target-reached behavior, bounded changes, and evidence-based holds.
- [x] P4.3 Render consistent structured results and human explanations; include
  units, dates, reason codes, coverage, source method, and uncertainty limitations.
- [x] P4.4 Wire `tdee targets` and `progress` to the service; preserve explicit
  diagnostic estimation and document the legacy command compatibility strategy.

Acceptance: an agent can obtain a recommendation using only profile, goal,
weight, and actual-intake commands on a fresh database with zero food rows;
no solver runs; all three goals produce above/below/on-track/insufficient results
when appropriate; invalid inputs have consistent machine-readable errors.

### P5 — Optional meal-planning integration and final handoff

Depends on: P4. Standalone acceptance must pass first.

- [ ] P5.1 Add an explicit opt-in adapter allowing meal planning to consume the
  shared recommendation without duplicating target policy. Define precedence
  for explicit calorie constraints and behavior when the recommendation is held.
- [ ] P5.2 Preserve existing explicit meal constraints and optimization behavior;
  do not silently override them or record a proposed meal plan as consumed food.
- [x] P5.3 Update README, repository agent guidance, and the tracking skill with
  tested standalone examples, migration instructions, and JSON/status semantics.
- [ ] P5.4 Run final validation and record remaining limitations and deferred work.

Acceptance: meal planning remains optional; the opt-in adapter matches the
standalone target for the same inputs; existing food/optimizer regression tests
continue to pass except for explicitly reviewed pre-existing failures.

## Required test matrix

Use explicit dates and temporary databases, including end-to-end CLI invocation.
Prefer deterministic scenario fixtures and invariant checks over tests that
merely copy implementation arithmetic.

| Area | Scenarios and expected properties |
| --- | --- |
| Formula | Male/female branches, activity normalization, changing current weight, explicit fallback |
| Dates | Daily, every-other-day, weekly, long gaps, leap day, out-of-order import, exactly 7/8/14/15 records, range boundaries |
| Corrections | Same-day replacement is idempotent; old corrections rebuild later trends and invalidate estimates; deletion does the same if supported |
| Intake | Complete, partial, estimated, unknown, confirmed zero, legacy unclassified, multiple users; no missing-to-zero conversion |
| Migration | Empty DB, existing DB, repeated migration, failed migration rollback, preserved food/planning records, deliberate legacy classification |
| Estimation | Known stable expenditure; synthetic loss/gain; noisy weigh-ins; sparse weights; random gaps; systematically missing high-intake days; activity changes |
| Freshness | Old saved estimates, new inputs after estimate, no recent weights, long logging break, repeat analysis, historic `as_of` analysis |
| Goals | Loss, gain, maintenance; both sides of target; tolerance boundaries; custom rates; target reached/crossed; effective-date transitions |
| Policy | Abstention, formula-only initialization, bounds, review cadence, uncertainty-related holds, unsupported requests |
| Agent API | Valid JSON on success/error, stable codes and units, null vs zero, invalid/nonfinite values, nonzero error exit status, no prompts |
| Independence | Fresh zero-food DB; recommendation path never invokes optimizer, food loader, or recipe functions |
| Integration | Optional meal adapter uses shared target; explicit constraints retain documented precedence; planned meals never become actual intake |

Do not assert that a statistical model can recover true expenditure under every
missingness pattern. Demonstrate degradation, disclose uncertainty, and test
abstention when the observations cannot support a reliable adjustment.

## Validation commands and completion evidence

Review task definitions before running them. Current intended commands:

```bash
mise trust mise.toml
mise install
mise run sync
mise run test
mise run lint
mise run format-check
mise run typecheck
mise run build
git diff --check
```

Use `mise run test -- -k <selection>` for focused regression work, then the full
suite. Do not use `format` as a read-only check; it edits files. Do not update
dependency locks unless the implementation requires a dependency change.

At planning time, mise is absent from this session and PyPI dependency installation
failed because of DNS resolution. Full tests have not been run. Treat that as
an environment limitation to recheck later, not evidence of code correctness.
The earlier assessment used stdlib-only in-memory checks; no production database
or measured personal data was read or changed.

Definition of done: all authorized phase acceptance criteria pass; regression
evidence is recorded; migrations are repeatable and preserve existing data;
standalone CLI examples run on a fresh isolated database; uncertainties and
baseline failures are disclosed; no unauthorized commit, deployment, or real-data
migration has occurred.

## Implementation evidence log

### 2026-10-23 — initial implementation pass (P0–P4 core)

Authorization: the owner asked for a review and an implementation attempt in the
current session. Work was isolated on branch
`feature/standalone-calorie-recommendations` in a Git worktree at
`/home/paseo/projects/.worktrees/diet-optimization-standalone-calories`; the
unrelated mise edits in the original checkout were left untouched.

Changed areas:

- `src/llmn/db/migrations.py`, `schema.py`, `connection.py`: versioned,
  savepoint-backed migrations; intake, goal, and activity tables; estimate
  metadata; legacy calorie backfill as `legacy_unclassified`.
- `src/llmn/tracking/models.py`, `queries.py`: dated intake observations,
  effective-dated goals/activity, weight correction rebuilds, calendar ranges,
  legacy classification.
- `src/llmn/tracking/formula.py`, `estimation.py`, `recommendations.py`,
  `service.py`, `policy.py`, `errors.py`: formula normalization, coverage-aware
  estimation, bounded policy, explicit holds, structured results.
- `src/llmn/profiles/body_calc.py`, `tracking/diagnostics.py`: sex/activity
  normalization and calendar-aware reports; progress now reads measured actual
  intake.
- `src/llmn/cli.py`, `cli_tracking.py`: `intake`, `goal`, `activity` command
  groups; `weight delete`; repaired `tdee estimate`/`targets`/`progress`.
- `docs/tracking-policy.md`, `README.md`, `CLAUDE.md`,
  `.claude/skills/tracking/SKILL.md`: policy, commands, and agent semantics.

Commands run and results:

```text
mise exec -- uv sync --locked --extra dev          # succeeded in worktree
mise exec -- uv run --no-sync pytest tests/test_tracking_schema.py   tests/test_tracking_service.py tests/test_tracking_cli.py -q
  # 38 passed
python -m pytest tests/ -q                        # 142 passed, 10 failed
  # the 10 failures are the pre-existing test_templates failures (missing foods table)
mise exec -- uv run --no-sync ruff check <new/changed new files>  # passed
mise exec -- uv run --no-sync ruff format --check <new files>     # passed
mise exec -- uv run --no-sync mypy <new modules>                  # passed
```

Limitations and deliberate changes:

- The observed rate uses a raw-weight least-squares slope rather than EMA
  endpoints; the EMA initialization lag biased a short first window. Stored EMA
  remains the display trend.
- The estimator uses consecutive 7-calendar-day segments and passes segment
  coverage into the filter, fixing the old row-count/terminal-window behavior.
- `calorie_log` remains a compatibility table. `calories log` is still explicitly
  planned intake and writes both a legacy row and a `planned` intake row.
- Existing broader lint/typecheck failures remain outside this change. The
  pre-existing `test_templates.py` failures are unchanged.
- P2.4 backup/recovery runbook, P3.5 realistic-missingness comparison, and the
  remaining P5 meal-planning integration work are open. A `--help` invocation
  initialized an empty, schema-only `~/.llmn/llmn.db`; it contained no user rows
  and was moved to `/tmp/llmn-db-schema-only-backup-*.db` after validation.
  No commit had been made when this evidence entry was recorded.
