"""Properties of the shared strategies in ``tests/strategies.py``.

They pin the family each strategy generates, so a later tweak that narrows
it (or lets it grow past its caps) fails here rather than silently changing
what every caller's property covers.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.strategies import (
    dictionaries_with_optional_keys,
    fixed_draw_lists,
    fixed_draw_one_of,
    json_values,
)


def _leaves_and_widths(value: object) -> tuple[int, int]:
    """The number of scalar leaves in ``value`` and its widest container."""
    if isinstance(value, dict):
        children = list(value.values())
    elif isinstance(value, list):
        children = value
    else:
        return 1, 0
    leaves, widest = 0, len(children)
    for child in children:
        child_leaves, child_widest = _leaves_and_widths(child)
        leaves += child_leaves
        widest = max(widest, child_widest)
    return leaves, widest


@pytest.mark.unit
@given(json_values)
def test_a_json_value_has_at_most_8_leaves_and_3_items_per_container(value):
    leaves, widest = _leaves_and_widths(value)
    assert leaves <= 8
    assert widest <= 3


@pytest.mark.unit
@given(st.data(), st.integers(0, 5), st.integers(0, 5))
def test_fixed_draw_lists_respects_its_bounds(data, a, b):
    min_size, max_size = sorted((a, b))
    value = data.draw(
        fixed_draw_lists(st.integers(), min_size=min_size, max_size=max_size)
    )
    assert min_size <= len(value) <= max_size
    assert all(isinstance(item, int) for item in value)


@pytest.mark.unit
@given(st.data())
def test_fixed_draw_lists_rejects_min_size_above_max_size(data):
    with pytest.raises(ValueError):
        data.draw(fixed_draw_lists(st.integers(), min_size=2, max_size=1))


@pytest.mark.unit
@given(fixed_draw_one_of(st.integers(), st.text(), st.booleans()))
def test_fixed_draw_one_of_returns_one_of_its_alternatives(value):
    assert isinstance(value, (int, str))  # bool is an int


@pytest.mark.unit
@given(
    dictionaries_with_optional_keys(
        {"a": st.integers()}, {"b": st.text(), "c": st.booleans()}
    )
)
def test_dictionaries_with_optional_keys_always_has_the_required_keys(value):
    assert "a" in value
    assert set(value) <= {"a", "b", "c"}
    assert isinstance(value["a"], int)
