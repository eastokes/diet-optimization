"""End-to-end CLI tests for standalone calorie tracking and recommendations."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from llmn.cli import app
from llmn.db.connection import DatabaseConnection, set_db
from llmn.tracking.queries import (
    CalorieQueries,
    GoalQueries,
    IntakeQueries,
    TDEEQueries,
    UserQueries,
)

runner = CliRunner()


@pytest.fixture
def cli_db(temp_db: DatabaseConnection):
    set_db(temp_db)
    yield temp_db
    set_db(None)


def run_json(args: list[str], expected_exit: int = 0) -> dict:
    result = runner.invoke(app, args)
    assert result.exit_code == expected_exit, result.output
    payload = json.loads(result.stdout)
    assert "NaN" not in result.stdout
    assert "Infinity" not in result.stdout
    return payload


def create_user() -> None:
    payload = run_json(
        [
            "user",
            "create",
            "--age",
            "38",
            "--sex",
            "male",
            "--height",
            "72",
            "--weight",
            "185",
            "--activity",
            "moderate",
            "--json",
        ]
    )
    assert payload["success"] is True


def recent_dates(days: int) -> tuple[date, date, list[date]]:
    as_of = date.today() - timedelta(days=1)
    start = as_of - timedelta(days=days - 1)
    return start, as_of, [start + timedelta(days=index) for index in range(days)]


def seed_through_cli(
    start: date,
    days: int,
    *,
    intake: float = 2000.0,
    start_weight: float = 185.0,
    expenditure: float = 2500.0,
) -> None:
    delta = (intake - expenditure) / 3500.0
    for index in range(days):
        day = start + timedelta(days=index)
        run_json(
            [
                "weight",
                "add",
                f"{start_weight + index * delta:.4f}",
                "--date",
                day.isoformat(),
                "--json",
            ]
        )
        run_json(
            [
                "intake",
                "log",
                f"{intake:.0f}",
                "--date",
                day.isoformat(),
                "--kind",
                "actual",
                "--json",
            ]
        )


class TestStandaloneFlow:
    def test_targets_work_on_zero_food_database(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, as_of, dates = recent_dates(15)
        run_json(
            [
                "goal",
                "set",
                "--mode",
                "loss",
                "--rate",
                "-1.0",
                "--target",
                "165",
                "--effective-from",
                start.isoformat(),
                "--json",
            ]
        )
        seed_through_cli(start, len(dates))

        payload = run_json(
            [
                "tdee",
                "targets",
                "--as-of",
                as_of.isoformat(),
                "--window-days",
                "15",
                "--json",
            ]
        )

        assert payload["success"] is True
        assert payload["schema_version"] == 2
        data = payload["data"]
        assert data["policy_version"] == "v1"
        assert data["analysis_window"]["start"] == start.isoformat()
        assert data["analysis_window"]["end"] == as_of.isoformat()
        assert data["assessment"] == "on_track"
        assert data["goal"]["mode"] == "loss"
        assert data["goal"]["rate_lbs_per_week"] == -1.0
        assert data["goal_description"].startswith("loss")
        assert data["expenditure"]["method"] == "learned"
        assert data["expenditure"]["kcal_per_day"] == pytest.approx(2500, abs=300)
        assert data["recommendation"]["target_kcal_per_day"] == 2000
        assert data["recommended_intake"] == 2000
        assert data["recommendation"]["change_kcal_per_day"] == 0
        assert data["evidence"]["complete_intake_days"] == len(dates)

        with cli_db.get_connection() as conn:
            assert cli_db.get_table_count("foods") == 0
            assert UserQueries.get_default_user(conn) is not None

    def test_planned_calories_are_not_actual_intake(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, as_of, _ = recent_dates(15)
        run_json(
            [
                "goal",
                "set",
                "--mode",
                "maintenance",
                "--effective-from",
                start.isoformat(),
                "--json",
            ]
        )
        payload = run_json(
            [
                "calories",
                "log",
                "1900",
                "--date",
                start.isoformat(),
                "--json",
            ]
        )
        assert payload["data"]["kind"] == "planned"

        targets = run_json(
            [
                "tdee",
                "targets",
                "--as-of",
                as_of.isoformat(),
                "--window-days",
                "15",
                "--json",
            ]
        )
        assert targets["data"]["assessment"] == "insufficient_data"
        assert targets["data"]["expenditure"]["method"] == "formula_only"
        assert "NO_ACTUAL_INTAKE" in targets["data"]["reason_codes"]
        assert targets["data"]["recommendation"]["action"] == "initialize_formula_only"
        assert targets["data"]["recommendation"]["target_kcal_per_day"] is not None

        listing = run_json(
            [
                "intake",
                "list",
                "--as-of",
                as_of.isoformat(),
                "--days",
                "15",
                "--json",
            ]
        )
        assert listing["data"]["coverage"]["complete_days"] == 0
        assert listing["data"]["coverage"]["mean_kcal_per_day"] is None

    def test_estimate_persists_and_progress_uses_analysis(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, as_of, dates = recent_dates(15)
        run_json(
            [
                "goal",
                "set",
                "--mode",
                "loss",
                "--rate",
                "-1.0",
                "--effective-from",
                start.isoformat(),
                "--json",
            ]
        )
        seed_through_cli(start, len(dates))

        estimate = run_json(["tdee", "estimate", "--as-of", as_of.isoformat(), "--json"])
        assert estimate["data"]["persisted"] is True
        assert estimate["data"]["input_fingerprint"]
        with cli_db.get_connection() as conn:
            persisted = TDEEQueries.get_latest_estimate(conn, 1)
        assert persisted is not None
        assert persisted.method == "learned"

        progress = run_json(
            ["tdee", "progress", "--as-of", as_of.isoformat(), "--days", "15", "--json"]
        )
        assert progress["data"]["analysis"]["assessment"] == "on_track"
        assert progress["data"]["tdee"]["avg_actual_calories"] == pytest.approx(2000)


class TestCorrectionsAndMigrationCLI:
    def test_weight_validation_is_structured(self, cli_db: DatabaseConnection) -> None:
        create_user()
        negative = run_json(["weight", "add", "--json", "--", "-5"], expected_exit=1)
        assert negative["success"] is False
        assert "INVALID_WEIGHT" in negative["error_codes"]

        future = run_json(
            [
                "weight",
                "add",
                "180",
                "--date",
                (date.today() + timedelta(days=1)).isoformat(),
                "--json",
            ],
            expected_exit=1,
        )
        assert future["success"] is False
        assert "FUTURE_DATE" in future["error_codes"]

    def test_unsupported_goal_is_rejected(self, cli_db: DatabaseConnection) -> None:
        create_user()
        payload = run_json(
            ["goal", "set", "--mode", "loss", "--rate", "1.0", "--json"],
            expected_exit=1,
        )
        assert payload["success"] is False
        assert "UNSUPPORTED_GOAL_RATE" in payload["error_codes"]

    def test_invalid_json_input_has_nonzero_exit(self, cli_db: DatabaseConnection) -> None:
        create_user()
        payload = run_json(
            ["tdee", "targets", "--as-of", "not-a-date", "--json"],
            expected_exit=1,
        )
        assert payload["success"] is False
        assert payload["error_codes"]

    def test_delete_weight_rebuilds_later_trends(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, _, _ = recent_dates(3)
        for index, weight in enumerate([185.0, 184.0, 183.0]):
            run_json(
                [
                    "weight",
                    "add",
                    str(weight),
                    "--date",
                    (start + timedelta(days=index)).isoformat(),
                    "--json",
                ]
            )
        run_json(
            [
                "weight",
                "delete",
                "--date",
                (start + timedelta(days=1)).isoformat(),
                "--json",
            ]
        )

        with cli_db.get_connection() as conn:
            history = conn.execute(
                "SELECT weight_lbs, trend_lbs FROM weight_log ORDER BY measured_at"
            ).fetchall()
        assert [row[0] for row in history] == [185.0, 183.0]
        assert history[1][1] < history[0][1]

    def test_legacy_confirmation_previews_then_applies(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, _, _ = recent_dates(5)
        with cli_db.get_connection() as conn:
            profile = UserQueries.get_default_user(conn)
            assert profile is not None and profile.user_id is not None
            IntakeQueries.upsert(
                conn,
                profile.user_id,
                start,
                1900,
                kind="legacy_unclassified",
                completeness="unknown",
                quality="unknown",
            )

        preview = run_json(["intake", "confirm-legacy", "--all", "--json"])
        assert preview["data"]["applied"] is False
        assert preview["data"]["preview_dates"] == [start.isoformat()]

        applied = run_json(["intake", "confirm-legacy", "--all", "--apply", "--json"])
        assert applied["data"]["updated_dates"] == [start.isoformat()]

        with cli_db.get_connection() as conn:
            row = IntakeQueries.get_entries(conn, 1, start, start, kind="actual")
        assert len(row) == 1
        assert row[0].provenance == "confirmed_legacy"

    def test_goal_show_and_activity_context(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, as_of, _ = recent_dates(5)
        run_json(
            [
                "goal",
                "set",
                "--mode",
                "maintenance",
                "--effective-from",
                start.isoformat(),
                "--activity",
                "active",
                "--json",
            ]
        )
        goal = run_json(["goal", "show", "--as-of", as_of.isoformat(), "--json"])
        activity = run_json(["activity", "show", "--as-of", as_of.isoformat(), "--json"])
        assert goal["data"]["goal"]["mode"] == "maintenance"
        assert goal["data"]["goal"]["rate_lbs_per_week"] == 0
        assert activity["data"]["activity_level"] == "active"
        assert activity["data"]["source"] == "activity_period"

    def test_calories_legacy_table_and_intake_table_stay_in_sync(
        self, cli_db: DatabaseConnection
    ) -> None:
        create_user()
        start, _, _ = recent_dates(5)
        run_json(["calories", "log", "1850", "--date", start.isoformat(), "--json"])
        with cli_db.get_connection() as conn:
            legacy = CalorieQueries.get_calorie_history(conn, 1)
            planned = IntakeQueries.get_entries(conn, 1, start, start, kind="planned")
            goals = GoalQueries.get_history(conn, 1)
        assert legacy[0].planned_calories == 1850
        assert planned[0].kcal == 1850
        assert planned[0].quality == "estimated"
        assert goals == []


class TestWriteFeedbackAndListOutput:
    def test_intake_log_reports_revision_and_replacement(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, _, _ = recent_dates(5)

        first = run_json(["intake", "log", "2000", "--date", start.isoformat(), "--json"])
        assert first["data"]["replaced"] is False
        assert first["data"]["previous"] is None
        assert first["data"]["entry"]["revision"] == 1

        replaced = run_json(
            [
                "intake",
                "log",
                "2100",
                "--date",
                start.isoformat(),
                "--quality",
                "estimated",
                "--json",
            ]
        )
        data = replaced["data"]
        assert data["replaced"] is True
        assert data["previous"] == {
            "kcal": 2000.0,
            "quality": "measured",
            "completeness": "complete",
            "revision": 1,
        }
        assert data["entry"]["kcal"] == 2100.0
        assert data["entry"]["quality"] == "estimated"
        assert data["entry"]["revision"] == 2
        assert any("half weight" in warning for warning in replaced["warnings"])

    def test_intake_log_human_output_names_the_replacement(
        self, cli_db: DatabaseConnection
    ) -> None:
        create_user()
        start, _, _ = recent_dates(5)
        runner.invoke(app, ["intake", "log", "2000", "--date", start.isoformat()])
        result = runner.invoke(app, ["intake", "log", "2100", "--date", start.isoformat()])
        assert result.exit_code == 0
        assert "revision 2" in result.stdout
        assert "Replaced the earlier 2000 kcal entry" in result.stdout

    def test_intake_list_explains_weighted_coverage(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, as_of, _ = recent_dates(10)
        run_json(["intake", "log", "2000", "--date", start.isoformat(), "--json"])
        run_json(
            [
                "intake",
                "log",
                "2100",
                "--date",
                (start + timedelta(days=1)).isoformat(),
                "--quality",
                "estimated",
                "--json",
            ]
        )

        payload = run_json(
            ["intake", "list", "--as-of", as_of.isoformat(), "--days", "10", "--json"]
        )
        coverage = payload["data"]["coverage"]
        assert coverage["window_days"] == 10
        assert coverage["measured_equivalent_days"] == pytest.approx(1.5)
        assert coverage["weighted_coverage"] == pytest.approx(0.15)
        assert coverage["estimated_days"] == 1
        assert "measured-equivalent" in coverage["coverage_explanation"]
        assert coverage["mean_kcal_basis"].startswith("mean of logged")

        human = runner.invoke(app, ["intake", "list", "--as-of", as_of.isoformat(), "--days", "10"])
        assert human.exit_code == 0
        assert "1.5 measured-equivalent day(s)" in human.stdout
        assert "not counted as zero" in human.stdout
        assert "Mean over logged complete days" in human.stdout
        assert start.isoformat() in human.stdout

    def test_weight_add_reports_replacement_and_rebuilds(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, _, _ = recent_dates(5)
        days = [start + timedelta(days=index) for index in range(3)]
        run_json(["weight", "add", "185", "--date", days[0].isoformat(), "--json"])
        run_json(["weight", "add", "184", "--date", days[1].isoformat(), "--json"])
        run_json(["weight", "add", "183", "--date", days[2].isoformat(), "--json"])

        replaced = run_json(["weight", "add", "184.5", "--date", days[1].isoformat(), "--json"])
        data = replaced["data"]
        assert data["replaced"] is True
        assert data["replaced_weight_lbs"] == pytest.approx(184.0)
        assert data["previous_measurement_date"] == days[0].isoformat()
        assert data["days_since_previous"] == 1
        assert data["trend_change_lbs"] == pytest.approx(-0.05, abs=0.01)
        assert data["later_entries_rebuilt"] == 1

        backfill = run_json(["weight", "add", "184.2", "--date", days[0].isoformat(), "--json"])
        assert backfill["data"]["replaced"] is True
        assert backfill["data"]["previous_measurement_date"] is None
        assert backfill["data"]["later_entries_rebuilt"] == 2

    def test_weight_list_avoids_negative_zero_delta(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, _, _ = recent_dates(4)
        for index, weight in enumerate([185.0, 185.02, 185.04]):
            run_json(
                [
                    "weight",
                    "add",
                    f"{weight:.2f}",
                    "--date",
                    (start + timedelta(days=index)).isoformat(),
                    "--json",
                ]
            )

        result = runner.invoke(app, ["weight", "list", "--days", "10"])
        assert result.exit_code == 0
        assert "-0.0" not in result.stdout
        assert "Trend change:" in result.stdout
        assert "Delta" not in result.stdout


class TestProgressReportConsistency:
    def test_progress_uses_one_window_for_weight_expenditure_and_intake(
        self, cli_db: DatabaseConnection
    ) -> None:
        create_user()
        start, as_of, dates = recent_dates(15)
        run_json(
            [
                "goal",
                "set",
                "--mode",
                "loss",
                "--rate",
                "-1.0",
                "--target",
                "165",
                "--effective-from",
                start.isoformat(),
                "--json",
            ]
        )
        seed_through_cli(start, len(dates))

        payload = run_json(
            ["tdee", "progress", "--as-of", as_of.isoformat(), "--days", "15", "--json"]
        )
        data = payload["data"]
        analysis = data["analysis"]

        assert data["window"]["days"] == 15
        assert data["window"]["start"] == start.isoformat()
        assert data["tdee"]["avg_actual_calories"] == pytest.approx(2000)
        assert data["tdee"]["complete_intake_days"] == len(dates)
        assert data["tdee"]["intake_coverage"] == pytest.approx(1.0)
        assert data["weight"]["measured_rate_lbs_per_week"] == pytest.approx(
            analysis["observed_rate_lbs_per_week"]
        )
        # The progress report must quote the same expenditure estimate that the
        # shared analysis (and therefore tdee targets) uses.
        assert data["tdee"]["baseline_kcal"] == pytest.approx(
            analysis["expenditure"]["baseline_kcal"], abs=0.5
        )
        assert data["tdee"]["adjusted_tdee"] == pytest.approx(
            analysis["expenditure"]["kcal_per_day"], abs=0.5
        )
        assert data["tdee"]["segments_used"] == analysis["expenditure"]["segments_used"]
        assert data["tdee"]["expected_deficit"] == pytest.approx(
            analysis["expenditure"]["kcal_per_day"] - 2000, abs=1
        )
        assert data["goal"]["rate_lbs_per_week"] == -1.0

    def test_progress_human_output_labels_both_rates(self, cli_db: DatabaseConnection) -> None:
        create_user()
        start, as_of, dates = recent_dates(15)
        run_json(
            [
                "goal",
                "set",
                "--mode",
                "maintenance",
                "--effective-from",
                start.isoformat(),
                "--json",
            ]
        )
        seed_through_cli(start, len(dates))
        result = runner.invoke(
            app, ["tdee", "progress", "--as-of", as_of.isoformat(), "--days", "15"]
        )
        assert result.exit_code == 0
        assert "EMA rate" in result.stdout
        assert "Measured rate" in result.stdout
        assert "least-squares over raw weights" in result.stdout
        assert f"{start.isoformat()} to {as_of.isoformat()}" in result.stdout
        assert "Coverage:" in result.stdout


class TestProfileBirthdateAndMeasuredRMR:
    def test_birthdate_create_and_show(self, cli_db: DatabaseConnection) -> None:
        birth = date(date.today().year - 44, 1, 1)
        payload = run_json(
            [
                "user",
                "create",
                "--birthdate",
                birth.isoformat(),
                "--sex",
                "male",
                "--height",
                "73",
                "--weight",
                "176.9",
                "--activity",
                "moderate",
                "--json",
            ]
        )
        profile = payload["data"]["profile"]
        assert profile["birthdate"] == birth.isoformat()
        assert profile["age"] == 44
        assert profile["age_source"] == "birthdate"
        assert profile["baseline_source"] == "mifflin"

        shown = run_json(["user", "show", "--json"])
        assert shown["data"]["birthdate"] == birth.isoformat()
        assert shown["data"]["age"] == 44
        assert shown["data"]["age_source"] == "birthdate"

    def test_missing_age_and_birthdate_is_structured(self, cli_db: DatabaseConnection) -> None:
        payload = run_json(
            [
                "user",
                "create",
                "--sex",
                "male",
                "--height",
                "73",
                "--weight",
                "176.9",
                "--json",
            ],
            expected_exit=1,
        )
        assert payload["success"] is False
        assert "MISSING_AGE" in payload["error_codes"]

    def test_invalid_birthdate_is_rejected(self, cli_db: DatabaseConnection) -> None:
        payload = run_json(
            [
                "user",
                "create",
                "--birthdate",
                (date.today() + timedelta(days=1)).isoformat(),
                "--sex",
                "male",
                "--height",
                "73",
                "--weight",
                "176.9",
                "--json",
            ],
            expected_exit=1,
        )
        assert "INVALID_BIRTHDATE" in payload["error_codes"]

    def test_measured_rmr_changes_the_baseline_and_can_be_cleared(
        self, cli_db: DatabaseConnection
    ) -> None:
        create_user()
        measured_on = (date.today() - timedelta(days=1)).isoformat()
        update = run_json(
            [
                "user",
                "update",
                "--rmr",
                "1850",
                "--rmr-date",
                measured_on,
                "--rmr-source",
                "dexa",
                "--json",
            ]
        )
        assert update["data"]["resting_metabolic_rate"] == pytest.approx(1850.0)
        assert update["data"]["rmr_source"] == "dexa"
        assert update["data"]["rmr_measured_at"] == measured_on
        assert update["data"]["baseline_source"] == "measured_rmr"

        estimate = run_json(["tdee", "estimate", "--json"])
        expenditure = estimate["data"]["expenditure"]
        assert expenditure["baseline_source"] == "measured_rmr"
        assert expenditure["resting_kcal"] == pytest.approx(1850.0)
        # test profile uses the default moderate activity multiplier (1.55)
        assert expenditure["baseline_kcal"] == pytest.approx(1850.0 * 1.55, abs=0.5)
        assert expenditure["uncertainty_95ci"] == pytest.approx(392.0, abs=0.5)

        cleared = run_json(["user", "update", "--clear-rmr", "--json"])
        assert cleared["data"]["resting_metabolic_rate"] is None
        assert cleared["data"]["baseline_source"] == "mifflin"

    def test_invalid_rmr_source_is_rejected(self, cli_db: DatabaseConnection) -> None:
        create_user()
        payload = run_json(
            [
                "user",
                "update",
                "--rmr",
                "1850",
                "--rmr-source",
                "vibes",
                "--json",
            ],
            expected_exit=1,
        )
        assert "INVALID_RMR_SOURCE" in payload["error_codes"]

    def test_rmr_source_without_rmr_is_rejected(self, cli_db: DatabaseConnection) -> None:
        create_user()
        payload = run_json(
            ["user", "update", "--rmr-source", "dexa", "--json"],
            expected_exit=1,
        )
        assert "MISSING_RMR" in payload["error_codes"]


class TestBodyCompositionCLI:
    def _add_scan(
        self,
        measured_at: date,
        *,
        weight: float,
        fat: float,
        lean: float,
        bone: float = 6.9,
        rmr: float | None = None,
    ) -> dict:
        args = [
            "bodycomp",
            "add",
            "--date",
            measured_at.isoformat(),
            "--method",
            "dexa",
            "--weight",
            str(weight),
            "--fat-mass",
            str(fat),
            "--lean-mass",
            str(lean),
            "--bone-mass",
            str(bone),
            "--json",
        ]
        if rmr is not None:
            args[args.index("--json") : args.index("--json")] = ["--rmr", str(rmr)]
        return run_json(args)

    def test_first_scan_derives_values_and_requests_a_second(
        self, cli_db: DatabaseConnection
    ) -> None:
        create_user()
        _, as_of, _ = recent_dates(2)
        payload = self._add_scan(as_of, weight=176.9, fat=44.7, lean=125.3)
        entry = payload["data"]["entry"]
        assert entry["fat_free_mass_lbs"] == pytest.approx(132.2)
        assert entry["body_fat_pct"] == pytest.approx(44.7 / 176.9 * 100, abs=0.1)
        assert payload["data"]["previous"] is None
        assert payload["data"]["comparison"] is None
        assert "first comparable measurement" in payload["human_summary"]

    def test_second_scan_reports_recomposition(self, cli_db: DatabaseConnection) -> None:
        create_user()
        as_of = date.today() - timedelta(days=1)
        start = as_of - timedelta(days=90)
        self._add_scan(start, weight=185.0, fat=45.0, lean=133.0)
        payload = self._add_scan(as_of, weight=183.0, fat=39.0, lean=137.0)

        comparison = payload["data"]["comparison"]
        assert comparison["classification"] == "recomposition"
        assert comparison["fat_mass_change_lbs"] == pytest.approx(-6.0)
        assert comparison["fat_free_mass_change_lbs"] == pytest.approx(4.0)
        assert comparison["weeks"] == pytest.approx(90 / 7, abs=0.1)
        assert "fat mass down 6.0 lbs" in payload["human_summary"]

    def test_scan_rmr_suggests_applying_it(self, cli_db: DatabaseConnection) -> None:
        create_user()
        _, as_of, _ = recent_dates(2)
        payload = self._add_scan(as_of, weight=176.9, fat=44.7, lean=125.3, rmr=1850)
        assert payload["data"]["entry"]["resting_metabolic_rate"] == pytest.approx(1850.0)
        assert any("llmn user update --rmr" in item for item in payload["suggestions"])

    def test_scan_requires_at_least_one_value(self, cli_db: DatabaseConnection) -> None:
        create_user()
        payload = run_json(
            ["bodycomp", "add", "--date", date.today().isoformat(), "--json"],
            expected_exit=1,
        )
        assert "MISSING_BODYCOMP_VALUES" in payload["error_codes"]

    def test_list_trend_and_delete(self, cli_db: DatabaseConnection) -> None:
        create_user()
        as_of = date.today() - timedelta(days=1)
        start = as_of - timedelta(days=60)
        self._add_scan(start, weight=185.0, fat=45.0, lean=133.0)
        self._add_scan(as_of, weight=183.0, fat=41.0, lean=135.0)

        listed = run_json(["bodycomp", "list", "--json"])
        assert len(listed["data"]["entries"]) == 2

        trend = run_json(["bodycomp", "trend", "--json"])
        assert trend["data"]["classification"] == "recomposition"
        assert trend["data"]["days"] == 60

        human = runner.invoke(app, ["bodycomp", "trend"])
        assert human.exit_code == 0
        assert "Body-composition trend" in human.stdout
        assert "Classification: recomposition" in human.stdout

        deleted = run_json(["bodycomp", "delete", "--date", as_of.isoformat(), "--json"])
        assert deleted["data"]["deleted"] == 1
        remaining = run_json(["bodycomp", "list", "--json"])
        assert len(remaining["data"]["entries"]) == 1

    def test_trend_needs_two_scans(self, cli_db: DatabaseConnection) -> None:
        create_user()
        _, as_of, _ = recent_dates(2)
        self._add_scan(as_of, weight=176.9, fat=44.7, lean=125.3)
        payload = run_json(["bodycomp", "trend", "--json"], expected_exit=1)
        assert "INSUFFICIENT_BODYCOMP" in payload["error_codes"]

    def test_progress_report_includes_body_composition(self, cli_db: DatabaseConnection) -> None:
        create_user()
        as_of = date.today() - timedelta(days=1)
        start = as_of - timedelta(days=90)
        weight_days = [as_of - timedelta(days=19 - index) for index in range(20)]
        for index, day in enumerate(weight_days):
            run_json(
                [
                    "weight",
                    "add",
                    f"{183.0 + 0.02 * index:.2f}",
                    "--date",
                    day.isoformat(),
                    "--json",
                ]
            )
        self._add_scan(start, weight=185.0, fat=45.0, lean=133.0)
        self._add_scan(as_of, weight=183.0, fat=39.0, lean=137.0)

        payload = run_json(
            ["tdee", "progress", "--as-of", as_of.isoformat(), "--days", "28", "--json"]
        )
        body = payload["data"]["body_composition"]
        assert body["latest"]["date"] == as_of.isoformat()
        assert body["delta"]["classification"] == "recomposition"

        human = runner.invoke(
            app, ["tdee", "progress", "--as-of", as_of.isoformat(), "--days", "28"]
        )
        assert human.exit_code == 0
        assert "Body composition" in human.stdout
        assert "recomposition" in human.stdout
