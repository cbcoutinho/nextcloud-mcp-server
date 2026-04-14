"""Unit tests for Nutrition model numeric coercion (issue #708)."""

import pytest

from nextcloud_mcp_server.models.cookbook import Nutrition


@pytest.mark.unit
def test_nutrition_calories_accepts_string():
    """String calories values should be accepted as-is."""
    n = Nutrition(calories="650 kcal")
    assert n.calories == "650 kcal"


@pytest.mark.unit
def test_nutrition_calories_coerces_int():
    """Integer calories values should be coerced to strings."""
    n = Nutrition(calories=260)
    assert n.calories == "260"


@pytest.mark.unit
def test_nutrition_calories_coerces_float():
    """Float calories values should be coerced to strings."""
    n = Nutrition(calories=260.5)
    assert n.calories == "260.5"


@pytest.mark.unit
def test_nutrition_calories_accepts_none():
    """None calories should remain None."""
    n = Nutrition(calories=None)
    assert n.calories is None


@pytest.mark.unit
def test_nutrition_all_fields_coerce_int():
    """All nutrition content fields should coerce integer values to strings."""
    n = Nutrition(
        calories=260,
        carbohydrateContent=30,
        cholesterolContent=10,
        fatContent=15,
        fiberContent=5,
        proteinContent=20,
        saturatedFatContent=3,
        servingSize=1,
        sodiumContent=500,
        sugarContent=8,
        transFatContent=0,
        unsaturatedFatContent=12,
    )
    assert n.calories == "260"
    assert n.carbohydrateContent == "30"
    assert n.cholesterolContent == "10"
    assert n.fatContent == "15"
    assert n.fiberContent == "5"
    assert n.proteinContent == "20"
    assert n.saturatedFatContent == "3"
    assert n.servingSize == "1"
    assert n.sodiumContent == "500"
    assert n.sugarContent == "8"
    assert n.transFatContent == "0"
    assert n.unsaturatedFatContent == "12"


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
