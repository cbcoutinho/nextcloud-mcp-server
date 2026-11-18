"""Unit tests for the comment message splitter.

The property tests are the load-bearing ones: they encode the invariants that
decide whether a split comment can actually be posted and whether the text
survives the round trip, over generated messages that mix markdown, CJK, emoji,
mentions, unbreakable runs and the whitespace PHP and Python disagree on. The
example tests pin the packing and error behaviour those properties do not fix.
"""

import re

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.utils.message_splitter import (
    _PHP_TRIM_CHARS,
    MENTION_PATTERN,
    PART_PREFIX_TEMPLATE,
    measured_length,
    split_message,
)

pytestmark = pytest.mark.unit

LIMIT = 1000

_PREFIX_RE = re.compile(r"^\((\d+)/(\d+)\) ")


def strip_prefix(part: str) -> str:
    """Drop the ``(i/N) `` marker a split part carries."""
    return _PREFIX_RE.sub("", part)


def visible(text: str) -> str:
    """The characters a split must keep: all but whitespace and PHP-trimmed NUL."""
    return "".join(c for c in text if not c.isspace() and c != "\0")


# Strategies ----------------------------------------------------------------

_tokens = st.one_of(
    st.from_regex(r"[A-Za-z]{1,12}", fullmatch=True),
    st.sampled_from(["指定された文書を解析し", "🚀", "✅", "é", "café"]),
    st.from_regex(r"@[a-z][a-z.]{0,8}", fullmatch=True),
    st.from_regex(r'@"[a-z]{1,5} [a-z]{1,5}"', fullmatch=True),
    st.from_regex(r"@[a-z]{1,5}@example\.com", fullmatch=True),
    st.from_regex(r"x{20,90}", fullmatch=True),  # no separator: hard-cut path
)
_separators = st.sampled_from(
    [" ", "\u00a0", "\u202f", ". ", "! ", "\n", "\n\n", "\n## ", "\n```\n", "\t"]
    + ["\u3000", "\0"]
)


@st.composite
def messages(draw):
    """Separator-joined tokens, padded with what either trim rule removes."""
    # min_size keeps most messages long enough to split (measured: 25% split
    # without it, 88% with it); short messages are covered by the "fits" test.
    words = draw(st.lists(st.tuples(_tokens, _separators), min_size=10, max_size=150))
    pad = st.text(alphabet=_PHP_TRIM_CHARS + "\u3000", max_size=3)
    return draw(pad) + "".join(t + s for t, s in words) + draw(pad)


max_lengths = st.integers(min_value=30, max_value=250)
templates = st.sampled_from([PART_PREFIX_TEMPLATE, None])

# 1000-1400 words crosses 9 -> 10 parts at LIMIT, where the prefix widens from
# 6 to 8 characters: exactly where a naive budget calculation overflows.
_WIDTH_BOUNDARY = " ".join(f"word{i:04d}" for i in range(1150))
_LONG_MENTION = '@"' + "n" * 300 + '" trailing words here'


# Properties ----------------------------------------------------------------


@given(messages(), max_lengths, templates)
@example(_WIDTH_BOUNDARY, LIMIT, PART_PREFIX_TEMPLATE)
@example(_LONG_MENTION, 100, PART_PREFIX_TEMPLATE)  # length outranks mentions
@example("x" * LIMIT + "\u3000", LIMIT, PART_PREFIX_TEMPLATE)  # server counts it
def test_every_part_fits_and_is_numbered_consistently(message, max_length, template):
    """Whether the POST succeeds: measured with the prefix applied, because
    that is what gets posted. Every part must agree on the total."""
    parts = split_message(message, max_length=max_length, part_prefix_template=template)

    assert all(measured_length(p) <= max_length for p in parts)
    if len(parts) <= 1 or template is None:
        assert not any(_PREFIX_RE.match(p) for p in parts)
        return
    numbering = [_PREFIX_RE.match(p) for p in parts]
    assert all(numbering), "every part of a split message carries a prefix"
    assert [int(m.group(1)) for m in numbering] == list(range(1, len(parts) + 1))
    assert {int(m.group(2)) for m in numbering} == {len(parts)}


@given(messages(), max_lengths, templates)
@example("z" * 1500, LIMIT, None)  # unbreakable run: hard cut, nothing lost
@example(_LONG_MENTION, 100, PART_PREFIX_TEMPLATE)
def test_round_trip_keeps_every_visible_character(message, max_length, template):
    """Parts are exact slices, so only boundary whitespace may be dropped."""
    parts = split_message(message, max_length=max_length, part_prefix_template=template)

    assert visible("".join(strip_prefix(p) for p in parts)) == visible(message)


def part_boundaries(text: str, parts: list[str]) -> list[int]:
    """Offsets in ``text`` where one part ends and the next begins.

    Located by walking each part body forward through the source rather than by
    rejoining: concatenating parts closes up the whitespace dropped at a cut,
    which would weld the tail of one part onto the head of the next and make a
    clean boundary look like a severed token.
    """
    if len(parts) < 2:
        return []  # a lone part is the untrimmed message: no cut to locate
    boundaries = []
    cursor = 0
    for part in parts:
        body = strip_prefix(part)
        start = text.index(body, cursor)
        cursor = start + len(body)
        boundaries.append(cursor)
    return boundaries[:-1]


@given(messages(), max_lengths)
@example("a" * 94 + ' @"alice smith" tail', 100)  # straddling mention moves whole
def test_no_mention_that_fits_is_split_across_parts(message, max_length):
    """A severed mention silently degrades to plain text, so cuts avoid any
    mention short enough to fit in a part."""
    parts = split_message(message, max_length=max_length)

    text = message.strip(_PHP_TRIM_CHARS)
    widest_prefix = len(PART_PREFIX_TEMPLATE.format(index=len(parts), total=len(parts)))
    budget = max_length - widest_prefix
    straddled = [
        (start, end, boundary)
        for start, end in (m.span() for m in MENTION_PATTERN.finditer(text))
        if end - start <= budget
        for boundary in part_boundaries(text, parts)
        if start < boundary < end
    ]
    assert not straddled, f"mentions cut by a part boundary: {straddled}"


@given(st.text(), st.integers(min_value=1, max_value=LIMIT))
@example("", LIMIT)
@example("\n\n  \t ", LIMIT)
@example("x" * LIMIT, LIMIT)  # the server's check is ">", so the limit is legal
@example("x" * LIMIT + "\n\n", LIMIT)  # trailing newlines are trimmed first
def test_blank_is_empty_and_a_message_that_fits_is_untouched(message, max_length):
    """Callers route every message through here, so the common case must not
    be perturbed: no prefix, no trimming."""
    length = measured_length(message)
    assume(length <= max_length)  # longer ones are the other properties' job

    expected = [] if length == 0 else [message]
    assert split_message(message, max_length=max_length) == expected


@given(
    st.text(),
    st.text(alphabet=_PHP_TRIM_CHARS),
    st.text(st.characters(categories=["Zs"], exclude_characters=" "), min_size=1),
)
@example("🚀é", "", "\u3000")  # code points, not bytes
@example("x" * LIMIT, "\0\0\0", "\u00a0\u202f")
def test_measured_length_trims_exactly_what_php_trims(core, php_pad, unicode_pad):
    """Core trims with PHP ``trim()`` before ``mb_strlen``: ASCII whitespace
    and NUL go, every other Unicode space (U+00A0, U+202F, U+3000) counts.

    Measuring with Python's broader ``strip()`` would report 1000 for a message
    the server counts as 1003, and narrower would reject what it would accept.
    """
    message = php_pad + "|" + core + "|" + unicode_pad + php_pad

    assert measured_length(message) == len(core) + 2 + len(unicode_pad)


# Examples ------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "z" * 1500,  # unbreakable run: hard-cut
        "y" * (LIMIT + 1),  # one character over
        "x" * LIMIT + "\u3000",  # over only by the server's count
    ],
)
def test_just_over_the_limit_packs_into_two_parts(message):
    """The properties bound every part from above; this pins the packing from
    below, so a splitter emitting needless extra comments cannot pass."""
    assert len(split_message(message, max_length=LIMIT)) == 2


def test_split_prefers_paragraph_boundaries_over_mid_sentence_cuts():
    paragraph = "Sentence one is here. Sentence two follows on. " * 12
    message = f"{paragraph}\n\n{paragraph}"

    parts = split_message(message, max_length=600)

    for part in parts:
        body = strip_prefix(part)
        assert not body.startswith(" ")
        # A cut mid-word would leave a fragment; every part ends on punctuation
        # or a complete word.
        assert body[-1].isalnum() or body[-1] in ".!?"


def test_non_convergence_fails_loudly_rather_than_silently(monkeypatch):
    """The fixed point cannot fail to settle in practice -- prove it says so.

    Starving the iteration budget is the only way to reach this branch. What
    matters is that it raises rather than returning parts whose prefixes were
    sized for a smaller total than they actually carry, which would silently
    break the length invariant for a caller using a different max_length.
    """
    monkeypatch.setattr(
        "nextcloud_mcp_server.utils.message_splitter._MAX_PREFIX_ITERATIONS", 1
    )
    message = " ".join(f"word{i:04d}" for i in range(2000))

    with pytest.raises(RuntimeError, match="did not converge"):
        split_message(message, max_length=LIMIT)


def test_non_positive_max_length_is_rejected():
    with pytest.raises(ValueError, match="max_length must be positive"):
        split_message("anything", max_length=0)


def test_max_length_too_small_for_the_prefix_is_rejected():
    """``(1/1) `` is six characters; a five-character budget cannot carry it."""
    with pytest.raises(ValueError, match="leaves no room"):
        split_message("x" * 50, max_length=5)


def test_default_prefix_template_shape():
    """The docstrings promise agents an ``(i/N) `` marker -- pin it."""
    assert PART_PREFIX_TEMPLATE.format(index=2, total=7) == "(2/7) "
