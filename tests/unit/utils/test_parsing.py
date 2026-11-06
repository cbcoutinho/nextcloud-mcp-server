"""Properties of the shared scalar parsers in ``utils/parsing.py``."""

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.utils.parsing import (
    _warn_unrecognised,
    parse_bool,
    parse_retry_after,
    split_csv,
)

pytestmark = pytest.mark.unit

_TRUE = ["1", "true", "yes", "on"]
_FALSE = ["0", "false", "no", "off", ""]


@st.composite
def spelled_flags(draw):
    """(text, meaning): a recognised spelling in any case and padding."""
    meaning = draw(st.booleans())
    word = draw(st.sampled_from(_TRUE if meaning else _FALSE))
    word = "".join(c.upper() if draw(st.booleans()) else c for c in word)
    pad = st.text(" \t\n", max_size=2)
    return draw(pad) + word + draw(pad), meaning


@given(spelled_flags(), st.booleans())
@example(("False", False), True)  # dynaconf hands back "False"; bool() says True
@example((" On ", True), False)
def test_recognised_spellings_ignore_default(flag, default):
    text, meaning = flag
    assert parse_bool(text, default) is meaning


@given(st.text(), st.booleans())
@example("ture", True)
@example("ture", False)
def test_unrecognised_spellings_yield_default(text, default):
    assume(text.strip().lower() not in _TRUE + _FALSE)
    assert parse_bool(text, default) is default


@given(st.booleans(), st.booleans())
def test_real_bools_pass_through(value, default):
    assert parse_bool(value, default) is value


@given(st.booleans())
def test_none_is_unset(default):
    assert parse_bool(None, default) is default


_items = st.text(min_size=1).map(str.strip).filter(lambda s: s and "," not in s)


@given(st.lists(_items), st.text(" \t", max_size=2))
@example([], "")
@example(["a.example", "b.example"], " ")
def test_split_csv_recovers_padded_items_and_drops_blanks(items, pad):
    raw = ",".join(f"{pad}{item}{pad},{pad}" for item in items)
    assert split_csv(raw) == items


@given(st.one_of(st.none(), st.text()))
@example(",")
def test_split_csv_items_are_stripped_and_non_empty(raw):
    assert all(item and item == item.strip() for item in split_csv(raw))


@given(st.integers(min_value=1, max_value=10**12), st.text(" \t", max_size=2))
def test_retry_after_reads_delta_seconds(seconds, pad):
    assert parse_retry_after(f"{pad}{seconds}{pad}") == seconds


@given(st.one_of(st.none(), st.text()))
@example("0")
@example("-5")
@example("+5")
@example("30.5")
@example("1_000")  # int() accepts it; RFC 9110 does not
@example("٢")  # Arabic-Indic digit: int() accepts it too
@example("Wed, 21 Oct 2026 07:28:00 GMT")  # HTTP-date: back off on our own
def test_retry_after_is_positive_or_none(raw):
    result = parse_retry_after(raw)
    assert result is None or (result > 0 and str(result) == raw.strip().lstrip("0"))


def test_unrecognised_spelling_is_logged_once_with_the_setting_name(caplog):
    """Once per (setting, value): COOKIE_SECURE is read on every login."""
    _warn_unrecognised.cache_clear()
    with caplog.at_level("WARNING", logger="nextcloud_mcp_server.utils.parsing"):
        assert parse_bool("ture", True, name="COOKIE_SECURE") is True
        assert parse_bool("ture", True, name="COOKIE_SECURE") is True
    assert caplog.text.count("COOKIE_SECURE='ture'") == 1
