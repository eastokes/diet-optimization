"""Tests for body-composition models and recomp comparison logic."""

from __future__ import annotations

from datetime import date

import pytest

from llmn.db.connection import DatabaseConnection
from llmn.db.migrations import migrate_schema
from llmn.tracking.bodycomp import compare_compositions, select_comparison
from llmn.tracking.models import BodyComposition
from llmn.tracking.queries import BodyCompositionQueries


def make_scan(
    measured_at: date,
    *,
    weight: float,
    fat: float,
    lean: float,
    bone: float = 7.0,
    method: str = "dexa",
) -> BodyComposition:
    return BodyComposition(
        composition_id=None,
        user_id=1,
        measured_at=measured_at,
        method=method,
        weight_lbs=weight,
        fat_mass_lbs=fat,
        lean_soft_tissue_lbs=lean,
        bone_mineral_lbs=bone,
    )


class TestBodyCompositionModel:
    def test_dexa_lean_and_bone_compose_fat_free_mass(self) -> None:
        scan = make_scan(date(2026, 9, 17), weight=176.9, fat=44.7, lean=125.3, bone=6.9)
        assert scan.derived_fat_free_mass_lbs == pytest.approx(132.2)
        assert scan.derived_fat_mass_lbs == pytest.approx(44.7)
        assert scan.derived_body_fat_pct == pytest.approx(44.7 / 176.9 * 100, abs=0.01)

    def test_body_fat_percent_fills_missing_mass_values(self) -> None:
        scan = BodyComposition(
            composition_id=None,
            user_id=1,
            measured_at=date(2026, 1, 1),
            weight_lbs=200.0,
            body_fat_pct=25.0,
        )
        assert scan.derived_fat_free_mass_lbs == pytest.approx(150.0)
        assert scan.derived_fat_mass_lbs == pytest.approx(50.0)

    def test_explicit_fat_free_mass_wins(self) -> None:
        scan = BodyComposition(
            composition_id=None,
            user_id=1,
            measured_at=date(2026, 1, 1),
            weight_lbs=200.0,
            fat_free_mass_lbs=160.0,
            lean_soft_tissue_lbs=150.0,
            bone_mineral_lbs=7.0,
        )
        assert scan.derived_fat_free_mass_lbs == pytest.approx(160.0)
        assert scan.derived_fat_mass_lbs == pytest.approx(40.0)

    def test_invalid_values_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            BodyComposition(
                composition_id=None,
                user_id=1,
                measured_at=date(2026, 1, 1),
                method="vibes",
            )
        with pytest.raises(ValueError):
            BodyComposition(
                composition_id=None,
                user_id=1,
                measured_at=date(2026, 1, 1),
                weight_lbs=176.9,
                body_fat_pct=95.0,
            )
        with pytest.raises(ValueError):
            BodyComposition(
                composition_id=None,
                user_id=1,
                measured_at=date(2026, 1, 1),
                weight_lbs=176.9,
                fat_mass_lbs=180.0,
            )


class TestRecompComparison:
    def test_fat_down_and_lean_up_is_recomposition(self) -> None:
        start = make_scan(date(2026, 1, 1), weight=185.0, fat=45.0, lean=133.0)
        end = make_scan(date(2026, 4, 1), weight=183.0, fat=39.0, lean=137.0)
        delta = compare_compositions(start, end)
        assert delta.classification == "recomposition"
        assert delta.fat_mass_change_lbs == pytest.approx(-6.0)
        assert delta.fat_free_mass_change_lbs == pytest.approx(4.0)
        assert delta.weeks == pytest.approx(90 / 7)
        assert "fat mass down 6.0 lbs" in delta.summary
        assert not delta.warnings

    def test_fat_loss_only(self) -> None:
        start = make_scan(date(2026, 1, 1), weight=185.0, fat=45.0, lean=133.0)
        end = make_scan(date(2026, 3, 1), weight=180.0, fat=40.0, lean=133.4)
        delta = compare_compositions(start, end)
        assert delta.classification == "fat_loss_only"

    def test_fat_and_lean_loss_warns(self) -> None:
        start = make_scan(date(2026, 1, 1), weight=185.0, fat=45.0, lean=133.0)
        end = make_scan(date(2026, 3, 1), weight=174.0, fat=37.0, lean=130.0)
        delta = compare_compositions(start, end)
        assert delta.classification == "fat_and_lean_loss"
        assert any("protein" in warning.lower() for warning in delta.warnings)

    def test_small_changes_are_within_noise(self) -> None:
        start = make_scan(date(2026, 1, 1), weight=185.0, fat=45.0, lean=133.0)
        end = make_scan(date(2026, 4, 1), weight=184.4, fat=44.6, lean=132.8)
        delta = compare_compositions(start, end)
        assert delta.classification == "within_noise"

    def test_short_interval_is_flagged(self) -> None:
        start = make_scan(date(2026, 1, 1), weight=185.0, fat=45.0, lean=133.0)
        end = make_scan(date(2026, 1, 15), weight=183.0, fat=42.0, lean=134.0)
        delta = compare_compositions(start, end)
        assert any("measurement error" in warning for warning in delta.warnings)

    def test_cross_method_comparison_is_not_comparable(self) -> None:
        start = make_scan(date(2026, 1, 1), weight=185.0, fat=45.0, lean=133.0, method="dexa")
        end = make_scan(date(2026, 4, 1), weight=183.0, fat=39.0, lean=137.0, method="bia_scale")
        delta = compare_compositions(start, end)
        assert delta.classification == "not_comparable"
        assert any("not directly comparable" in warning for warning in delta.warnings)

    def test_missing_mass_data_is_insufficient(self) -> None:
        start = BodyComposition(
            composition_id=None,
            user_id=1,
            measured_at=date(2026, 1, 1),
            waist_inches=37.5,
        )
        end = BodyComposition(
            composition_id=None,
            user_id=1,
            measured_at=date(2026, 3, 1),
            waist_inches=36.0,
        )
        delta = compare_compositions(start, end)
        assert delta.classification == "insufficient_data"
        assert delta.waist_change_inches == pytest.approx(-1.5)

    def test_select_comparison_needs_two_usable_rows(self) -> None:
        waist_only = BodyComposition(
            composition_id=None,
            user_id=1,
            measured_at=date(2026, 1, 1),
            waist_inches=37.5,
        )
        scan = make_scan(date(2026, 2, 1), weight=185.0, fat=45.0, lean=133.0)
        assert select_comparison([waist_only, scan]) is None
        second = make_scan(date(2026, 4, 1), weight=183.0, fat=41.0, lean=135.0)
        pair = select_comparison([scan, waist_only, second])
        assert pair is not None
        assert pair[0].measured_at == date(2026, 2, 1)
        assert pair[1].measured_at == date(2026, 4, 1)


class TestBodyCompositionQueries:
    def test_upsert_list_and_delete(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            conn.execute(
                "INSERT INTO user_profiles (age, sex, height_inches, weight_lbs, "
                "activity_level) VALUES (44, 'male', 73, 176.9, 'moderate')"
            )
            user_id = conn.execute("SELECT user_id FROM user_profiles").fetchone()[0]
            scan = make_scan(date(2026, 9, 17), weight=176.9, fat=44.7, lean=125.3, bone=6.9)
            scan.user_id = user_id
            stored = BodyCompositionQueries.upsert(conn, scan)
            assert stored.composition_id is not None

            # Re-saving the same date/method updates rather than duplicating.
            scan.notes = "corrected"
            BodyCompositionQueries.upsert(conn, scan)
            entries = BodyCompositionQueries.get_entries(conn, user_id)
            assert len(entries) == 1
            assert entries[0].notes == "corrected"

            deleted = BodyCompositionQueries.delete(conn, user_id, date(2026, 9, 17))
            assert deleted == 1
            assert BodyCompositionQueries.get_entries(conn, user_id) == []

    def test_migration_creates_body_composition(self, temp_db: DatabaseConnection) -> None:
        with temp_db.get_connection() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = 5")
            assert migrate_schema(conn) == [5]
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert "body_composition" in tables
