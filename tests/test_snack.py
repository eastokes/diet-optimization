"""Tests for the remaining-macro snack suggestion command."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from llmn.cli import app
from llmn.db.connection import DatabaseConnection, set_db
from llmn.explore.snack import SnackRequest, suggest_snacks

runner = CliRunner()


FOODS = [
    # fdc_id, description, kcal, protein, carbs, fat, fiber
    (1, "Yogurt, Greek, plain, nonfat", 59.0, 10.0, 3.6, 0.4, 0.0),
    (2, "Blueberries, raw", 57.0, 0.7, 14.5, 0.3, 2.4),
    (3, "Almonds", 579.0, 21.2, 21.6, 49.9, 12.5),
    (4, "Chicken breast, cooked", 165.0, 31.0, 0.0, 3.6, 0.0),
    (5, "Brown rice, cooked", 123.0, 2.7, 25.6, 1.0, 1.6),
    (6, "Broccoli, cooked", 35.0, 2.4, 7.2, 0.4, 3.3),
    (7, "Egg, whole, hard-boiled", 155.0, 12.6, 1.1, 10.6, 0.0),
    (8, "Apple, raw", 52.0, 0.3, 13.8, 0.2, 2.4),
    (9, "Peanut butter", 588.0, 25.0, 20.0, 50.0, 6.0),
    (10, "Protein powder", 380.0, 80.0, 8.0, 5.0, 1.0),
    (11, "Olive oil", 884.0, 0.0, 0.0, 100.0, 0.0),
    (12, "Baby food, chicken", 81.0, 13.0, 0.0, 3.0, 0.0),
    (13, "Cottage cheese", 98.0, 11.1, 3.4, 4.3, 0.0),
    (14, "Banana, raw", 89.0, 1.1, 22.8, 0.3, 2.6),
    (15, "Tuna, canned in water", 116.0, 25.5, 0.0, 0.8, 0.0),
    (16, "Crackers, whole wheat", 443.0, 9.0, 68.0, 14.0, 6.0),
    (17, "Hummus, commercial", 166.0, 7.9, 14.3, 9.6, 6.0),
    (18, "Carrot, raw", 41.0, 0.9, 9.6, 0.2, 2.8),
    (19, "Walnuts", 654.0, 15.2, 13.7, 65.2, 6.7),
    (20, "Milk, nonfat", 34.0, 3.4, 5.0, 0.1, 0.0),
]

SERVINGS = [
    (1, "1 container", 170.0),
    (2, "1 cup", 100.0),
    (3, "1 oz", 28.0),
    (4, "3 oz", 85.0),
    (5, "1 cup", 195.0),
    (6, "1 cup", 91.0),
    (7, "1 large", 50.0),
    (8, "1 medium", 182.0),
    (9, "2 tbsp", 32.0),
    (10, "1 scoop", 30.0),
    (15, "1 can", 142.0),
    (16, "5 crackers", 15.0),
    (18, "1 medium", 61.0),
]

STAPLE_IDS = {1, 2, 3, 4, 5, 6, 8, 9, 10, 13, 14, 15, 16, 17, 18, 19, 20}


@pytest.fixture
def snack_db(temp_db: DatabaseConnection) -> DatabaseConnection:
    with temp_db.get_connection() as conn:
        conn.executemany(
            "INSERT INTO nutrients (nutrient_id, name, unit, display_name, is_macro) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (1008, "Energy", "kcal", "Calories", True),
                (1003, "Protein", "g", "Protein", True),
                (1004, "Total lipid (fat)", "g", "Fat", True),
                (1005, "Carbohydrate, by difference", "g", "Carbs", True),
                (1079, "Fiber, total dietary", "g", "Fiber", False),
            ],
        )
        conn.executemany(
            "INSERT INTO foods (fdc_id, description, data_type, is_active) "
            "VALUES (?, ?, 'test', TRUE)",
            [(food[0], food[1]) for food in FOODS],
        )
        conn.executemany(
            "INSERT INTO food_nutrients (fdc_id, nutrient_id, amount) VALUES (?, ?, ?)",
            [
                (food[0], nutrient_id, amount)
                for food in FOODS
                for nutrient_id, amount in (
                    (1008, food[2]),
                    (1003, food[3]),
                    (1005, food[4]),
                    (1004, food[5]),
                    (1079, food[6]),
                )
            ],
        )
        conn.executemany(
            "INSERT INTO servings (fdc_id, portion_description, gram_weight) VALUES (?, ?, ?)",
            SERVINGS,
        )
        conn.executemany(
            "INSERT INTO food_tags (fdc_id, tag) VALUES (?, 'staple')",
            [(fdc_id,) for fdc_id in STAPLE_IDS],
        )
        conn.execute("INSERT INTO food_tags (fdc_id, tag) VALUES (12, 'exclude')")
    return temp_db


@pytest.fixture
def cli_db(snack_db: DatabaseConnection):
    set_db(snack_db)
    yield snack_db
    set_db(None)


def _request(**overrides: object) -> SnackRequest:
    values = {
        "calories": 300.0,
        "protein_min": 30.0,
        "max_carbs": 20.0,
        "max_fat": 10.0,
    }
    values.update(overrides)
    return SnackRequest(**values)  # type: ignore[arg-type]


class TestSnackSuggestions:
    def test_suggests_macro_matched_combinations(self, snack_db: DatabaseConnection) -> None:
        with snack_db.get_connection() as conn:
            result = suggest_snacks(conn, _request())

        assert result.suggestions
        assert result.feasible_count >= len(result.suggestions)
        assert any(len(suggestion.portions) > 1 for suggestion in result.suggestions)

        for suggestion in result.suggestions:
            assert suggestion.protein >= 30.0
            assert suggestion.carbs <= 20.0
            assert suggestion.fat <= 10.0
            assert abs(suggestion.calories - 300) <= 31
            for portion in suggestion.portions:
                assert portion.grams > 0
                assert "baby food" not in portion.food.description.lower()
                assert "olive oil" not in portion.food.description.lower()

    def test_top_suggestion_prefers_complementary_foods(self, snack_db: DatabaseConnection) -> None:
        with snack_db.get_connection() as conn:
            result = suggest_snacks(conn, _request())

        assert result.suggestions
        top = result.suggestions[0]
        categories = {portion.food.category.value for portion in top.portions}
        assert len(categories) >= 2
        assert any("protein" in note or "complementary" in note for note in top.pairing_notes)

    def test_single_food_suggestion_can_fit(self, snack_db: DatabaseConnection) -> None:
        with snack_db.get_connection() as conn:
            result = suggest_snacks(
                conn,
                _request(
                    calories=200.0,
                    protein_min=30.0,
                    max_carbs=0.0,
                    max_fat=5.0,
                    max_foods=1,
                ),
            )

        assert result.suggestions
        assert all(len(suggestion.portions) == 1 for suggestion in result.suggestions)

    def test_tag_filter_restricts_candidates(self, snack_db: DatabaseConnection) -> None:
        with snack_db.get_connection() as conn:
            result = suggest_snacks(conn, _request(include_tag="staple"))

        assert result.suggestions
        for suggestion in result.suggestions:
            for portion in suggestion.portions:
                assert "staple" in portion.food.tags

    def test_returns_explicit_no_result(self, snack_db: DatabaseConnection) -> None:
        with snack_db.get_connection() as conn:
            result = suggest_snacks(
                conn,
                _request(protein_min=100.0, max_carbs=0.0, max_fat=0.0),
            )

        assert result.suggestions == ()
        assert "NO_FEASIBLE_COMBINATION" in result.reason_codes
        assert result.warnings

    def test_reproducible_for_same_inputs(self, snack_db: DatabaseConnection) -> None:
        with snack_db.get_connection() as conn:
            first = suggest_snacks(conn, _request()).to_dict()
            second = suggest_snacks(conn, _request()).to_dict()

        assert first == second


class TestSnackCLI:
    def test_json_output(self, cli_db: DatabaseConnection) -> None:
        result = runner.invoke(
            app,
            [
                "snack",
                "--calories",
                "300",
                "--protein",
                "30",
                "--max-carbs",
                "20",
                "--max-fat",
                "10",
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["success"] is True
        assert payload["data"]["target"]["calories"] == 300
        assert payload["data"]["suggestions"]
        assert "NaN" not in result.stdout
        assert "Infinity" not in result.stdout

    def test_json_no_result_is_success_with_actions(self, cli_db: DatabaseConnection) -> None:
        result = runner.invoke(
            app,
            [
                "snack",
                "--calories",
                "300",
                "--protein",
                "100",
                "--max-carbs",
                "0",
                "--max-fat",
                "0",
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["success"] is True
        assert payload["data"]["suggestions"] == []
        assert payload["suggestions"]

    def test_invalid_input_is_structured(self, cli_db: DatabaseConnection) -> None:
        result = runner.invoke(
            app,
            ["snack", "--calories", "300", "--max-foods", "0", "--json"],
        )

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert payload["errors"]

    def test_missing_food_database_is_structured(self, temp_db: DatabaseConnection) -> None:
        set_db(temp_db)
        try:
            result = runner.invoke(app, ["snack", "--calories", "300", "--json"])
        finally:
            set_db(None)

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert "food data" in payload["errors"][0].lower()
