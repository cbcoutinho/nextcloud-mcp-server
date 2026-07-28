"""Unit tests for the comment message splitter.

The four property tests are the load-bearing ones: they encode the invariants
that decide whether a split comment can actually be posted and whether the text
survives the round trip. The example tests pin the individual edge cases those
properties are easy to satisfy vacuously on.
"""

import re

import pytest

from nextcloud_mcp_server.utils.message_splitter import (
    MENTION_PATTERN,
    PART_PREFIX_TEMPLATE,
    measured_length,
    split_message,
)

pytestmark = pytest.mark.unit

LIMIT = 1000

_PREFIX_RE = re.compile(r"^\(\d+/\d+\) ")


def strip_prefix(part: str) -> str:
    """Drop the ``(i/N) `` marker a split part carries."""
    return _PREFIX_RE.sub("", part)


# Corpus --------------------------------------------------------------------

MARKDOWN_DOC = (
    "## Shipped state\n\n"
    "The migration landed in 0.142.0. Every tenant now resolves its collection "
    "through the registry rather than the hardcoded map. Rollout was staged "
    "over two days and no tenant saw downtime.\n\n"
    "### Follow-ups\n\n"
    "- purge the legacy map\n"
    "- backfill the audit log\n\n"
    'Thanks @alice and @"bob smith" for the review.\n\n'
) * 8

PROSE = (
    "The parser rejected the header. It expected four fields and found three! "
    "Was that a regression? No; the fixture was stale. "
) * 30

CJK = "指定された文書を解析し、埋め込みを生成してから索引に登録します。" * 60

EMOJI = "shipped 🚀 verified ✅ rolled out 🌍 " * 60

NO_SPACES = "x" * 4000

MENTION_HEAVY = (
    'Reviewed by @alice, @"bob smith", @carol.dev and @dan@example.com. '
) * 40

CORPUS = {
    "markdown": MARKDOWN_DOC,
    "prose": PROSE,
    "cjk": CJK,
    "emoji": EMOJI,
    "no_spaces": NO_SPACES,
    "mentions": MENTION_HEAVY,
}


# Properties ----------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_every_part_fits_the_limit(name):
    """The invariant that decides whether the POST succeeds.

    Measured with the prefix applied, because that is what gets posted.
    """
    parts = split_message(CORPUS[name], max_length=LIMIT)

    assert parts, "a non-empty message must produce at least one part"
    oversized = [
        (i, measured_length(p))
        for i, p in enumerate(parts, 1)
        if measured_length(p) > LIMIT
    ]
    assert not oversized, f"parts over the {LIMIT}-character limit: {oversized}"


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_round_trip_preserves_every_non_whitespace_character(name):
    """Parts are exact slices, so only boundary whitespace may be dropped."""
    message = CORPUS[name]
    parts = split_message(message, max_length=LIMIT)

    rejoined = "".join(strip_prefix(p) for p in parts)

    assert re.sub(r"\s+", "", rejoined) == re.sub(r"\s+", "", message)


def part_boundaries(message: str, parts: list[str]) -> list[int]:
    """Offsets in ``message.strip()`` where one part ends and the next begins.

    Located by walking each part body forward through the source rather than by
    rejoining: concatenating parts closes up the whitespace dropped at a cut,
    which would weld the tail of one part onto the head of the next and make a
    clean boundary look like a severed token.
    """
    text = message.strip()
    boundaries = []
    cursor = 0
    for part in parts:
        body = strip_prefix(part)
        start = text.index(body, cursor)
        cursor = start + len(body)
        boundaries.append(cursor)
    return boundaries[:-1]


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_no_mention_is_split_across_parts(name):
    """A severed mention silently degrades to plain text, so cuts avoid them."""
    message = CORPUS[name]
    parts = split_message(message, max_length=LIMIT)

    mentions = [m.span() for m in MENTION_PATTERN.finditer(message.strip())]
    straddled = [
        (start, end, boundary)
        for start, end in mentions
        for boundary in part_boundaries(message, parts)
        if start < boundary < end
    ]

    assert not straddled, f"mentions cut by a part boundary: {straddled}"


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_part_numbering_is_consistent(name):
    """Guards the prefix fixed point: every part agrees on the total."""
    parts = split_message(CORPUS[name], max_length=LIMIT)

    if len(parts) == 1:
        assert not _PREFIX_RE.match(parts[0]), "a lone part carries no prefix"
        return

    numbering = [_PREFIX_RE.match(p) for p in parts]
    assert all(numbering), "every part of a split message carries a prefix"

    indices = [int(re.match(r"\((\d+)/(\d+)\)", p).group(1)) for p in parts]
    totals = {int(re.match(r"\((\d+)/(\d+)\)", p).group(2)) for p in parts}

    assert indices == list(range(1, len(parts) + 1))
    assert totals == {len(parts)}


@pytest.mark.parametrize("word_count", range(1000, 1400, 23))
def test_invariants_hold_across_the_prefix_width_boundary(word_count):
    """Around 9->10 parts the prefix widens from 6 to 8 characters.

    That is exactly where a naive budget calculation overflows the limit, so
    sweep the sizes that cross it.
    """
    message = " ".join(f"word{i:04d}" for i in range(word_count))

    parts = split_message(message, max_length=LIMIT)

    assert all(measured_length(p) <= LIMIT for p in parts)
    totals = {int(re.match(r"\((\d+)/(\d+)\)", p).group(2)) for p in parts}
    assert totals == {len(parts)}


# measured_length -----------------------------------------------------------


def test_measured_length_ignores_surrounding_whitespace():
    """Core trims before it measures, so we must too."""
    assert measured_length("  hello  \n\n") == len("hello")


def test_measured_length_counts_code_points_not_bytes():
    """mb_strlen(..., 'UTF-8') counts code points; so does Python's len()."""
    assert measured_length("🚀") == 1
    assert measured_length("é") == 1


# Edge cases ----------------------------------------------------------------


@pytest.mark.parametrize("message", ["", "   ", "\n\n  \t "])
def test_blank_message_yields_no_parts(message):
    assert split_message(message, max_length=LIMIT) == []


def test_message_at_exactly_the_limit_is_returned_untouched():
    """The server's check is ``> max``, so 1000 characters is legal."""
    message = "x" * LIMIT

    assert split_message(message, max_length=LIMIT) == [message]


def test_message_at_the_limit_plus_trailing_whitespace_is_not_split():
    """Trailing newlines are trimmed away server-side before the check.

    Measuring with a bare ``len()`` here would split a message the server would
    have accepted whole.
    """
    message = "x" * LIMIT + "\n\n"

    assert split_message(message, max_length=LIMIT) == [message]


def test_one_character_over_the_limit_splits_in_two():
    parts = split_message("y" * (LIMIT + 1), max_length=LIMIT)

    assert len(parts) == 2
    assert all(measured_length(p) <= LIMIT for p in parts)


def test_unbreakable_run_is_hard_cut_without_losing_characters():
    """No separator anywhere -- the hard-cut fallback must still be lossless."""
    message = "z" * 1500

    parts = split_message(message, max_length=LIMIT)

    assert len(parts) == 2
    assert "".join(strip_prefix(p) for p in parts) == message


def test_cut_backs_off_to_the_start_of_a_straddling_mention():
    """A mention crossing the budget moves wholly into the next part."""
    message = "a" * 94 + ' @"alice smith" tail'

    parts = split_message(message, max_length=100)

    assert len(parts) == 2
    assert '@"alice smith"' in strip_prefix(parts[1])
    assert "@" not in strip_prefix(parts[0])


def test_mention_longer_than_the_budget_is_cut_rather_than_overflowing():
    """The length invariant outranks the mention invariant.

    An over-length part is rejected by the server outright; a severed mention
    merely renders as plain text.
    """
    message = '@"' + "n" * 300 + '" trailing words here'

    parts = split_message(message, max_length=100)

    assert all(measured_length(p) <= 100 for p in parts)
    assert "".join(strip_prefix(p) for p in parts).replace(" ", "") == message.replace(
        " ", ""
    )


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


def test_prefix_template_can_be_disabled():
    message = "w" * 1500

    parts = split_message(message, max_length=LIMIT, part_prefix_template=None)

    assert all(not _PREFIX_RE.match(p) for p in parts)
    assert "".join(parts) == message


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
