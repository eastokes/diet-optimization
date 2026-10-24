"""Versioned, transactional migrations for tracking-related schema changes.

Every migration is safe to run against a database that already has the target
shape (fresh databases are created from ``SCHEMA_SQL`` first). Applied versions
are recorded in ``schema_migrations`` so repeated calls are no-ops.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from llmn.db.schema import TRACKING_V2_SQL, TRACKING_V3_SQL

MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> bool:
    """Add a column if it is missing. Return True when the column was added."""
    if column in _table_columns(conn, table):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    return True


def _migration_1_diet_columns(conn: sqlite3.Connection) -> None:
    _ensure_column(
        conn,
        "user_profiles",
        "diet_type",
        (
            "TEXT CHECK(diet_type IN ('omnivore', 'pescatarian', 'vegetarian', "
            "'vegan') OR diet_type IS NULL)"
        ),
    )
    _ensure_column(
        conn,
        "user_profiles",
        "diet_style",
        (
            "TEXT CHECK(diet_style IN ('standard', 'slow_carb', 'low_carb', "
            "'keto', 'mediterranean', 'paleo') OR diet_style IS NULL)"
        ),
    )


def _migration_2_tracking_v2(conn: sqlite3.Connection) -> None:
    # Execute statements individually: sqlite3.executescript() commits any
    # pending transaction, which would defeat the migration savepoint. Strip
    # line comments first because one contains a semicolon.
    cleaned = "\n".join(line.split("--", 1)[0] for line in TRACKING_V2_SQL.splitlines())
    for statement in cleaned.split(";"):
        if statement.strip():
            conn.execute(statement)

    # Extend the existing estimate table without replacing it, preserving IDs,
    # dates, and historic values.
    new_columns = {
        "method": "TEXT",
        "policy_version": "TEXT",
        "interval_start": "DATE",
        "interval_end": "DATE",
        "weight_observations": "INTEGER",
        "complete_intake_days": "INTEGER",
        "partial_intake_days": "INTEGER",
        "input_fingerprint": "TEXT",
    }
    for column, declaration in new_columns.items():
        _ensure_column(conn, "tdee_estimates", column, declaration)


def _migration_3_legacy_calories(conn: sqlite3.Connection) -> None:
    """Copy old planned-calorie rows into intake_log without reclassifying them."""
    conn.execute(
        """
        INSERT INTO intake_log
            (user_id, date, kcal, kind, completeness, quality, provenance,
             notes, revision, created_at, updated_at)
        SELECT c.user_id, c.date, c.planned_calories, 'legacy_unclassified',
               'unknown', 'unknown', 'legacy_calorie_log', c.notes, 1,
               CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM calorie_log AS c
        WHERE NOT EXISTS (
            SELECT 1 FROM intake_log AS i
            WHERE i.user_id = c.user_id
              AND i.date = c.date
              AND i.kind = 'legacy_unclassified'
        )
        """
    )


def _migration_4_birthdate_and_rmr(conn: sqlite3.Connection) -> None:
    """Store a birthdate (derivable age) and an optional measured RMR.

    ``age`` stays for compatibility and for profiles that only know their age;
    a birthdate, when present, wins so the age cannot silently go stale.
    """
    _ensure_column(conn, "user_profiles", "birthdate", "DATE")
    _ensure_column(conn, "user_profiles", "resting_metabolic_rate", "REAL")
    _ensure_column(conn, "user_profiles", "rmr_measured_at", "DATE")
    _ensure_column(conn, "user_profiles", "rmr_source", "TEXT")
    # Record which resting component produced a persisted baseline.
    _ensure_column(conn, "tdee_estimates", "baseline_source", "TEXT")


def _migration_5_body_composition(conn: sqlite3.Connection) -> None:
    """Create the body-composition table for recomp analysis."""
    cleaned = "\n".join(line.split("--", 1)[0] for line in TRACKING_V3_SQL.splitlines())
    for statement in cleaned.split(";"):
        if statement.strip():
            conn.execute(statement)


Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]

MIGRATIONS: tuple[Migration, ...] = (
    (1, "add diet columns to user_profiles", _migration_1_diet_columns),
    (2, "create tracking v2 tables and estimate metadata", _migration_2_tracking_v2),
    (3, "backfill legacy calorie rows as unclassified intake", _migration_3_legacy_calories),
    (4, "add birthdate and measured RMR to user_profiles", _migration_4_birthdate_and_rmr),
    (5, "create body_composition table", _migration_5_body_composition),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


def get_schema_version(conn: sqlite3.Connection) -> int:
    conn.execute(MIGRATION_TABLE_SQL)
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def migrate_schema(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations and return the versions applied this call.

    The caller owns the transaction. On failure the exception propagates so the
    connection context manager can roll back the entire migration batch.
    """
    conn.execute("SAVEPOINT llmn_schema_migration")
    try:
        conn.execute(MIGRATION_TABLE_SQL)
        applied_rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        applied = {int(row[0]) for row in applied_rows}

        newly_applied: list[int] = []
        for version, _description, migration in MIGRATIONS:
            if version in applied:
                continue
            migration(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (version,),
            )
            newly_applied.append(version)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT llmn_schema_migration")
        conn.execute("RELEASE SAVEPOINT llmn_schema_migration")
        raise

    conn.execute("RELEASE SAVEPOINT llmn_schema_migration")
    return newly_applied
