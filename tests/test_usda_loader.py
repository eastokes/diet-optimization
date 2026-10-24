"""Tests for USDA CSV loading edge cases."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from llmn.data.usda_loader import USDALoader
from llmn.db.connection import DatabaseConnection


@pytest.fixture
def usda_dir(tmp_path: Path) -> Path:
    for filename in ("food.csv", "food_nutrient.csv", "nutrient.csv"):
        (tmp_path / filename).write_text("")
    pd.DataFrame(
        [
            {
                "fdc_id": 1,
                "portion_description": "",
                "modifier": "serving",
                "amount": 1.0,
                "gram_weight": 30.0,
            },
            {
                "fdc_id": 1,
                "portion_description": "",
                "modifier": "",
                "amount": 1.0,
                "gram_weight": 50.0,
            },
        ]
    ).to_csv(tmp_path / "food_portion.csv", index=False)
    return tmp_path


def test_servings_use_modifier_when_description_is_blank(
    usda_dir: Path, temp_db: DatabaseConnection
) -> None:
    with temp_db.get_connection() as conn:
        conn.execute(
            "INSERT INTO foods (fdc_id, description, is_active) VALUES (1, 'Test food', TRUE)"
        )
        loader = USDALoader(usda_dir, conn)
        count = loader._load_servings()
        row = conn.execute(
            "SELECT portion_description, gram_weight FROM servings WHERE fdc_id = 1"
        ).fetchone()

    assert count == 1
    assert row is not None
    assert row[0] == "1 serving"
    assert row[1] == 30.0
