"""Body-composition comparison and recomposition feedback.

Scale weight alone cannot distinguish fat loss from muscle gain, so
recomposition questions need at least two comparable body-composition
measurements. This module compares two :class:`BodyComposition` rows and
refuses to over-read changes that fall inside measurement noise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from llmn.tracking.models import BodyComposition

# Changes below this magnitude are treated as measurement noise. DEXA body-fat
# precision is commonly quoted as 1-2 percentage points; 1.0 lb is a
# deliberately conservative floor rather than a claimed detection limit.
CHANGE_NOISE_LBS = 1.0
SHORT_INTERVAL_DAYS = 28


@dataclass
class BodyCompDelta:
    """Change between two body-composition measurements."""

    start: BodyComposition
    end: BodyComposition
    days: int
    weeks: float
    weight_change_lbs: float | None
    fat_mass_change_lbs: float | None
    fat_free_mass_change_lbs: float | None
    body_fat_pct_change: float | None
    waist_change_inches: float | None
    classification: str
    summary: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "days": self.days,
            "weeks": round(self.weeks, 1),
            "weight_change_lbs": _round(self.weight_change_lbs, 1),
            "fat_mass_change_lbs": _round(self.fat_mass_change_lbs, 1),
            "fat_free_mass_change_lbs": _round(self.fat_free_mass_change_lbs, 1),
            "body_fat_pct_change": _round(self.body_fat_pct_change, 1),
            "waist_change_inches": _round(self.waist_change_inches, 1),
            "fat_mass_rate_lbs_per_week": (
                _round(self.fat_mass_change_lbs / self.weeks, 2)
                if self.fat_mass_change_lbs is not None and self.weeks
                else None
            ),
            "fat_free_mass_rate_lbs_per_week": (
                _round(self.fat_free_mass_change_lbs / self.weeks, 2)
                if self.fat_free_mass_change_lbs is not None and self.weeks
                else None
            ),
            "classification": self.classification,
            "summary": self.summary,
            "warnings": list(self.warnings),
        }


def _round(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _change(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    return end - start


def _classify(fat_change: float | None, lean_change: float | None) -> str:
    if fat_change is None or lean_change is None:
        return "insufficient_data"
    fat_signal = abs(fat_change) >= CHANGE_NOISE_LBS
    lean_signal = abs(lean_change) >= CHANGE_NOISE_LBS
    if not fat_signal and not lean_signal:
        return "within_noise"
    if fat_signal and fat_change < 0 and lean_signal and lean_change > 0:
        return "recomposition"
    if fat_signal and fat_change < 0 and not lean_signal:
        return "fat_loss_only"
    if fat_signal and fat_change < 0 and lean_signal and lean_change < 0:
        return "fat_and_lean_loss"
    if lean_signal and lean_change > 0 and not fat_signal:
        return "lean_gain"
    if fat_signal and fat_change > 0 and lean_signal and lean_change > 0:
        return "gain"
    if fat_signal and fat_change > 0 and lean_signal and lean_change < 0:
        return "unfavorable"
    if lean_signal and lean_change < 0 and not fat_signal:
        return "lean_loss"
    return "mixed"


_CLASSIFICATION_TEXT = {
    "recomposition": "recomposition (fat mass down, fat-free mass up)",
    "fat_loss_only": "fat loss with fat-free mass preserved",
    "fat_and_lean_loss": "fat loss accompanied by fat-free mass loss",
    "lean_gain": "fat-free mass gain with fat mass stable",
    "gain": "both fat mass and fat-free mass up",
    "unfavorable": "fat mass up while fat-free mass down",
    "lean_loss": "fat-free mass down",
    "within_noise": "no change beyond measurement noise",
    "mixed": "mixed changes inside or across noise thresholds",
    "insufficient_data": "not enough measurements to compare fat and lean mass",
    "not_comparable": "measurements used different methods and are not comparable",
    "same_day": "the two measurements are on the same date",
}


def compare_compositions(
    start: BodyComposition,
    end: BodyComposition,
) -> BodyCompDelta:
    """Compare two measurements without over-reading measurement noise."""
    days = (end.measured_at - start.measured_at).days
    weeks = days / 7.0 if days > 0 else 0.0

    weight_change = _change(start.weight_lbs, end.weight_lbs)
    fat_change = _change(start.derived_fat_mass_lbs, end.derived_fat_mass_lbs)
    lean_change = _change(start.derived_fat_free_mass_lbs, end.derived_fat_free_mass_lbs)
    bf_change = _change(start.derived_body_fat_pct, end.derived_body_fat_pct)
    waist_change = _change(start.waist_inches, end.waist_inches)

    warnings: list[str] = []
    if start.method != end.method:
        classification = "not_comparable"
        warnings.append(
            f"Compared a {start.method} measurement with a {end.method} measurement. "
            "Cross-method body-fat values are not directly comparable; repeat with the "
            "same method before drawing conclusions."
        )
    elif days <= 0:
        classification = "same_day"
        warnings.append("Choose two measurements on different dates.")
    else:
        classification = _classify(fat_change, lean_change)
        if days < SHORT_INTERVAL_DAYS:
            warnings.append(
                f"Interval is {days} day(s); changes under {CHANGE_NOISE_LBS:.0f} lb "
                "are within measurement error. Recomposition is best judged over "
                "8-12 weeks."
            )
        if classification == "fat_and_lean_loss" or classification == "lean_loss":
            warnings.append(
                "Fat-free mass fell. Check protein intake, resistance training, and "
                "the size of the deficit before cutting calories further."
            )

    if weight_change is None and fat_change is None:
        warnings.append("No weight or body-composition values recorded for comparison.")

    parts: list[str] = []
    if fat_change is not None:
        direction = "down" if fat_change < 0 else "up"
        parts.append(f"fat mass {direction} {abs(fat_change):.1f} lbs")
    if lean_change is not None:
        direction = "up" if lean_change > 0 else "down"
        parts.append(f"fat-free mass {direction} {abs(lean_change):.1f} lbs")
    if weight_change is not None:
        parts.append(f"weight {weight_change:+.1f} lbs")
    if bf_change is not None:
        parts.append(f"body fat {bf_change:+.1f} pts")
    if waist_change is not None:
        parts.append(f"waist {waist_change:+.1f} in")

    summary = f"{_CLASSIFICATION_TEXT.get(classification, classification).capitalize()}"
    if parts:
        summary += f" over {weeks:.1f} weeks: " + ", ".join(parts)
    summary += "."

    return BodyCompDelta(
        start=start,
        end=end,
        days=days,
        weeks=weeks,
        weight_change_lbs=weight_change,
        fat_mass_change_lbs=fat_change,
        fat_free_mass_change_lbs=lean_change,
        body_fat_pct_change=bf_change,
        waist_change_inches=waist_change,
        classification=classification,
        summary=summary,
        warnings=warnings,
    )


def latest_scan(entries: list[BodyComposition]) -> BodyComposition | None:
    """Return the most recent measurement, preferring full scans on a date."""
    if not entries:
        return None
    return max(entries, key=lambda entry: (entry.measured_at, entry.method == "dexa"))


def weight_measurements(entries: list[BodyComposition]) -> list[BodyComposition]:
    """Return entries that carry both weight and fat/lean information."""
    return [
        entry
        for entry in entries
        if entry.weight_lbs is not None and entry.derived_fat_mass_lbs is not None
    ]


def select_comparison(
    entries: list[BodyComposition],
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[BodyComposition, BodyComposition] | None:
    """Pick the first and last usable measurements for a comparison."""
    usable = weight_measurements(entries)
    if start_date is not None:
        usable = [entry for entry in usable if entry.measured_at >= start_date]
    if end_date is not None:
        usable = [entry for entry in usable if entry.measured_at <= end_date]
    if len(usable) < 2:
        return None
    usable.sort(key=lambda entry: entry.measured_at)
    return usable[0], usable[-1]
