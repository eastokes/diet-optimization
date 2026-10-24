"""Tests for tracking corrections, coverage, estimation, and recommendations."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from llmn.db.connection import DatabaseConnection
from llmn.tracking import policy
from llmn.tracking.diagnostics import calculate_mifflin_tdee
from llmn.tracking.ema import update_trend
from llmn.tracking.errors import GoalValidationError
from llmn.tracking.estimation import AnalysisWindow, summarize_intake
from llmn.tracking.formula import BASELINE_MEASURED_RMR, baseline_expenditure
from llmn.tracking.models import GoalPeriod, IntakeEntry, UserProfile
from llmn.tracking.queries import (
    GoalQueries,
    IntakeQueries,
    TDEEQueries,
    UserQueries,
    WeightQueries,
)
from llmn.tracking.recommendations import GoalSpec, validate_goal
from llmn.tracking.service import (
    analyze,
    analyze_and_persist,
    current_calculation_weight,
)


def make_user(
    conn,
    *,
    sex: str = "male",
    weight_lbs: float = 185.0,
    activity: str = "moderate",
    goal: str | None = None,
    target: float | None = None,
) -> int:
    profile = UserProfile(
        user_id=None,
        age=38,
        sex=sex,
        height_inches=72,
        weight_lbs=weight_lbs,
        activity_level=activity,
        goal=goal,
        target_weight_lbs=target,
    )
    return UserQueries.create_user(conn, profile)


def add_scenario(
    conn,
    user_id: int,
    *,
    start: date = date(2026, 1, 1),
    days: int = 28,
    start_weight: float = 185.0,
    expenditure: float = 2500.0,
    intake: float = 2000.0,
    daily_delta: float | None = None,
) -> None:
    delta = daily_delta if daily_delta is not None else (intake - expenditure) / 3500.0
    for index in range(days):
        day = start + timedelta(days=index)
        WeightQueries.add_weight(conn, user_id, start_weight + index * delta, day)
        IntakeQueries.upsert(
            conn,
            user_id,
            day,
            intake,
            kind="actual",
            completeness="complete",
            quality="measured",
        )


def set_goal(conn, user_id: int, mode: str, rate: float, target: float | None = None) -> None:
    GoalQueries.set_goal(
        conn,
        GoalPeriod(
            goal_id=None,
            user_id=user_id,
            effective_from=date(2026, 1, 1),
            mode=mode,
            rate_lbs_per_week=rate,
            target_weight_lbs=target,
        ),
    )


class TestFormulaAndCorrections:
    def test_male_and_female_formula_branches_b1(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            male_id = make_user(conn, sex="male")
            female_id = make_user(conn, sex="female")
            male = UserQueries.get_user(conn, male_id)
            female = UserQueries.get_user(conn, female_id)
            assert male is not None and female is not None

            assert calculate_mifflin_tdee(male) == pytest.approx(2785.58, abs=0.1)
            assert calculate_mifflin_tdee(female) == pytest.approx(2528.28, abs=0.1)

    def test_elapsed_day_ema_persisted_b3(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            WeightQueries.add_weight(conn, user_id, 185.0, date(2026, 1, 1))
            entry = WeightQueries.add_weight(conn, user_id, 181.0, date(2026, 1, 8))

        expected = update_trend(185.0, 181.0, days_elapsed=7)
        assert entry.trend_lbs == pytest.approx(expected)
        assert entry.trend_lbs == pytest.approx(182.9132, abs=0.001)

    def test_correction_rebuilds_later_trends(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            WeightQueries.add_weight(conn, user_id, 185.0, date(2026, 1, 1))
            WeightQueries.add_weight(conn, user_id, 184.0, date(2026, 1, 2))
            WeightQueries.add_weight(conn, user_id, 183.0, date(2026, 1, 3))

            WeightQueries.add_weight(conn, user_id, 180.0, date(2026, 1, 2))
            history = WeightQueries.get_weight_history(conn, user_id, end_date=date(2026, 1, 3))

        expected_day_2 = update_trend(185.0, 180.0, days_elapsed=1)
        expected_day_3 = update_trend(expected_day_2, 183.0, days_elapsed=1)
        assert history[1].trend_lbs == pytest.approx(expected_day_2)
        assert history[2].trend_lbs == pytest.approx(expected_day_3)

    def test_shuffled_backfill_matches_chronological_import(
        self, temp_db: DatabaseConnection
    ) -> None:
        with temp_db.get_connection() as conn:
            sequential_id = make_user(conn)
            shuffled_id = make_user(conn)
            measurements = {
                date(2026, 1, 1): 185.0,
                date(2026, 1, 4): 184.2,
                date(2026, 1, 9): 182.9,
                date(2026, 1, 16): 181.8,
            }
            for day, weight in measurements.items():
                WeightQueries.add_weight(conn, sequential_id, weight, day)
            for day, weight in reversed(list(measurements.items())):
                WeightQueries.add_weight(conn, shuffled_id, weight, day)

            sequential = WeightQueries.get_weight_history(
                conn, sequential_id, end_date=date(2026, 1, 16)
            )
            shuffled = WeightQueries.get_weight_history(
                conn, shuffled_id, end_date=date(2026, 1, 16)
            )

        assert [round(entry.trend_lbs, 9) for entry in sequential] == [
            round(entry.trend_lbs, 9) for entry in shuffled
        ]

    def test_current_weight_uses_recent_trend_or_profile_fallback(
        self, temp_db: DatabaseConnection
    ) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            WeightQueries.add_weight(conn, user_id, 190.0, date(2026, 1, 1))

            weight, source, latest = current_calculation_weight(conn, user_id, date(2026, 1, 10))
            assert source == "recent_trend"
            assert weight == pytest.approx(190.0)
            assert latest == date(2026, 1, 1)

            fallback_weight, fallback_source, _ = current_calculation_weight(
                conn, user_id, date(2026, 2, 10)
            )
            profile = UserQueries.get_user(conn, user_id)
            assert profile is not None
            assert fallback_source == "profile_fallback"
            assert fallback_weight == profile.weight_lbs
            assert profile.weight_lbs == 185.0

    def test_weight_history_uses_calendar_range_b6(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            WeightQueries.add_weight(conn, user_id, 190.0, date(2026, 1, 1))
            WeightQueries.add_weight(conn, user_id, 185.0, date(2026, 3, 31))

            recent = WeightQueries.get_weight_history(
                conn, user_id, days=30, end_date=date(2026, 3, 31)
            )

        assert [entry.measured_at for entry in recent] == [date(2026, 3, 31)]


class TestInputValidation:
    def test_nonfinite_and_negative_intake_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), -1.0):
            with pytest.raises(ValueError):
                IntakeEntry(
                    intake_id=None,
                    user_id=1,
                    date=date(2026, 1, 1),
                    kcal=value,
                )

    def test_nonpositive_or_nonfinite_weight_is_rejected(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            for value in (0.0, -1.0, float("nan"), float("inf")):
                with pytest.raises(ValueError):
                    WeightQueries.add_weight(conn, user_id, value, date(2026, 1, 1))


class TestIntakeCoverage:
    def test_missing_days_are_unknown_not_zero(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            IntakeQueries.upsert(conn, user_id, date(2026, 1, 1), 2000)
            IntakeQueries.upsert(
                conn,
                user_id,
                date(2026, 1, 2),
                500,
                completeness="partial",
            )
            entries = IntakeQueries.get_entries(conn, user_id)

        summary = summarize_intake(entries, AnalysisWindow(date(2026, 1, 1), date(2026, 1, 7)))
        assert summary.mean_kcal == pytest.approx(2000.0)
        assert summary.complete_days == 1
        assert summary.partial_days == 1
        assert summary.unknown_days == 5
        assert summary.weighted_coverage == pytest.approx(1 / 7)

    def test_estimated_intake_is_down_weighted(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            IntakeQueries.upsert(conn, user_id, date(2026, 1, 1), 2000)
            IntakeQueries.upsert(
                conn,
                user_id,
                date(2026, 1, 2),
                3000,
                quality="estimated",
            )
            entries = IntakeQueries.get_entries(conn, user_id)

        summary = summarize_intake(entries, AnalysisWindow(date(2026, 1, 1), date(2026, 1, 2)))
        assert summary.estimated_days == 1
        assert summary.mean_kcal == pytest.approx((2000 + 1500) / 1.5)

    def test_legacy_rows_are_excluded_from_learning(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            IntakeQueries.upsert(
                conn,
                user_id,
                date(2026, 1, 1),
                1900,
                kind="legacy_unclassified",
                completeness="unknown",
                quality="unknown",
            )
            entries = IntakeQueries.get_entries(conn, user_id)
            analysis = analyze(conn, user_id, date(2026, 1, 28), 28)

        summary = summarize_intake(entries, AnalysisWindow(date(2026, 1, 1), date(2026, 1, 28)))
        assert summary.mean_kcal is None
        assert summary.legacy_unclassified_days == 1
        assert "LEGACY_INTAKE_UNCLASSIFIED" in analysis.reason_codes
        assert analysis.estimate.method == "formula_only"


class TestEstimation:
    def test_known_expenditure_is_learned(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            set_goal(conn, user_id, "loss", -1.0)
            add_scenario(conn, user_id, expenditure=2500.0, intake=2000.0)
            analysis = analyze(conn, user_id, date(2026, 1, 28), 28)

        assert analysis.estimate.method == "learned"
        assert analysis.estimate.segments_used == 4
        assert analysis.estimate.kcal_per_day is not None
        assert analysis.estimate.kcal_per_day == pytest.approx(2500, abs=250)
        assert analysis.estimate.observed_rate_lbs_per_week == pytest.approx(-1.0, abs=0.05)
        assert analysis.recommendation.assessment == "on_track"
        assert analysis.recommendation.action == "keep"
        assert analysis.recommendation.change_kcal_per_day == 0

    def test_repeated_analysis_is_deterministic_and_read_only(
        self, temp_db: DatabaseConnection
    ) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            set_goal(conn, user_id, "loss", -1.0)
            add_scenario(conn, user_id)
            first = analyze(conn, user_id, date(2026, 1, 28), 28)
            before = [
                entry.trend_lbs
                for entry in WeightQueries.get_weight_history(
                    conn, user_id, end_date=date(2026, 1, 28)
                )
            ]
            second = analyze(conn, user_id, date(2026, 1, 28), 28)
            after = [
                entry.trend_lbs
                for entry in WeightQueries.get_weight_history(
                    conn, user_id, end_date=date(2026, 1, 28)
                )
            ]
            persisted = TDEEQueries.get_latest_estimate(conn, user_id)

        assert first.to_dict() == second.to_dict()
        assert first.estimate.fingerprint == second.estimate.fingerprint
        assert before == after
        assert persisted is None

    def test_persisted_snapshot_carries_evidence(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            set_goal(conn, user_id, "loss", -1.0)
            add_scenario(conn, user_id)
            analysis = analyze_and_persist(conn, user_id, date(2026, 1, 28), 28)
            persisted = TDEEQueries.get_latest_estimate(conn, user_id)

        assert persisted is not None
        assert persisted.method == "learned"
        assert persisted.interval_start == analysis.window.start
        assert persisted.interval_end == analysis.window.end
        assert persisted.input_fingerprint == analysis.estimate.fingerprint
        assert persisted.complete_intake_days == 28


class TestRecommendationPolicy:
    @pytest.mark.parametrize(
        ("intake", "expected_assessment", "expected_action", "expected_change"),
        [
            (2000.0, "on_track", "keep", 0),
            (1500.0, "intake_below_goal", "increase_intake", 250),
            (2500.0, "intake_above_goal", "decrease_intake", -250),
        ],
    )
    def test_loss_goal_both_directions(
        self,
        temp_db: DatabaseConnection,
        intake: float,
        expected_assessment: str,
        expected_action: str,
        expected_change: int,
    ) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            set_goal(conn, user_id, "loss", -1.0)
            add_scenario(conn, user_id, expenditure=2500.0, intake=intake)
            analysis = analyze(conn, user_id, date(2026, 1, 28), 28)

        assert analysis.recommendation.assessment == expected_assessment
        assert analysis.recommendation.action == expected_action
        assert analysis.recommendation.change_kcal_per_day == expected_change

    @pytest.mark.parametrize(
        ("intake", "expected_assessment", "expected_action"),
        [
            (2000.0, "intake_below_goal", "increase_intake"),
            (3000.0, "intake_above_goal", "decrease_intake"),
        ],
    )
    def test_maintenance_goal_both_directions(
        self,
        temp_db: DatabaseConnection,
        intake: float,
        expected_assessment: str,
        expected_action: str,
    ) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            set_goal(conn, user_id, "maintenance", 0.0)
            add_scenario(conn, user_id, expenditure=2500.0, intake=intake)
            analysis = analyze(conn, user_id, date(2026, 1, 28), 28)

        assert analysis.recommendation.assessment == expected_assessment
        assert analysis.recommendation.action == expected_action

    @pytest.mark.parametrize(
        ("intake", "expected_assessment", "expected_action"),
        [
            (2500.0, "intake_below_goal", "increase_intake"),
            (3500.0, "intake_above_goal", "decrease_intake"),
        ],
    )
    def test_gain_goal_both_directions(
        self,
        temp_db: DatabaseConnection,
        intake: float,
        expected_assessment: str,
        expected_action: str,
    ) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            set_goal(conn, user_id, "gain", 0.5)
            add_scenario(conn, user_id, expenditure=2500.0, intake=intake)
            analysis = analyze(conn, user_id, date(2026, 1, 28), 28)

        assert analysis.recommendation.assessment == expected_assessment
        assert analysis.recommendation.action == expected_action

    def test_target_reached_holds_adjustment(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn, weight_lbs=168.0)
            set_goal(conn, user_id, "loss", -1.0, target=165.0)
            add_scenario(
                conn,
                user_id,
                start_weight=168.0,
                expenditure=2500.0,
                intake=1800.0,
                daily_delta=-0.2,
            )
            analysis = analyze(conn, user_id, date(2026, 1, 28), 28)

        assert analysis.recommendation.assessment == "target_reached"
        assert analysis.recommendation.action == "transition_to_maintenance"
        assert analysis.recommendation.target_kcal_per_day is None

    def test_no_history_returns_formula_only_initialization(
        self, temp_db: DatabaseConnection
    ) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            set_goal(conn, user_id, "maintenance", 0.0)
            analysis = analyze(conn, user_id, date(2026, 1, 28), 28)

        recommendation = analysis.recommendation
        assert analysis.estimate.method == "formula_only"
        assert recommendation.assessment == "insufficient_data"
        assert recommendation.action == "initialize_formula_only"
        assert recommendation.target_kcal_per_day is not None
        assert recommendation.target_kcal_per_day >= 1500

    def test_partial_history_holds_instead_of_guessing(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            set_goal(conn, user_id, "loss", -1.0)
            WeightQueries.add_weight(conn, user_id, 185.0, date(2026, 1, 1))
            IntakeQueries.upsert(conn, user_id, date(2026, 1, 1), 2000)
            analysis = analyze(conn, user_id, date(2026, 1, 28), 28)

        assert analysis.estimate.method == "formula_only"
        assert analysis.recommendation.assessment == "insufficient_data"
        assert analysis.recommendation.action == "hold"
        assert analysis.recommendation.target_kcal_per_day is None
        assert "LOW_INTAKE_COVERAGE" in analysis.reason_codes

    def test_unsupported_goal_rate_is_rejected(self) -> None:
        with pytest.raises(GoalValidationError) as excinfo:
            validate_goal(GoalSpec(mode="loss", rate_lbs_per_week=1.0))
        assert excinfo.value.code == "UNSUPPORTED_GOAL_RATE"

    def test_no_goal_requests_a_goal(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            add_scenario(conn, user_id)
            analysis = analyze(conn, user_id, date(2026, 1, 28), 28)

        assert analysis.recommendation.assessment == "no_goal"
        assert analysis.recommendation.action == "set_goal"
        assert "NO_GOAL_SET" in analysis.reason_codes


class TestBirthdateAndMeasuredRMR:
    def test_age_is_derived_from_birthdate(self) -> None:
        profile = UserProfile(
            user_id=1,
            age=44,
            sex="male",
            height_inches=73,
            weight_lbs=176.9,
            activity_level="moderate",
            birthdate=date(1981, 11, 1),
        )
        assert profile.age_on(date(2026, 10, 31)) == 44
        assert profile.age_on(date(2026, 11, 1)) == 45
        assert profile.age_on(date(2027, 10, 31)) == 45
        assert profile.age_on(date(2027, 11, 1)) == 46

    def test_stored_age_is_used_when_birthdate_is_unknown(self) -> None:
        profile = UserProfile(
            user_id=1,
            age=38,
            sex="female",
            height_inches=65,
            weight_lbs=140,
            activity_level="lightly_active",
        )
        assert profile.age_on(date(2026, 1, 1)) == 38

    def test_measured_rmr_replaces_mifflin_resting_component(
        self, temp_db: DatabaseConnection
    ) -> None:
        with temp_db.get_connection() as conn:
            profile = UserProfile(
                user_id=None,
                age=44,
                sex="male",
                height_inches=73,
                weight_lbs=176.9,
                activity_level="moderate",
                resting_metabolic_rate=1850.0,
                rmr_measured_at=date(2026, 9, 17),
                rmr_source="dexa",
            )
            user_id = UserQueries.create_user(conn, profile)
            stored = UserQueries.get_user(conn, user_id)
        assert stored is not None
        assert stored.has_measured_rmr
        assert stored.resting_metabolic_rate == pytest.approx(1850.0)

        baseline = baseline_expenditure(stored, 176.9, "moderate", date(2026, 10, 24))
        assert baseline.source == BASELINE_MEASURED_RMR
        assert baseline.resting_source == "measured_rmr"
        assert baseline.resting_kcal == pytest.approx(1850.0)
        assert baseline.kcal_per_day == pytest.approx(1850.0 * 1.55, abs=0.5)

    def test_measured_rmr_is_used_by_the_estimate(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            user_id = make_user(conn)
            profile = UserQueries.get_user(conn, user_id)
            assert profile is not None
            profile.resting_metabolic_rate = 1850.0
            profile.rmr_measured_at = date(2026, 9, 17)
            profile.rmr_source = "dexa"
            UserQueries.update_user(conn, profile)
            analysis = analyze(conn, user_id, date(2026, 1, 20), 14)

        estimate = analysis.estimate
        assert estimate.method == "formula_only"
        assert estimate.baseline_source == "measured_rmr"
        assert estimate.resting_kcal == pytest.approx(1850.0)
        assert estimate.baseline_kcal == pytest.approx(1850.0 * 1.55, abs=0.5)
        assert estimate.uncertainty_95ci == pytest.approx(policy.MEASURED_RMR_UNCERTAINTY_95CI)

    def test_invalid_measured_rmr_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            UserProfile(
                user_id=None,
                age=44,
                sex="male",
                height_inches=73,
                weight_lbs=176.9,
                activity_level="moderate",
                resting_metabolic_rate=-10.0,
            )
