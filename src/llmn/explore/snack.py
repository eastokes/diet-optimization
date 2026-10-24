"""Suggest small, complementary food combinations for remaining macros.

The command answers a practical question: "I have 300 kcal, 30 g protein,
and tight carb/fat caps left today. What snack actually fits?" It searches
small combinations (1-3 foods), optimizes portions with a tiny QP, and ranks
the results by macro fit and pairing compatibility.
"""

from __future__ import annotations

import math
import re
import sqlite3
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy.sparse import csc_matrix  # type: ignore[import-untyped]

from llmn.data.food_categories import FoodCategory, classify_food
from llmn.export.llm_prompt import clean_food_name_with_context

ENERGY_ID = 1008
PROTEIN_ID = 1003
FAT_ID = 1004
CARB_ID = 1005
FIBER_ID = 1079

# Per-100g category defaults used when the USDA serving table has no entry.
CATEGORY_TARGET_GRAMS: dict[FoodCategory, float] = {
    FoodCategory.PROTEIN: 100.0,
    FoodCategory.VEGETABLE: 100.0,
    FoodCategory.FRUIT: 100.0,
    FoodCategory.FAT: 20.0,
    FoodCategory.CARB: 50.0,
    FoodCategory.LEGUME: 100.0,
    FoodCategory.MIXED: 100.0,
}

CATEGORY_BOUNDS: dict[FoodCategory, tuple[float, float]] = {
    FoodCategory.PROTEIN: (20.0, 200.0),
    FoodCategory.VEGETABLE: (15.0, 250.0),
    FoodCategory.FRUIT: (15.0, 250.0),
    FoodCategory.FAT: (5.0, 40.0),
    FoodCategory.CARB: (15.0, 150.0),
    FoodCategory.LEGUME: (20.0, 250.0),
    FoodCategory.MIXED: (15.0, 200.0),
}

SNACK_KEYWORDS: tuple[tuple[str, float], ...] = (
    ("yogurt", 2.0),
    ("cottage cheese", 2.0),
    ("jerky", 1.8),
    ("hummus", 1.8),
    ("edamame", 1.6),
    ("tuna", 1.4),
    ("salmon", 1.2),
    ("sardine", 1.2),
    ("chicken", 1.0),
    ("turkey", 1.0),
    ("egg", 1.0),
    ("cheese", 1.0),
    ("milk", 0.6),
    ("almond", 1.6),
    ("peanut", 1.4),
    ("walnut", 1.2),
    ("cashew", 1.2),
    ("pistachio", 1.2),
    ("seed", 1.0),
    ("avocado", 1.5),
    ("apple", 1.5),
    ("banana", 1.2),
    ("berr", 1.6),
    ("orange", 1.0),
    ("grape", 1.0),
    ("kiwi", 1.0),
    ("peach", 0.7),
    ("pear", 0.7),
    ("carrot", 1.5),
    ("celery", 1.5),
    ("cucumber", 1.5),
    ("pepper", 0.6),
    ("tomato", 0.6),
    ("broccoli", 0.6),
    ("oat", 1.0),
    ("cracker", 1.0),
    ("rice cake", 1.4),
    ("popcorn", 1.0),
    ("bread", 0.5),
    ("tortilla", 0.5),
    ("dark chocolate", 1.0),
    ("protein", 1.2),
)

ANIMAL_KEYWORDS = (
    "chicken",
    "beef",
    "pork",
    "turkey",
    "fish",
    "salmon",
    "tuna",
    "cod",
    "shrimp",
    "egg",
    "meat",
    "poultry",
)

BAD_DESCRIPTION_KEYWORDS = (
    "baby food",
    "babyfood",
    "infant formula",
    "restaurant",
    "fast food",
    "school lunch",
)

ANIMAL_PATTERN = re.compile(r"\b(?:" + "|".join(ANIMAL_KEYWORDS) + r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class SnackRequest:
    """User's remaining-macro target."""

    calories: float
    protein_min: float = 0.0
    max_carbs: float | None = None
    max_fat: float | None = None
    calorie_tolerance: float = 30.0
    max_foods: int = 3
    limit: int = 3
    include_tag: str | None = None
    exclude_tags: tuple[str, ...] = ("exclude",)


@dataclass(frozen=True)
class SnackFood:
    """A candidate food with its compatible portion bounds."""

    fdc_id: int
    description: str
    display_name: str
    prep_state: str
    kcal_per_100g: float
    protein_per_100g: float
    carbs_per_100g: float
    fat_per_100g: float
    fiber_per_100g: float
    category: FoodCategory
    tags: tuple[str, ...]
    serving_description: str | None
    serving_grams: float | None
    target_grams: float
    min_grams: float
    max_grams: float
    affinity: float

    @property
    def label(self) -> str:
        if self.prep_state:
            return f"{self.display_name} ({self.prep_state})"
        return self.display_name


@dataclass(frozen=True)
class FoodPortion:
    """One food inside a suggestion."""

    food: SnackFood
    grams: float
    calories: float
    protein: float
    carbs: float
    fat: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "fdc_id": self.food.fdc_id,
            "description": self.food.description,
            "display_name": self.food.label,
            "category": self.food.category.value,
            "grams": round(self.grams, 1),
            "calories": round(self.calories, 1),
            "protein_g": round(self.protein, 1),
            "carbs_g": round(self.carbs, 1),
            "fat_g": round(self.fat, 1),
            "serving": (
                {
                    "description": self.food.serving_description,
                    "grams": self.food.serving_grams,
                }
                if self.food.serving_description
                else None
            ),
            "tags": list(self.food.tags),
        }


@dataclass(frozen=True)
class SnackSuggestion:
    """A feasible snack combination."""

    portions: tuple[FoodPortion, ...]
    calories: float
    protein: float
    carbs: float
    fat: float
    score: float
    calorie_error: float
    pairing_notes: tuple[str, ...]

    @property
    def food_ids(self) -> frozenset[int]:
        return frozenset(portion.food.fdc_id for portion in self.portions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "foods": [portion.to_dict() for portion in self.portions],
            "totals": {
                "calories": round(self.calories, 1),
                "protein_g": round(self.protein, 1),
                "carbs_g": round(self.carbs, 1),
                "fat_g": round(self.fat, 1),
            },
            "score": round(self.score, 1),
            "calorie_error": round(self.calorie_error, 1),
            "pairing_notes": list(self.pairing_notes),
        }


@dataclass(frozen=True)
class SnackResult:
    """Result of a snack search."""

    request: SnackRequest
    suggestions: tuple[SnackSuggestion, ...]
    reason_codes: tuple[str, ...]
    warnings: tuple[str, ...]
    candidate_count: int
    feasible_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": {
                "calories": self.request.calories,
                "protein_min_g": self.request.protein_min,
                "max_carbs_g": self.request.max_carbs,
                "max_fat_g": self.request.max_fat,
                "calorie_tolerance": self.request.calorie_tolerance,
            },
            "suggestions": [suggestion.to_dict() for suggestion in self.suggestions],
            "reason_codes": list(self.reason_codes),
            "candidate_count": self.candidate_count,
            "feasible_count": self.feasible_count,
        }


def validate_request(request: SnackRequest) -> None:
    """Reject nonsensical targets before doing any work."""
    values = {
        "calories": request.calories,
        "protein": request.protein_min,
        "max_carbs": request.max_carbs,
        "max_fat": request.max_fat,
        "calorie_tolerance": request.calorie_tolerance,
    }
    for name, value in values.items():
        if value is not None and not math.isfinite(value):
            raise ValueError(f"{name} must be finite")
    if request.calories <= 0:
        raise ValueError("calories must be greater than zero")
    if request.protein_min < 0:
        raise ValueError("protein must be zero or greater")
    if request.max_carbs is not None and request.max_carbs < 0:
        raise ValueError("max_carbs must be zero or greater")
    if request.max_fat is not None and request.max_fat < 0:
        raise ValueError("max_fat must be zero or greater")
    if request.calorie_tolerance < 0:
        raise ValueError("calorie_tolerance must be zero or greater")
    if request.max_foods < 1 or request.max_foods > 3:
        raise ValueError("max_foods must be between 1 and 3")
    if request.limit < 1 or request.limit > 10:
        raise ValueError("limit must be between 1 and 10")


def suggest_snacks(conn: sqlite3.Connection, request: SnackRequest) -> SnackResult:
    """Return ranked snack suggestions for a remaining-macro target."""
    validate_request(request)

    raw_foods = _load_raw_foods(conn, request)
    if not raw_foods:
        return SnackResult(
            request=request,
            suggestions=(),
            reason_codes=("NO_CANDIDATES",),
            warnings=(
                "No foods matched the active filters. Try removing --tag or check the database.",
            ),
            candidate_count=0,
            feasible_count=0,
        )

    servings = _load_servings(conn, [row["fdc_id"] for row in raw_foods])
    foods: list[SnackFood] = []
    for row in raw_foods:
        food = _build_food(row, servings.get(row["fdc_id"]))
        if food is not None:
            foods.append(food)

    anchors, supports = _select_pool(foods, request)
    if not anchors:
        return SnackResult(
            request=request,
            suggestions=(),
            reason_codes=("NO_PROTEIN_ANCHORS",),
            warnings=(
                "No protein-rich foods were available. Try removing --tag or loading more foods.",
            ),
            candidate_count=len(foods),
            feasible_count=0,
        )

    feasible: list[SnackSuggestion] = []
    for combo in _generate_combinations(anchors, supports, request.max_foods):
        suggestion = _optimize_combo(combo, request)
        if suggestion is not None:
            feasible.append(suggestion)

    if not feasible:
        return SnackResult(
            request=request,
            suggestions=(),
            reason_codes=("NO_FEASIBLE_COMBINATION",),
            warnings=_no_result_warnings(request),
            candidate_count=len(foods),
            feasible_count=0,
        )

    suggestions = _pick_diverse(feasible, request.limit)
    warnings: list[str] = []
    if request.max_foods == 1:
        warnings.append(
            "Only single-food suggestions were requested; allow more foods for combinations."
        )
    if request.include_tag:
        warnings.append(f"Only foods tagged '{request.include_tag}' were considered.")

    return SnackResult(
        request=request,
        suggestions=tuple(suggestions),
        reason_codes=(),
        warnings=tuple(warnings),
        candidate_count=len(foods),
        feasible_count=len(feasible),
    )


def _load_raw_foods(conn: sqlite3.Connection, request: SnackRequest) -> list[sqlite3.Row]:
    params: list[object] = []
    tag_filter = ""
    if request.include_tag:
        tag_filter = (
            "AND EXISTS (SELECT 1 FROM food_tags ft WHERE ft.fdc_id = f.fdc_id AND ft.tag = ?)"
        )
        params.append(request.include_tag.lower())

    exclude_filter = ""
    exclude_tags = tuple(tag.lower() for tag in request.exclude_tags if tag)
    if exclude_tags:
        placeholders = ",".join("?" for _ in exclude_tags)
        exclude_filter = (
            "AND NOT EXISTS (SELECT 1 FROM food_tags ft2 "
            f"WHERE ft2.fdc_id = f.fdc_id AND ft2.tag IN ({placeholders}))"
        )
        params.extend(exclude_tags)

    query = f"""
        SELECT
            f.fdc_id,
            f.description,
            MAX(CASE WHEN fn.nutrient_id = ? THEN fn.amount END) AS kcal,
            MAX(CASE WHEN fn.nutrient_id = ? THEN fn.amount END) AS protein,
            MAX(CASE WHEN fn.nutrient_id = ? THEN fn.amount END) AS carbs,
            MAX(CASE WHEN fn.nutrient_id = ? THEN fn.amount END) AS fat,
            MAX(CASE WHEN fn.nutrient_id = ? THEN fn.amount END) AS fiber,
            (SELECT GROUP_CONCAT(tag) FROM food_tags WHERE fdc_id = f.fdc_id) AS tags
        FROM foods f
        JOIN food_nutrients fn
            ON f.fdc_id = fn.fdc_id
           AND fn.nutrient_id IN (?, ?, ?, ?, ?)
        WHERE f.is_active = 1
        {tag_filter}
        {exclude_filter}
        GROUP BY f.fdc_id
        HAVING kcal IS NOT NULL
           AND protein IS NOT NULL
           AND carbs IS NOT NULL
           AND fat IS NOT NULL
        ORDER BY f.fdc_id
    """
    nutrient_params: list[object] = [
        ENERGY_ID,
        PROTEIN_ID,
        CARB_ID,
        FAT_ID,
        FIBER_ID,
        ENERGY_ID,
        PROTEIN_ID,
        CARB_ID,
        FAT_ID,
        FIBER_ID,
    ]
    return list(conn.execute(query, nutrient_params + params).fetchall())


def _load_servings(
    conn: sqlite3.Connection, fdc_ids: list[int]
) -> dict[int, list[tuple[str, float]]]:
    if not fdc_ids:
        return {}

    result: dict[int, list[tuple[str, float]]] = {}
    chunk_size = 500
    for start in range(0, len(fdc_ids), chunk_size):
        chunk = fdc_ids[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT fdc_id, portion_description, gram_weight
            FROM servings
            WHERE fdc_id IN ({placeholders}) AND gram_weight > 0
            """,
            chunk,
        ).fetchall()
        for row in rows:
            description = row["portion_description"]
            if not description:
                continue
            result.setdefault(int(row["fdc_id"]), []).append(
                (str(description), float(row["gram_weight"]))
            )
    return result


def _display_label(description: str) -> tuple[str, str]:
    name, prep_state = clean_food_name_with_context(description)
    lower = name.lower()
    if lower.startswith("vegetables, mixed") or lower.startswith("mixed vegetables"):
        name = "Mixed vegetables"
    elif lower.startswith("nuts, mixed nuts") or lower.startswith("mixed nuts"):
        name = "Mixed nuts"
    elif "tahini" in lower or "sesame butter" in lower:
        name = "Tahini"
    elif lower.startswith("turkey"):
        name = "Turkey"
    elif "whey protein" in lower:
        name = "Whey protein powder"
    elif lower.startswith("beverages") and "protein powder" in lower:
        name = "Protein powder"
    elif lower.startswith("cereals ready-to-eat"):
        name = "Cereal"
    return name, prep_state


def _is_raw_animal(description_lower: str) -> bool:
    return bool(ANIMAL_PATTERN.search(description_lower)) and (
        "raw" in description_lower or "uncooked" in description_lower
    )


def _is_ingredient_protein(description_lower: str) -> bool:
    if "stabilized" in description_lower or "glucose reduced" in description_lower:
        return True
    if any(
        keyword in description_lower
        for keyword in (
            "protein concentrate",
            "protein isolate",
            "soy protein",
            "textured vegetable protein",
            "vital wheat gluten",
        )
    ):
        return True
    if any(keyword in description_lower for keyword in ("egg", "milk", "whey", "casein")):
        if "dried" in description_lower or "dry" in description_lower:
            return True
        if "powder" in description_lower and "protein" not in description_lower:
            return True
        if "flakes" in description_lower:
            return True
    return False


def _is_pure_fat(description_lower: str) -> bool:
    if any(keyword in description_lower for keyword in ("shortening", "lard", "tallow")):
        return True
    if description_lower.startswith("oil,") or description_lower.startswith("oil "):
        return True
    if any(
        keyword in description_lower for keyword in ("vegetable oil", "canola oil", "soybean oil")
    ):
        return True
    return description_lower.startswith("olive oil")


def _build_food(
    row: sqlite3.Row,
    servings: list[tuple[str, float]] | None,
) -> SnackFood | None:
    description = str(row["description"])
    description_lower = description.lower()
    if any(keyword in description_lower for keyword in BAD_DESCRIPTION_KEYWORDS):
        return None
    if _is_pure_fat(description_lower):
        return None
    if _is_raw_animal(description_lower) or _is_ingredient_protein(description_lower):
        return None

    kcal = float(row["kcal"] or 0.0)
    protein = float(row["protein"] or 0.0)
    carbs = float(row["carbs"] or 0.0)
    fat = float(row["fat"] or 0.0)
    fiber = float(row["fiber"] or 0.0)
    if kcal <= 0 or protein < 0 or carbs < 0 or fat < 0:
        return None

    category = classify_food(
        protein_per_100g=protein,
        carbs_per_100g=carbs,
        fat_per_100g=fat,
        fiber_per_100g=fiber,
        energy_per_100g=kcal,
        description=description,
    )

    if (
        category == FoodCategory.PROTEIN
        and "dried" in description_lower
        and "jerky" not in description_lower
    ):
        return None

    min_grams, max_grams = CATEGORY_BOUNDS[category]
    if kcal > 700:
        max_grams = min(max_grams, 25.0)
    elif kcal > 500:
        max_grams = min(max_grams, 50.0)

    default_target = CATEGORY_TARGET_GRAMS[category]
    serving_description, serving_grams = _choose_serving(
        servings or [], default_target, min_grams, max_grams
    )
    target_grams = serving_grams if serving_grams is not None else default_target
    target_grams = min(max(target_grams, min_grams), max_grams)

    tags = tuple(sorted(tag.lower() for tag in str(row["tags"] or "").split(",") if tag.strip()))
    display_name, prep_state = _display_label(description)

    return SnackFood(
        fdc_id=int(row["fdc_id"]),
        description=description,
        display_name=display_name,
        prep_state=prep_state,
        kcal_per_100g=kcal,
        protein_per_100g=protein,
        carbs_per_100g=carbs,
        fat_per_100g=fat,
        fiber_per_100g=fiber,
        category=category,
        tags=tags,
        serving_description=serving_description,
        serving_grams=serving_grams,
        target_grams=target_grams,
        min_grams=min_grams,
        max_grams=max_grams,
        affinity=_affinity(description, category, kcal, protein, tags),
    )


def _choose_serving(
    servings: list[tuple[str, float]],
    default_target: float,
    min_grams: float,
    max_grams: float,
) -> tuple[str | None, float | None]:
    if not servings:
        return None, None
    candidates = [
        (description, grams)
        for description, grams in servings
        if min_grams <= grams <= max_grams * 1.5
    ]
    if not candidates:
        return None, None
    description, grams = min(candidates, key=lambda item: abs(item[1] - default_target))
    return description, grams


def _affinity(
    description: str,
    category: FoodCategory,
    kcal_per_100g: float,
    protein_per_100g: float,
    tags: tuple[str, ...],
) -> float:
    text = description.lower()
    score = 0.0
    for keyword, weight in SNACK_KEYWORDS:
        if keyword in text:
            score += weight

    is_animal = bool(ANIMAL_PATTERN.search(text))
    if is_animal and ("raw" in text or "uncooked" in text):
        score -= 5.0
    if "cooked" in text or "boiled" in text or "roasted" in text or "baked" in text:
        score += 0.5
    if "canned" in text:
        score += 0.5
    if "flour" in text or "cornstarch" in text:
        score -= 2.0
    if "sugar" in text or "syrup" in text:
        score -= 2.0
    if any(
        keyword in text
        for keyword in (
            "candies",
            "candy",
            "cookies",
            "cake",
            "pie",
            "ice cream",
            "toaster pastry",
            "granola bar",
        )
    ):
        score -= 1.5
    if "staple" in tags:
        score += 2.0

    if category == FoodCategory.VEGETABLE and kcal_per_100g <= 60:
        score += 1.0
    if category == FoodCategory.FRUIT and kcal_per_100g <= 100:
        score += 0.5
    if category == FoodCategory.FAT and any(
        keyword in text
        for keyword in ("nut", "almond", "peanut", "walnut", "cashew", "seed", "avocado")
    ):
        score += 1.0
    if category == FoodCategory.CARB and any(
        keyword in text
        for keyword in ("oat", "cracker", "rice cake", "popcorn", "bread", "tortilla")
    ):
        score += 1.0
    if category == FoodCategory.PROTEIN:
        score += min(3.0, protein_per_100g / 20.0)
    if any(
        keyword in text
        for keyword in ("chicken breast", "ground beef", "steak", "pork", "turkey breast")
    ):
        score -= 1.5

    return max(-10.0, min(10.0, score))


def _select_pool(
    foods: list[SnackFood], request: SnackRequest
) -> tuple[list[SnackFood], list[SnackFood]]:
    protein_foods = [
        food
        for food in foods
        if food.protein_per_100g >= 8.0
        and (food.protein_per_100g * 4.0 / max(food.kcal_per_100g, 1.0)) >= 0.20
    ]
    if request.protein_min <= 0:
        anchors = sorted(foods, key=lambda food: food.affinity, reverse=True)[:30]
    elif protein_foods:
        anchors = sorted(protein_foods, key=_anchor_key, reverse=True)[:30]
    else:
        anchors = sorted(foods, key=_anchor_key, reverse=True)[:30]

    anchor_ids = {food.fdc_id for food in anchors}
    per_category = 6 if len(foods) > 200 else 12
    support_categories = (
        FoodCategory.VEGETABLE,
        FoodCategory.FRUIT,
        FoodCategory.CARB,
        FoodCategory.LEGUME,
        FoodCategory.FAT,
        FoodCategory.MIXED,
    )
    supports: list[SnackFood] = []
    for category in support_categories:
        group = [
            food for food in foods if food.category == category and food.fdc_id not in anchor_ids
        ]
        group.sort(key=_support_key, reverse=True)
        supports.extend(group[:per_category])

    return anchors, supports


def _anchor_key(food: SnackFood) -> float:
    protein_density = food.protein_per_100g / max(food.kcal_per_100g, 1.0) * 100.0
    return food.affinity * 1.5 + protein_density * 0.8 + food.protein_per_100g * 0.1


def _support_key(food: SnackFood) -> float:
    return food.affinity * 2.0 + (1.0 if food.kcal_per_100g <= 100 else 0.0)


def _generate_combinations(
    anchors: list[SnackFood],
    supports: list[SnackFood],
    max_foods: int,
) -> Iterable[tuple[SnackFood, ...]]:
    for anchor in anchors:
        yield (anchor,)

    if max_foods < 2:
        return

    for anchor in anchors:
        for support in supports:
            if support.fdc_id != anchor.fdc_id:
                yield (anchor, support)

    if max_foods < 3:
        return

    groups: dict[FoodCategory, list[SnackFood]] = {}
    for support in supports:
        groups.setdefault(support.category, []).append(support)

    patterns = (
        (FoodCategory.VEGETABLE, FoodCategory.FAT),
        (FoodCategory.FRUIT, FoodCategory.FAT),
        (FoodCategory.CARB, FoodCategory.FAT),
        (FoodCategory.LEGUME, FoodCategory.FAT),
        (FoodCategory.VEGETABLE, FoodCategory.CARB),
        (FoodCategory.FRUIT, FoodCategory.CARB),
        (FoodCategory.VEGETABLE, FoodCategory.LEGUME),
        (FoodCategory.FRUIT, FoodCategory.LEGUME),
        (FoodCategory.VEGETABLE, FoodCategory.FRUIT),
    )
    for anchor in anchors:
        for first_category, second_category in patterns:
            for first in groups.get(first_category, []):
                for second in groups.get(second_category, []):
                    if len({anchor.fdc_id, first.fdc_id, second.fdc_id}) < 3:
                        continue
                    yield (anchor, first, second)

    # A protein anchor plus three small supports is intentionally not generated
    # by default; three-food combinations cover the practical snack use case.


def _optimize_combo(combo: tuple[SnackFood, ...], request: SnackRequest) -> SnackSuggestion | None:
    if not _could_be_feasible(combo, request):
        return None

    n = len(combo)
    calories = np.array([food.kcal_per_100g / 100.0 for food in combo])
    protein = np.array([food.protein_per_100g / 100.0 for food in combo])
    carbs = np.array([food.carbs_per_100g / 100.0 for food in combo])
    fat = np.array([food.fat_per_100g / 100.0 for food in combo])
    targets = np.array([food.target_grams for food in combo])
    lower = np.array([food.min_grams for food in combo])
    upper = np.array([food.max_grams for food in combo])

    weight_calories = 0.2
    weight_protein = 1.0
    weight_portion = 0.0005
    P = (
        2.0
        * (
            weight_calories * np.outer(calories, calories)
            + weight_protein * np.outer(protein, protein)
        )
        + 2.0 * weight_portion * np.eye(n)
        + 1e-6 * np.eye(n)
    )
    q = -2.0 * (
        weight_calories * request.calories * calories
        + weight_protein * request.protein_min * protein
        + weight_portion * targets
    )

    G_rows: list[np.ndarray] = []
    h_values: list[float] = []

    # Leave a small headroom for whole-gram rounding so the displayed totals
    # still respect the user's caps.
    G_rows.append(-protein)
    h_values.append(-(request.protein_min + 0.5))
    if request.max_carbs is not None:
        G_rows.append(carbs)
        h_values.append(max(0.0, request.max_carbs - 0.5))
    if request.max_fat is not None:
        G_rows.append(fat)
        h_values.append(max(0.0, request.max_fat - 0.5))

    calorie_low = request.calories - request.calorie_tolerance
    calorie_high = request.calories + request.calorie_tolerance
    G_rows.append(-calories)
    h_values.append(-calorie_low)
    G_rows.append(calories)
    h_values.append(calorie_high)

    G = csc_matrix(np.vstack(G_rows))
    h = np.array(h_values)

    try:
        import qpsolvers

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            solution = qpsolvers.solve_qp(
                P=csc_matrix(P),
                q=q,
                G=G,
                h=h,
                lb=lower,
                ub=upper,
                solver="clarabel",
            )
    except Exception:
        return None

    if solution is None or not np.all(np.isfinite(solution)):
        return None

    grams = np.clip(np.round(np.maximum(solution, 0.0)), lower, upper)

    totals = {
        "calories": float(np.dot(calories, grams)),
        "protein": float(np.dot(protein, grams)),
        "carbs": float(np.dot(carbs, grams)),
        "fat": float(np.dot(fat, grams)),
    }

    if totals["protein"] < request.protein_min - 1e-6:
        return None
    if request.max_carbs is not None and totals["carbs"] > request.max_carbs + 1e-6:
        return None
    if request.max_fat is not None and totals["fat"] > request.max_fat + 1e-6:
        return None
    if abs(totals["calories"] - request.calories) > request.calorie_tolerance + 1.0:
        return None

    gram_total = float(np.sum(grams))
    if gram_total <= 0:
        return None

    pairing_score, notes = _pairing_score(combo, totals, request)
    protein_excess = max(0.0, totals["protein"] - request.protein_min)
    portion_error = sum(
        abs(float(grams[index]) - food.target_grams) / max(food.target_grams, 1.0)
        for index, food in enumerate(combo)
    )
    staple_bonus = sum(2.0 for food in combo if "staple" in food.tags)
    affinity_bonus = 0.4 * sum(food.affinity for food in combo)
    count_bonus = 1.5 if n == 2 else 1.0 if n == 3 else 0.0
    score = (
        100.0
        - 2.0 * abs(totals["calories"] - request.calories)
        - 0.4 * protein_excess
        - 1.0 * portion_error
        + 1.5 * pairing_score
        + affinity_bonus
        + staple_bonus
        + count_bonus
    )

    portions = tuple(
        FoodPortion(
            food=food,
            grams=float(grams[index]),
            calories=float(calories[index] * grams[index]),
            protein=float(protein[index] * grams[index]),
            carbs=float(carbs[index] * grams[index]),
            fat=float(fat[index] * grams[index]),
        )
        for index, food in enumerate(combo)
    )

    return SnackSuggestion(
        portions=portions,
        calories=totals["calories"],
        protein=totals["protein"],
        carbs=totals["carbs"],
        fat=totals["fat"],
        score=score,
        calorie_error=abs(totals["calories"] - request.calories),
        pairing_notes=tuple(notes),
    )


def _could_be_feasible(combo: tuple[SnackFood, ...], request: SnackRequest) -> bool:
    max_calories = sum(food.kcal_per_100g * food.max_grams / 100.0 for food in combo)
    min_calories = sum(food.kcal_per_100g * food.min_grams / 100.0 for food in combo)
    max_protein = sum(food.protein_per_100g * food.max_grams / 100.0 for food in combo)
    min_carbs = sum(food.carbs_per_100g * food.min_grams / 100.0 for food in combo)
    min_fat = sum(food.fat_per_100g * food.min_grams / 100.0 for food in combo)

    if max_protein < request.protein_min - 0.5:
        return False
    if max_calories < request.calories - request.calorie_tolerance:
        return False
    if min_calories > request.calories + request.calorie_tolerance:
        return False
    if request.max_carbs is not None and min_carbs > request.max_carbs + 0.5:
        return False
    if request.max_fat is not None and min_fat > request.max_fat + 0.5:
        return False
    return True


def _protein_kind(food: SnackFood) -> str:
    text = food.description.lower()
    if any(keyword in text for keyword in ("yogurt", "cottage cheese", "cheese", "milk")):
        return "dairy"
    if "protein" in text:
        return "powder"
    if any(keyword in text for keyword in ("tuna", "salmon", "sardine", "fish", "cod", "shrimp")):
        return "fish"
    if "egg" in text:
        return "egg"
    if any(keyword in text for keyword in ("chicken", "turkey", "beef", "pork", "meat")):
        return "meat"
    return "other"


def _pairing_score(
    combo: tuple[SnackFood, ...],
    totals: dict[str, float],
    request: SnackRequest,
) -> tuple[float, list[str]]:
    categories = [food.category for food in combo]
    notes: list[str] = []
    score = 0.0
    protein_food = max(combo, key=lambda food: food.protein_per_100g)
    protein_kind = _protein_kind(protein_food)

    has_protein = FoodCategory.PROTEIN in categories or totals["protein"] >= max(
        10.0, request.protein_min
    )
    if has_protein and FoodCategory.FRUIT in categories:
        if protein_kind in ("dairy", "powder"):
            score += 3.0
            notes.append("protein + fruit")
        elif protein_kind == "egg":
            score += 1.5
            notes.append("protein + fruit")
        else:
            score += 0.5
            notes.append("protein + fruit (less common pairing)")
    if has_protein and FoodCategory.VEGETABLE in categories:
        if protein_kind in ("fish", "meat", "egg"):
            score += 3.0
            notes.append("protein + vegetable")
        elif protein_kind == "dairy":
            score += 2.0
            notes.append("protein + vegetable")
        else:
            score += 0.5
            notes.append("protein + vegetable (less common pairing)")
    if has_protein and FoodCategory.CARB in categories:
        score += 2.0
        notes.append("protein + carb")
    if has_protein and FoodCategory.FAT in categories:
        score += 1.0
        notes.append("protein + a small fat portion")
    if has_protein and FoodCategory.LEGUME in categories:
        score += 1.0
        notes.append("protein + legume")
    if FoodCategory.FAT in categories and FoodCategory.FRUIT in categories:
        score += 1.0
        notes.append("fruit + fat")
    if FoodCategory.FAT in categories and FoodCategory.VEGETABLE in categories:
        score += 1.0
        notes.append("vegetable + fat")

    if categories.count(FoodCategory.FAT) > 1:
        score -= 4.0
        notes.append("more than one fat source")
    if categories.count(FoodCategory.PROTEIN) > 1:
        score -= 4.0
        notes.append("more than one protein source")
    protein_sources = [
        food
        for food in combo
        if food.protein_per_100g >= 8.0
        and food.protein_per_100g * 4.0 / max(food.kcal_per_100g, 1.0) >= 0.20
    ]
    if len(protein_sources) > 1:
        score -= 3.0
        notes.append("multiple protein-dense foods")

    sweet_carb = any(
        food.category == FoodCategory.CARB
        and any(
            keyword in food.description.lower()
            for keyword in (
                "cereals ready-to-eat",
                "granola",
                "honey",
                "sweet",
                "candies",
                "cake",
                "cookie",
                "pie",
                "toaster pastry",
                "caramel",
                "chocolate",
                "candy",
            )
        )
        for food in combo
    )
    if sweet_carb and protein_kind in ("fish", "meat"):
        score -= 3.0
        notes.append("sweet carb with fish/meat is an unusual pairing")

    flavored_yogurt = any(
        "yogurt" in food.description.lower()
        and any(
            keyword in food.description.lower()
            for keyword in (
                "strawberry",
                "banana",
                "berry",
                "vanilla",
                "honey",
                "sweet",
                "fruit",
            )
        )
        for food in combo
    )
    if flavored_yogurt and protein_kind in ("fish", "meat"):
        score -= 3.0
        notes.append("sweetened yogurt with fish/meat is an unusual pairing")

    if len(set(categories)) == len(categories) and len(categories) > 1:
        score += 1.0
        notes.append("complementary categories")

    return score, notes


def _pick_diverse(suggestions: list[SnackSuggestion], limit: int) -> list[SnackSuggestion]:
    ordered = sorted(
        suggestions,
        key=lambda suggestion: (
            -suggestion.score,
            suggestion.calorie_error,
            len(suggestion.portions),
        ),
    )
    selected: list[SnackSuggestion] = []
    seen_names: set[tuple[str, ...]] = set()
    for suggestion in ordered:
        signature = tuple(
            sorted(portion.food.display_name.lower() for portion in suggestion.portions)
        )
        if signature in seen_names:
            continue
        if any(len(suggestion.food_ids & existing.food_ids) > 1 for existing in selected):
            continue
        selected.append(suggestion)
        seen_names.add(signature)
        if len(selected) >= limit:
            return selected

    for suggestion in ordered:
        signature = tuple(
            sorted(portion.food.display_name.lower() for portion in suggestion.portions)
        )
        if signature in seen_names:
            continue
        selected.append(suggestion)
        seen_names.add(signature)
        if len(selected) >= limit:
            break
    return selected


def _no_result_warnings(request: SnackRequest) -> tuple[str, ...]:
    warnings = [
        "No feasible snack combination matched those targets.",
        (
            "Try increasing --calorie-tolerance, relaxing --max-carbs/--max-fat, "
            "or lowering --protein."
        ),
    ]
    if request.include_tag:
        warnings.append(f"Try removing --tag {request.include_tag}.")
    if request.max_foods < 3:
        warnings.append("Try --max-foods 3 to allow more combinations.")
    return tuple(warnings)


def human_summary(result: SnackResult) -> str:
    """One-line summary for the response envelope."""
    if not result.suggestions:
        return "No snack combination matched those targets."
    top = result.suggestions[0]
    names = " + ".join(portion.food.display_name for portion in top.portions)
    return f"Top snack: {names} ({top.calories:.0f} kcal, {top.protein:.0f} g protein)."


def action_suggestions(result: SnackResult) -> list[str]:
    """Actionable next steps when no combination is feasible."""
    if result.suggestions:
        return []
    suggestions = [
        "Increase --calorie-tolerance to allow a wider calorie window.",
        "Relax --max-carbs or --max-fat.",
        "Lower --protein if it is not a hard requirement.",
    ]
    if result.request.include_tag:
        suggestions.append(f"Try removing --tag {result.request.include_tag}.")
    if result.request.max_foods < 3:
        suggestions.append("Try --max-foods 3.")
    return suggestions


def format_suggestion(index: int, suggestion: SnackSuggestion) -> list[str]:
    """Format one suggestion for human-readable CLI output."""
    names = " + ".join(portion.food.display_name for portion in suggestion.portions)
    lines = [f"{index}. {names}"]
    for portion in suggestion.portions:
        serving = ""
        if (
            portion.food.serving_description
            and portion.food.serving_grams
            and abs(portion.grams - portion.food.serving_grams)
            <= max(5.0, portion.food.serving_grams * 0.25)
        ):
            serving = f" ({portion.food.serving_description})"
        lines.append(f"   {portion.grams:.0f} g {portion.food.display_name}{serving}")
    lines.append(
        f"   Totals: {suggestion.calories:.0f} kcal | "
        f"{suggestion.protein:.1f} g protein | "
        f"{suggestion.carbs:.1f} g carbs | "
        f"{suggestion.fat:.1f} g fat"
    )
    if suggestion.pairing_notes:
        lines.append(f"   Why: {', '.join(suggestion.pairing_notes)}")
    return lines
