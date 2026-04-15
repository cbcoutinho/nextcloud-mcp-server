"""Unit tests for Nutrition model numeric coercion (issue #708)."""

import pytest
from pydantic import ValidationError

from nextcloud_mcp_server.models.cookbook import Nutrition

NUTRITION_FIELDS = [
    "calories",
    "carbohydrateContent",
    "cholesterolContent",
    "fatContent",
    "fiberContent",
    "proteinContent",
    "saturatedFatContent",
    "servingSize",
    "sodiumContent",
    "sugarContent",
    "transFatContent",
    "unsaturatedFatContent",
]


@pytest.mark.unit
@pytest.mark.parametrize("field", NUTRITION_FIELDS)
def test_nutrition_field_accepts_string(field: str):
    """String values should be accepted as-is for every nutrition field."""
    n = Nutrition(**{field: "650 kcal"})
    assert getattr(n, field) == "650 kcal"


@pytest.mark.unit
@pytest.mark.parametrize("field", NUTRITION_FIELDS)
def test_nutrition_field_coerces_int(field: str):
    """Integer values should be coerced to strings for every nutrition field."""
    n = Nutrition(**{field: 260})
    assert getattr(n, field) == "260"


@pytest.mark.unit
@pytest.mark.parametrize("field", NUTRITION_FIELDS)
def test_nutrition_field_coerces_float(field: str):
    """Float values should be coerced to strings for every nutrition field."""
    n = Nutrition(**{field: 260.5})
    assert getattr(n, field) == "260.5"


@pytest.mark.unit
@pytest.mark.parametrize("field", NUTRITION_FIELDS)
def test_nutrition_field_accepts_none(field: str):
    """None should remain None for every nutrition field."""
    n = Nutrition(**{field: None})
    assert getattr(n, field) is None


@pytest.mark.unit
@pytest.mark.parametrize("field", NUTRITION_FIELDS)
def test_nutrition_field_rejects_bool(field: str):
    """Boolean values should not be silently coerced to strings."""
    with pytest.raises(ValidationError):
        Nutrition(**{field: True})


@pytest.mark.unit
def test_nutrition_mixed_types():
    """Nutrition model should handle a mix of string, int, and None values."""
    n = Nutrition(
        calories="650 kcal",
        proteinContent=18,
        fatContent=None,
    )
    assert n.calories == "650 kcal"
    assert n.proteinContent == "18"
    assert n.fatContent is None
