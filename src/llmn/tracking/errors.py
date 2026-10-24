"""Structured errors for tracking and recommendation commands."""

from __future__ import annotations


class TrackingError(Exception):
    """An expected, machine-readable tracking failure."""

    def __init__(
        self,
        code: str,
        message: str,
        suggestions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestions = suggestions or []


class GoalValidationError(TrackingError):
    """A goal configuration that cannot be supported by the policy."""
