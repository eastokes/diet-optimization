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
