"""Database queries for weight tracking and calorie recommendations."""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta

from llmn.tracking.ema import update_trend
from llmn.tracking.models import (
    ActivityPeriod,
    CalorieEntry,
    GoalPeriod,
    IntakeEntry,
    TDEEEstimate,
    UserProfile,
    WeightEntry,
)


class UserQueries:
    """Database queries for user profiles."""

    _SELECT = """
        SELECT user_id, age, sex, height_inches, weight_lbs,
               activity_level, goal, target_weight_lbs,
               diet_type, diet_style, created_at
        FROM user_profiles
    """

    @staticmethod
    def _from_row(row: sqlite3.Row) -> UserProfile:
        return UserProfile(
            user_id=row[0],
            age=row[1],
            sex=row[2],
            height_inches=row[3],
            weight_lbs=row[4],
            activity_level=row[5],
            goal=row[6],
            target_weight_lbs=row[7],
            diet_type=row[8],
            diet_style=row[9],
            created_at=datetime.fromisoformat(row[10]) if row[10] else None,
        )

    @staticmethod
    def create_user(conn: sqlite3.Connection, profile: UserProfile) -> int:
        """Create a new user profile and return the user_id."""
        cursor = conn.execute(
            """
            INSERT INTO user_profiles (age, sex, height_inches, weight_lbs,
                                       activity_level, goal, target_weight_lbs,
                                       diet_type, diet_style)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                profile.age,
                profile.sex,
                profile.height_inches,
                profile.weight_lbs,
                profile.activity_level,
                profile.goal,
                profile.target_weight_lbs,
                profile.diet_type,
                profile.diet_style,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0

    @staticmethod
    def get_user(conn: sqlite3.Connection, user_id: int) -> UserProfile | None:
        """Get user profile by ID."""
        row = conn.execute(
            UserQueries._SELECT + " WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return UserQueries._from_row(row) if row is not None else None

    @staticmethod
    def get_default_user(conn: sqlite3.Connection) -> UserProfile | None:
        """Get the first (default) user profile."""
        row = conn.execute(UserQueries._SELECT + " ORDER BY user_id LIMIT 1").fetchone()
        return UserQueries._from_row(row) if row is not None else None

    @staticmethod
    def update_user(conn: sqlite3.Connection, profile: UserProfile) -> None:
        """Update an existing user profile."""
        if profile.user_id is None:
            raise ValueError("Cannot update profile without user_id")

        conn.execute(
            """
            UPDATE user_profiles
            SET age = ?, sex = ?, height_inches = ?, weight_lbs = ?,
                activity_level = ?, goal = ?, target_weight_lbs = ?,
                diet_type = ?, diet_style = ?
            WHERE user_id = ?
            """,
            (
                profile.age,
                profile.sex,
                profile.height_inches,
                profile.weight_lbs,
                profile.activity_level,
                profile.goal,
                profile.target_weight_lbs,
                profile.diet_type,
                profile.diet_style,
                profile.user_id,
            ),
        )
        conn.commit()


class WeightQueries:
    """Database queries for weight log entries."""

    @staticmethod
    def _recompute_trends(conn: sqlite3.Connection, user_id: int) -> None:
        """Rebuild every stored trend for a user from raw measurements.

        Repeated reads never touch stored data. Writes call this so corrections
        and backfills propagate to all subsequent trend values.
        """
        rows = conn.execute(
            """
            SELECT log_id, weight_lbs, measured_at
            FROM weight_log
            WHERE user_id = ?
            ORDER BY measured_at
            """,
            (user_id,),
        ).fetchall()

        if not rows:
            return

        trend = float(rows[0][1])
        prev_date = date.fromisoformat(str(rows[0][2]))
        updates: list[tuple[float, int]] = [(trend, int(rows[0][0]))]

        for row in rows[1:]:
            log_id = int(row[0])
            weight = float(row[1])
            measured_at = date.fromisoformat(str(row[2]))
            elapsed_days = max(1, (measured_at - prev_date).days)
            trend = update_trend(trend, weight, days_elapsed=elapsed_days)
            updates.append((trend, log_id))
            prev_date = measured_at

        conn.executemany(
            "UPDATE weight_log SET trend_lbs = ? WHERE log_id = ?",
            updates,
        )

    @staticmethod
    def add_weight(
        conn: sqlite3.Connection,
        user_id: int,
        weight_lbs: float,
        measured_at: date,
        notes: str | None = None,
    ) -> WeightEntry:
        """Add or replace a weight entry, then rebuild all later trends."""
        if not math.isfinite(weight_lbs) or weight_lbs <= 0:
            raise ValueError("weight_lbs must be a finite, positive number")
        conn.execute(
            """
            INSERT INTO weight_log (user_id, weight_lbs, trend_lbs, measured_at, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, measured_at) DO UPDATE SET
                weight_lbs = excluded.weight_lbs,
                notes = excluded.notes
            """,
            (user_id, weight_lbs, weight_lbs, measured_at.isoformat(), notes),
        )
        WeightQueries._recompute_trends(conn, user_id)
        conn.commit()

        row = conn.execute(
            """
            SELECT log_id, user_id, weight_lbs, trend_lbs, measured_at, notes
            FROM weight_log
            WHERE user_id = ? AND measured_at = ?
            """,
            (user_id, measured_at.isoformat()),
        ).fetchone()
        assert row is not None

        return WeightEntry(
            log_id=row[0],
            user_id=row[1],
            weight_lbs=row[2],
            trend_lbs=row[3],
            measured_at=date.fromisoformat(row[4]),
            notes=row[5],
        )

    @staticmethod
    def delete_weight(conn: sqlite3.Connection, user_id: int, measured_at: date) -> bool:
        """Delete a weight entry and rebuild later trends. Return True if deleted."""
        cursor = conn.execute(
            "DELETE FROM weight_log WHERE user_id = ? AND measured_at = ?",
            (user_id, measured_at.isoformat()),
        )
        if cursor.rowcount:
            WeightQueries._recompute_trends(conn, user_id)
        conn.commit()
        return bool(cursor.rowcount)

    @staticmethod
    def get_latest_weight(conn: sqlite3.Connection, user_id: int) -> WeightEntry | None:
        """Get the most recent weight entry."""
        row = conn.execute(
            """
            SELECT log_id, user_id, weight_lbs, trend_lbs, measured_at, notes
            FROM weight_log
            WHERE user_id = ?
            ORDER BY measured_at DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()

        if row is None:
            return None

        return WeightEntry(
            log_id=row[0],
            user_id=row[1],
            weight_lbs=row[2],
            trend_lbs=row[3],
            measured_at=date.fromisoformat(row[4]),
            notes=row[5],
        )

    @staticmethod
    def get_latest_weight_on_or_before(
        conn: sqlite3.Connection, user_id: int, as_of: date
    ) -> WeightEntry | None:
        """Get the most recent measurement on or before ``as_of``."""
        row = conn.execute(
            """
            SELECT log_id, user_id, weight_lbs, trend_lbs, measured_at, notes
            FROM weight_log
            WHERE user_id = ? AND measured_at <= ?
            ORDER BY measured_at DESC LIMIT 1
            """,
            (user_id, as_of.isoformat()),
        ).fetchone()

        if row is None:
            return None

        return WeightEntry(
            log_id=row[0],
            user_id=row[1],
            weight_lbs=row[2],
            trend_lbs=row[3],
            measured_at=date.fromisoformat(row[4]),
            notes=row[5],
        )

    @staticmethod
    def get_weight_history(
        conn: sqlite3.Connection,
        user_id: int,
        days: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[WeightEntry]:
        """Get weight history for a calendar interval.

        ``days`` selects the last N calendar days ending on ``end_date`` (or
        today). It is never translated into a row count.
        """
        if end_date is None:
            end_date = date.today()
        if days is not None and start_date is None:
            start_date = end_date - timedelta(days=max(1, days) - 1)

        query = """
            SELECT log_id, user_id, weight_lbs, trend_lbs, measured_at, notes
            FROM weight_log
            WHERE user_id = ?
        """
        params: list[object] = [user_id]

        if start_date:
            query += " AND measured_at >= ?"
            params.append(start_date.isoformat())
        if end_date:
            query += " AND measured_at <= ?"
            params.append(end_date.isoformat())

        query += " ORDER BY measured_at"

        rows = conn.execute(query, params).fetchall()

        return [
            WeightEntry(
                log_id=row[0],
                user_id=row[1],
                weight_lbs=row[2],
                trend_lbs=row[3],
                measured_at=date.fromisoformat(row[4]),
                notes=row[5],
            )
            for row in rows
        ]

    @staticmethod
    def get_trend_at_date(conn: sqlite3.Connection, user_id: int, at_date: date) -> float | None:
        """Get the trend value at or before a specific date."""
        row = conn.execute(
            """
            SELECT trend_lbs FROM weight_log
            WHERE user_id = ? AND measured_at <= ?
            ORDER BY measured_at DESC LIMIT 1
            """,
            (user_id, at_date.isoformat()),
        ).fetchone()

        return float(row[0]) if row else None


class CalorieQueries:
    """Legacy planned-calorie queries retained for compatibility."""

    @staticmethod
    def log_calories(
        conn: sqlite3.Connection,
        user_id: int,
        log_date: date,
        planned_calories: float,
        notes: str | None = None,
    ) -> CalorieEntry:
        """Log planned calories for a day (insert or replace)."""
        cursor = conn.execute(
            """
            INSERT OR REPLACE INTO calorie_log (user_id, date, planned_calories, notes)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, log_date.isoformat(), planned_calories, notes),
        )
        conn.commit()

        return CalorieEntry(
            log_id=cursor.lastrowid,
            user_id=user_id,
            date=log_date,
            planned_calories=planned_calories,
            notes=notes,
        )

    @staticmethod
    def get_calorie_history(
        conn: sqlite3.Connection,
        user_id: int,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[CalorieEntry]:
        """Get calorie log history."""
        query = """
            SELECT log_id, user_id, date, planned_calories, notes
            FROM calorie_log
            WHERE user_id = ?
        """
        params: list[object] = [user_id]

        if start_date:
            query += " AND date >= ?"
            params.append(start_date.isoformat())
        if end_date:
            query += " AND date <= ?"
            params.append(end_date.isoformat())

        query += " ORDER BY date"

        rows = conn.execute(query, params).fetchall()

        return [
            CalorieEntry(
                log_id=row[0],
                user_id=row[1],
                date=date.fromisoformat(row[2]),
                planned_calories=row[3],
                notes=row[4],
            )
            for row in rows
        ]

    @staticmethod
    def get_average_calories(
        conn: sqlite3.Connection,
        user_id: int,
        start_date: date,
        end_date: date,
    ) -> float | None:
        """Get average daily planned calories over a period."""
        row = conn.execute(
            """
            SELECT AVG(planned_calories) FROM calorie_log
            WHERE user_id = ? AND date >= ? AND date <= ?
            """,
            (user_id, start_date.isoformat(), end_date.isoformat()),
        ).fetchone()

        return float(row[0]) if row and row[0] is not None else None


class IntakeQueries:
    """Queries for dated actual/planned intake observations."""

    _SELECT = """
        SELECT intake_id, user_id, date, kcal, kind, completeness, quality,
               provenance, notes, revision
        FROM intake_log
    """

    @staticmethod
    def _from_row(row: sqlite3.Row) -> IntakeEntry:
        return IntakeEntry(
            intake_id=row[0],
            user_id=row[1],
            date=date.fromisoformat(row[2]),
            kcal=row[3],
            kind=row[4],
            completeness=row[5],
            quality=row[6],
            provenance=row[7],
            notes=row[8],
            revision=row[9],
        )

    @staticmethod
    def upsert(
        conn: sqlite3.Connection,
        user_id: int,
        log_date: date,
        kcal: float,
        kind: str = "actual",
        completeness: str = "complete",
        quality: str = "measured",
        provenance: str = "manual",
        notes: str | None = None,
    ) -> IntakeEntry:
        """Insert or revise an intake observation for a date and kind."""
        # Validate through the model before writing.
        IntakeEntry(
            intake_id=None,
            user_id=user_id,
            date=log_date,
            kcal=kcal,
            kind=kind,
            completeness=completeness,
            quality=quality,
            provenance=provenance,
            notes=notes,
        )

        conn.execute(
            """
            INSERT INTO intake_log
                (user_id, date, kcal, kind, completeness, quality, provenance, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, date, kind) DO UPDATE SET
                kcal = excluded.kcal,
                completeness = excluded.completeness,
                quality = excluded.quality,
                provenance = excluded.provenance,
                notes = excluded.notes,
                revision = intake_log.revision + 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                log_date.isoformat(),
                kcal,
                kind,
                completeness,
                quality,
                provenance,
                notes,
            ),
        )
        conn.commit()

        row = conn.execute(
            IntakeQueries._SELECT + " WHERE user_id = ? AND date = ? AND kind = ?",
            (user_id, log_date.isoformat(), kind),
        ).fetchone()
        assert row is not None
        return IntakeQueries._from_row(row)

    @staticmethod
    def get_entries(
        conn: sqlite3.Connection,
        user_id: int,
        start_date: date | None = None,
        end_date: date | None = None,
        kind: str | None = None,
    ) -> list[IntakeEntry]:
        query = IntakeQueries._SELECT + " WHERE user_id = ?"
        params: list[object] = [user_id]

        if start_date:
            query += " AND date >= ?"
            params.append(start_date.isoformat())
        if end_date:
            query += " AND date <= ?"
            params.append(end_date.isoformat())
        if kind:
            query += " AND kind = ?"
            params.append(kind)

        query += " ORDER BY date, kind"
        rows = conn.execute(query, params).fetchall()
        return [IntakeQueries._from_row(row) for row in rows]

    @staticmethod
    def delete(
        conn: sqlite3.Connection, user_id: int, log_date: date, kind: str = "actual"
    ) -> bool:
        cursor = conn.execute(
            "DELETE FROM intake_log WHERE user_id = ? AND date = ? AND kind = ?",
            (user_id, log_date.isoformat(), kind),
        )
        conn.commit()
        return bool(cursor.rowcount)

    @staticmethod
    def preview_legacy(
        conn: sqlite3.Connection,
        user_id: int,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[date]:
        rows = conn.execute(
            """
            SELECT date FROM intake_log
            WHERE user_id = ? AND kind = 'legacy_unclassified'
              AND (? IS NULL OR date >= ?)
              AND (? IS NULL OR date <= ?)
            ORDER BY date
            """,
            (
                user_id,
                start_date.isoformat() if start_date else None,
                start_date.isoformat() if start_date else None,
                end_date.isoformat() if end_date else None,
                end_date.isoformat() if end_date else None,
            ),
        ).fetchall()
        return [date.fromisoformat(row[0]) for row in rows]

    @staticmethod
    def classify_legacy(
        conn: sqlite3.Connection,
        user_id: int,
        as_kind: str,
        completeness: str,
        quality: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[list[date], list[date]]:
        """Classify legacy rows. Return (updated_dates, skipped_conflicts)."""
        dates = IntakeQueries.preview_legacy(conn, user_id, start_date, end_date)
        updated: list[date] = []
        skipped: list[date] = []

        for log_date in dates:
            try:
                cursor = conn.execute(
                    """
                    UPDATE intake_log
                    SET kind = ?, completeness = ?, quality = ?,
                        provenance = 'confirmed_legacy',
                        revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND date = ? AND kind = 'legacy_unclassified'
                    """,
                    (
                        as_kind,
                        completeness,
                        quality,
                        user_id,
                        log_date.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                skipped.append(log_date)
                continue

            if cursor.rowcount:
                updated.append(log_date)
            else:
                skipped.append(log_date)

        conn.commit()
        return updated, skipped


class GoalQueries:
    """Effective-dated weight-change goals."""

    _SELECT = """
        SELECT goal_id, user_id, effective_from, mode, rate_lbs_per_week,
               target_weight_lbs, training_priority, notes
        FROM goal_periods
    """

    @staticmethod
    def _from_row(row: sqlite3.Row) -> GoalPeriod:
        return GoalPeriod(
            goal_id=row[0],
            user_id=row[1],
            effective_from=date.fromisoformat(row[2]),
            mode=row[3],
            rate_lbs_per_week=row[4],
            target_weight_lbs=row[5],
            training_priority=row[6],
            notes=row[7],
        )

    @staticmethod
    def set_goal(conn: sqlite3.Connection, goal: GoalPeriod) -> GoalPeriod:
        """Create or update the goal effective on a date, preserving other periods."""
        conn.execute(
            """
            INSERT INTO goal_periods
                (user_id, effective_from, mode, rate_lbs_per_week,
                 target_weight_lbs, training_priority, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, effective_from) DO UPDATE SET
                mode = excluded.mode,
                rate_lbs_per_week = excluded.rate_lbs_per_week,
                target_weight_lbs = excluded.target_weight_lbs,
                training_priority = excluded.training_priority,
                notes = excluded.notes
            """,
            (
                goal.user_id,
                goal.effective_from.isoformat(),
                goal.mode,
                goal.rate_lbs_per_week,
                goal.target_weight_lbs,
                goal.training_priority,
                goal.notes,
            ),
        )
        conn.commit()

        row = conn.execute(
            GoalQueries._SELECT + " WHERE user_id = ? AND effective_from = ?",
            (goal.user_id, goal.effective_from.isoformat()),
        ).fetchone()
        assert row is not None
        return GoalQueries._from_row(row)

    @staticmethod
    def get_effective_goal(
        conn: sqlite3.Connection, user_id: int, as_of: date
    ) -> GoalPeriod | None:
        row = conn.execute(
            GoalQueries._SELECT
            + " WHERE user_id = ? AND effective_from <= ? ORDER BY effective_from DESC LIMIT 1",
            (user_id, as_of.isoformat()),
        ).fetchone()
        return GoalQueries._from_row(row) if row is not None else None

    @staticmethod
    def get_history(
        conn: sqlite3.Connection, user_id: int, as_of: date | None = None
    ) -> list[GoalPeriod]:
        query = GoalQueries._SELECT + " WHERE user_id = ?"
        params: list[object] = [user_id]
        if as_of:
            query += " AND effective_from <= ?"
            params.append(as_of.isoformat())
        query += " ORDER BY effective_from"
        rows = conn.execute(query, params).fetchall()
        return [GoalQueries._from_row(row) for row in rows]


class ActivityQueries:
    """Effective-dated activity/training context."""

    _SELECT = """
        SELECT activity_id, user_id, effective_from, activity_level,
               training_priority, notes
        FROM activity_periods
    """

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ActivityPeriod:
        return ActivityPeriod(
            activity_id=row[0],
            user_id=row[1],
            effective_from=date.fromisoformat(row[2]),
            activity_level=row[3],
            training_priority=row[4],
            notes=row[5],
        )

    @staticmethod
    def set_activity(conn: sqlite3.Connection, period: ActivityPeriod) -> ActivityPeriod:
        conn.execute(
            """
            INSERT INTO activity_periods
                (user_id, effective_from, activity_level, training_priority, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, effective_from) DO UPDATE SET
                activity_level = excluded.activity_level,
                training_priority = excluded.training_priority,
                notes = excluded.notes
            """,
            (
                period.user_id,
                period.effective_from.isoformat(),
                period.activity_level,
                period.training_priority,
                period.notes,
            ),
        )
        conn.commit()

        row = conn.execute(
            ActivityQueries._SELECT + " WHERE user_id = ? AND effective_from = ?",
            (period.user_id, period.effective_from.isoformat()),
        ).fetchone()
        assert row is not None
        return ActivityQueries._from_row(row)

    @staticmethod
    def get_effective_activity(
        conn: sqlite3.Connection, user_id: int, as_of: date
    ) -> ActivityPeriod | None:
        row = conn.execute(
            ActivityQueries._SELECT
            + " WHERE user_id = ? AND effective_from <= ? ORDER BY effective_from DESC LIMIT 1",
            (user_id, as_of.isoformat()),
        ).fetchone()
        return ActivityQueries._from_row(row) if row is not None else None

    @staticmethod
    def get_history(
        conn: sqlite3.Connection, user_id: int, as_of: date | None = None
    ) -> list[ActivityPeriod]:
        query = ActivityQueries._SELECT + " WHERE user_id = ?"
        params: list[object] = [user_id]
        if as_of:
            query += " AND effective_from <= ?"
            params.append(as_of.isoformat())
        query += " ORDER BY effective_from"
        rows = conn.execute(query, params).fetchall()
        return [ActivityQueries._from_row(row) for row in rows]


class TDEEQueries:
    """Database queries for persisted expenditure estimates."""

    _SELECT = """
        SELECT estimate_id, user_id, estimated_at, mifflin_tdee, tdee_bias,
               variance, adjusted_tdee, method, policy_version, interval_start,
               interval_end, weight_observations, complete_intake_days,
               partial_intake_days, input_fingerprint
        FROM tdee_estimates
    """

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TDEEEstimate:
        return TDEEEstimate(
            estimate_id=row[0],
            user_id=row[1],
            estimated_at=date.fromisoformat(row[2]),
            mifflin_tdee=row[3],
            tdee_bias=row[4],
            variance=row[5],
            adjusted_tdee=row[6],
            method=row[7],
            policy_version=row[8],
            interval_start=date.fromisoformat(row[9]) if row[9] else None,
            interval_end=date.fromisoformat(row[10]) if row[10] else None,
            weight_observations=row[11],
            complete_intake_days=row[12],
            partial_intake_days=row[13],
            input_fingerprint=row[14],
        )

    @staticmethod
    def save_estimate(conn: sqlite3.Connection, estimate: TDEEEstimate) -> int:
        """Save an estimate snapshot (insert or replace for the date)."""
        cursor = conn.execute(
            """
            INSERT OR REPLACE INTO tdee_estimates
            (user_id, estimated_at, mifflin_tdee, tdee_bias, variance,
             adjusted_tdee, method, policy_version, interval_start, interval_end,
             weight_observations, complete_intake_days, partial_intake_days,
             input_fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                estimate.user_id,
                estimate.estimated_at.isoformat(),
                estimate.mifflin_tdee,
                estimate.tdee_bias,
                estimate.variance,
                estimate.adjusted_tdee,
                estimate.method,
                estimate.policy_version,
                estimate.interval_start.isoformat() if estimate.interval_start else None,
                estimate.interval_end.isoformat() if estimate.interval_end else None,
                estimate.weight_observations,
                estimate.complete_intake_days,
                estimate.partial_intake_days,
                estimate.input_fingerprint,
            ),
        )
        conn.commit()
        return cursor.lastrowid or 0

    @staticmethod
    def get_latest_estimate(conn: sqlite3.Connection, user_id: int) -> TDEEEstimate | None:
        """Get the most recent TDEE estimate."""
        row = conn.execute(
            TDEEQueries._SELECT
            + " WHERE user_id = ? ORDER BY estimated_at DESC, estimate_id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return TDEEQueries._from_row(row) if row is not None else None

    @staticmethod
    def get_estimate_history(
        conn: sqlite3.Connection, user_id: int, limit: int = 10
    ) -> list[TDEEEstimate]:
        """Get TDEE estimate history in chronological order."""
        rows = conn.execute(
            TDEEQueries._SELECT
            + " WHERE user_id = ? ORDER BY estimated_at DESC, estimate_id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [TDEEQueries._from_row(row) for row in reversed(rows)]
