"""Weight, intake, and expenditure tracking.

Key components:
- Gap-aware EMA trend calculation for weight display
- Dated actual/planned intake observations with coverage semantics
- Effective-dated goals and activity context
- Calendar-aware expenditure estimation and bounded recommendations
"""

from __future__ import annotations

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
from llmn.tracking.service import TrackingAnalysis, analyze, analyze_and_persist
from llmn.tracking.tdee_filter import TDEEFilter

__all__ = [
    "ActivityPeriod",
    "CalorieEntry",
    "GoalPeriod",
    "IntakeEntry",
    "TDEEEstimate",
    "TDEEFilter",
    "TrackingAnalysis",
    "UserProfile",
    "WeightEntry",
    "analyze",
    "analyze_and_persist",
    "update_trend",
]
