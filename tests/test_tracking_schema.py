"""Tests for versioned tracking migrations."""

from __future__ import annotations

import sqlite3

import pytest

from llmn.db import migrations
from llmn.db.connection import DatabaseConnection


def test_fresh_schema_records_all_versions(temp_db: DatabaseConnection) -> None:
    with temp_db.get_connection() as conn:
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        profile_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(user_profiles)").fetchall()
        }
        estimate_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(tdee_estimates)").fetchall()
        }

    assert versions == [1, 2, 3, 4, 5]
    assert {"intake_log", "goal_periods", "activity_periods", "body_composition"} <= tables
    assert {"birthdate", "resting_metabolic_rate", "rmr_measured_at", "rmr_source"} <= (
        profile_columns
    )
    assert "baseline_source" in estimate_columns


def test_repeated_migration_is_noop(temp_db: DatabaseConnection) -> None:
    with temp_db.get_connection() as conn:
        assert migrations.migrate_schema(conn) == []
        assert migrations.migrate_schema(conn) == []


def test_legacy_calories_are_backfilled_without_relabelling(
    temp_db: DatabaseConnection,
) -> None:
    with temp_db.get_connection() as conn:
        conn.execute(
            "INSERT INTO user_profiles (age, sex, height_inches, weight_lbs, activity_level) "
            "VALUES (38, 'male', 72, 185, 'moderate')"
        )
        user_id = conn.execute("SELECT user_id FROM user_profiles").fetchone()[0]
        conn.execute("INSERT INTO foods (fdc_id, description) VALUES (1, 'Preserved food')")
        conn.execute(
            "INSERT INTO calorie_log (user_id, date, planned_calories, notes) "
            "VALUES (?, '2026-01-01', 1900, 'old plan')",
            (user_id,),
        )

        # Simulate an existing database that has not run migration 3.
        conn.execute("DELETE FROM schema_migrations WHERE version = 3")
        assert migrations.migrate_schema(conn) == [3]

        row = conn.execute(
            "SELECT kcal, kind, completeness, quality, provenance "
            "FROM intake_log WHERE user_id = ? AND date = '2026-01-01'",
            (user_id,),
        ).fetchone()
        food_count = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
        assert tuple(row) == (
            1900,
            "legacy_unclassified",
            "unknown",
            "unknown",
            "legacy_calorie_log",
        )
        assert food_count == 1

        # Repeat the backfill; the unique legacy row must not duplicate.
        conn.execute("DELETE FROM schema_migrations WHERE version = 3")
        migrations.migrate_schema(conn)
        duplicates = conn.execute(
            "SELECT COUNT(*) FROM intake_log WHERE user_id = ? AND kind = 'legacy_unclassified'",
            (user_id,),
        ).fetchone()[0]
        assert duplicates == 1


def test_failed_migration_rolls_back(
    temp_db: DatabaseConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_migration(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE should_be_rolled_back (value INTEGER)")
        raise RuntimeError("boom")

    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        ((99, "failing migration", failing_migration),),
    )

    with pytest.raises(RuntimeError):
        with temp_db.get_connection() as conn:
            migrations.migrate_schema(conn)

    with temp_db.get_connection() as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='should_be_rolled_back'"
        ).fetchone()
        version = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = 99"
        ).fetchone()
    assert exists is None
    assert version is None
