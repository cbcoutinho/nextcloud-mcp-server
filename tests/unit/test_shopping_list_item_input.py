"""Validation rules for the ``nc_shopping_list_add_items`` item schema.

These exist because the tool's ``items`` parameter is the one place in the
Shopping List surface where a caller hands over free-form objects, so the model
is what stops a typo from becoming a silently dropped field.
"""

import pytest
from pydantic import ValidationError

from nextcloud_mcp_server.models.shopping_list import ShoppingListItemInput

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("key", ["shop_area_id", "shopAreaId"])
def test_both_shop_area_spellings_are_accepted(key):
    """snake_case matches the sibling update tool, camelCase matches the app."""
    assert ShoppingListItemInput(name="flour", **{key: 7}).shop_area_id == 7


def test_a_misspelled_key_is_rejected_rather_than_dropped():
    """A silently ignored key would add the item missing a field the caller set."""
    with pytest.raises(ValidationError) as exc_info:
        ShoppingListItemInput(name="flour", shoparea=7)

    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    ("value", "expected"), [(3, "3"), (2.5, "2.5"), ("2", "2"), (None, None)]
)
def test_a_numeric_quantity_is_coerced_to_the_apps_string_column(value, expected):
    """A model sending `"quantity": 3` is obvious enough to accept, not reject."""
    assert ShoppingListItemInput(name="eggs", quantity=value).quantity == expected


def test_a_boolean_quantity_is_not_treated_as_a_number():
    """`True` is an int in Python — coercing it to "True" would be nonsense."""
    with pytest.raises(ValidationError):
        ShoppingListItemInput(name="eggs", quantity=True)


def test_an_empty_name_is_rejected():
    """The app would happily store a nameless row, which no caller wants."""
    with pytest.raises(ValidationError):
        ShoppingListItemInput(name="")


def test_only_name_is_required():
    item = ShoppingListItemInput(name="milk")

    assert (item.quantity, item.unit, item.shop_area_id, item.checked) == (
        None,
        None,
        None,
        False,
    )
