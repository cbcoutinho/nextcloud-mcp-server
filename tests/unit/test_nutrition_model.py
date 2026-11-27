"""Unit tests for Nutrition model numeric coercion (issue #708)."""

import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st
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


@st.composite
def nutrition_values(draw) -> tuple[object, str | None]:
    """A value as Cookbook may store it (schema.org allows text or a number),
    and what the model holds."""
    value = draw(st.text() | st.integers() | st.floats() | st.none())
    event(type(value).__name__)
    return value, value if value is None or isinstance(value, str) else str(value)


@pytest.mark.unit
@given(st.sampled_from(NUTRITION_FIELDS), nutrition_values())
@example("calories", ("650 kcal", "650 kcal"))
@example("calories", (260, "260"))
@example("calories", (260.5, "260.5"))
def test_nutrition_field_keeps_text_and_numbers_as_text(field: str, case):
    value, expected = case
    assert getattr(Nutrition(**{field: value}), field) == expected


@pytest.mark.unit
@given(st.sampled_from(NUTRITION_FIELDS), st.booleans())
def test_nutrition_field_rejects_bool(field: str, value: bool):
    """Boolean values should not be silently coerced to strings."""
    with pytest.raises(ValidationError):
        Nutrition(**{field: value})


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
