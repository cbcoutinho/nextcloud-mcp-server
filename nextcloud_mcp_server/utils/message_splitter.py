"""Split over-long messages into postable parts on natural boundaries.

Nextcloud caps a comment at ``IComment::MAX_MESSAGE_LENGTH`` (1000) characters.
Rather than truncating -- which silently corrupts an activity log while
reporting success -- this module cuts a message into parts that each fit, on the
most structural boundary available.

This is deliberately *not* :mod:`nextcloud_mcp_server.vector.document_chunker`.
That one is async, overlap-based (overlap would duplicate text into the comment
thread) and tuned for MB-scale RAG documents. Here we want a synchronous,
exact-slice split of a few kilobytes.

The boundary rules come from LangChain's ``RecursiveCharacterTextSplitter``,
which is already a dependency. It is used as a *cut-offset finder*: we take the
offsets it would split at and slice the original string ourselves, so every part
is an exact substring of the input. That keeps the round-trip exact -- LangChain
drops separators from the chunks it returns, which for an activity log would
quietly mangle the text.
"""

import bisect
import logging
import re
from typing import Final

from langchain_text_splitters import RecursiveCharacterTextSplitter

logger = logging.getLogger(__name__)

# Break preference, most structural first. Regexes, because the heading and
# sentence rules need them (hence is_separator_regex=True below). The trailing
# "" is LangChain's hard-cut fallback and is what bounds every chunk by
# chunk_size, so it must stay last.
_SEPARATORS: Final[tuple[str, ...]] = (
    r"\n#{1,6} ",  # markdown headings
    r"```\n",  # fenced code blocks
    r"\n\n",  # paragraphs
    r"\n",  # lines
    r"(?<=[.!?])\s+",  # sentence ends; the lookbehind keeps the punctuation left
    r" ",  # words -- the floor for "never cut mid-word"
    r"",  # hard cut, last resort
)

# Nextcloud mention syntax: @userid, or @"user id with spaces". Also covers
# federated @user@example.com. Cutting one in half turns a working mention into
# plain text, so cuts are steered around these spans.
MENTION_PATTERN: Final[re.Pattern[str]] = re.compile(r'@(?:"[^"\n]*"|[\w.\-@]+)')

PART_PREFIX_TEMPLATE: Final[str] = "({index}/{total}) "

# Nextcloud core caps a comment at IComment::MAX_MESSAGE_LENGTH, checked in
# OC\Comments\Comment::setMessage AFTER trim(), in UTF-8 code points, with a
# strict ">" so exactly 1000 is legal. It is a *core* rule, not a Deck one, so
# every comment surface (Deck cards, file comments) measures against this one
# definition -- next to measured_length, which is what makes the measurement
# agree with the server's.
COMMENT_MAX_LENGTH: Final[int] = 1000

# The prefix width depends on the part count, which depends on the budget, which
# depends on the prefix width. The fixed point converges as soon as the digit
# count stops moving -- three rounds is already generous.
_MAX_PREFIX_ITERATIONS: Final[int] = 8


# PHP trim()'s default charlist. Deliberately NOT Python's bare str.strip(),
# which also strips every Unicode Zs-category space -- U+00A0 (NBSP), U+202F,
# U+3000 (ideographic space, ordinary in CJK text) -- while PHP leaves those in
# place. The two disagree in both directions and each one costs us:
#   * strip more than PHP  -> we measure 1000, the server measures 1003, we
#     post it and get a 400 back.
#   * strip less than PHP  -> we reject a message the server would accept
#     (PHP trims NUL, Python does not).
# Matching the charlist exactly is the only way the client-side check agrees
# with the server it is standing in for.
_PHP_TRIM_CHARS: Final[str] = " \t\n\r\0\x0b"


def measured_length(message: str) -> int:
    """Return the length as the Nextcloud server measures it.

    Core's ``OC\\Comments\\Comment::setMessage`` trims first and then applies
    ``mb_strlen($message, 'UTF-8') > $maxLength`` -- Unicode **code points**,
    not bytes and not grapheme clusters. Python's ``len()`` on a ``str`` is
    also code points, so trimming is the only correction needed -- but it has
    to be PHP's notion of trimming, not Python's. See ``_PHP_TRIM_CHARS``.
    """
    return len(message.strip(_PHP_TRIM_CHARS))


def split_message(
    message: str,
    *,
    max_length: int,
    part_prefix_template: str | None = PART_PREFIX_TEMPLATE,
) -> list[str]:
    """Split ``message`` into parts that each fit within ``max_length``.

    Every returned part satisfies ``measured_length(part) <= max_length`` with
    its prefix already applied, and is an exact slice of the input apart from
    that prefix and stripped boundary whitespace.

    A message that already fits comes back unchanged in a one-element list, with
    no prefix -- so callers can route every message through here without
    perturbing the common case.

    Args:
        message: The text to split.
        max_length: Hard per-part limit, measured as :func:`measured_length`.
        part_prefix_template: Format string receiving ``index`` and ``total``,
            prepended to each part when there is more than one. Pass ``None``
            to number nothing and give the full ``max_length`` to content.

    Returns:
        The parts in order. Empty when ``message`` has no non-whitespace
        content.

    Raises:
        ValueError: If ``max_length`` is not positive, or the prefix would
            leave no room for content.

    Known limitation: a fenced code block straddling a cut renders as two broken
    fences. The fence separator sits second in the preference order so this is
    rare, and treating fences as atomic would cost far more complexity than the
    cosmetic gain is worth.
    """
    if max_length <= 0:
        raise ValueError(f"max_length must be positive, got {max_length}")

    if measured_length(message) == 0:
        return []
    if measured_length(message) <= max_length:
        return [message]

    # Trim exactly what the server trims -- never more, or we would drop
    # trailing content (an ideographic space, say) that Deck would have kept.
    text = message.strip(_PHP_TRIM_CHARS)

    parts: list[str] = []
    total_guess = 1
    for _ in range(_MAX_PREFIX_ITERATIONS):
        budget = max_length - _prefix_width(part_prefix_template, total_guess)
        if budget <= 0:
            raise ValueError(
                f"max_length={max_length} leaves no room for the "
                f"{max_length - budget}-character part prefix"
            )
        parts = _pack(text, budget)
        if len(parts) <= total_guess:
            break
        total_guess = len(parts)
    else:
        # Unreachable in practice: total_guess strictly increases on every
        # non-converging pass, and the prefix only widens at powers of ten, so
        # the fixed point settles within a couple of rounds for any realistic
        # part count. Fail loudly rather than fall out of the loop and return
        # parts whose prefixes were sized for a smaller total than they will
        # actually carry -- that would quietly break the one invariant this
        # function promises, which matters because this module is generic and
        # its callers pick their own max_length.
        raise RuntimeError(
            f"part-count fixed point did not converge after "
            f"{_MAX_PREFIX_ITERATIONS} iterations for a "
            f"{len(text)}-character message at max_length={max_length} "
            f"(last estimate {total_guess}, produced {len(parts)})"
        )

    if len(parts) == 1 or part_prefix_template is None:
        return parts

    total = len(parts)
    return [
        part_prefix_template.format(index=index, total=total) + part
        for index, part in enumerate(parts, 1)
    ]


def _prefix_width(part_prefix_template: str | None, total: int) -> int:
    """Upper bound on the prefix width for a split of ``total`` parts.

    Rendered with ``index=total`` because ``index <= total`` always, so the
    widest index is the total itself. Erring wide keeps the budget honest: a
    prefix narrower than reserved only leaves a part shorter than it could be,
    whereas a wider one would blow the limit.
    """
    if part_prefix_template is None:
        return 0
    return len(part_prefix_template.format(index=total, total=total))


def _candidate_offsets(text: str, budget: int) -> list[int]:
    """Offsets in ``text`` that the boundary rules consider legal cut points."""
    splitter = RecursiveCharacterTextSplitter(
        separators=list(_SEPARATORS),
        is_separator_regex=True,
        chunk_size=budget,
        chunk_overlap=0,
        keep_separator="start",
        add_start_index=True,
        strip_whitespace=True,
    )
    documents = splitter.create_documents([text])
    # start_index is the chunk's offset in the original text; 0 is the start of
    # the message, which is never a cut. -1 means LangChain could not locate the
    # chunk, which should not happen but must not become a bogus offset.
    return sorted(
        {
            offset
            for document in documents
            if (offset := document.metadata.get("start_index", -1)) > 0
        }
    )


def _pack(text: str, budget: int) -> list[str]:
    """Greedily fill parts up to ``budget``, cutting at the rightmost legal offset.

    LangChain's own chunks are not used directly because it merges only within a
    recursion level and so under-packs: on a 312-character sample at
    ``chunk_size=120`` it returns six chunks where four fit. Every extra part is
    an extra comment on the card, so packing greedily is worth the pass.
    """
    offsets = _candidate_offsets(text, budget)
    mentions = [match.span() for match in MENTION_PATTERN.finditer(text)]

    parts: list[str] = []
    position = 0
    length = len(text)
    while position < length:
        if length - position <= budget:
            parts.append(text[position:])
            break
        cut = _choose_cut(offsets, mentions, position, position + budget)
        parts.append(text[position:cut])
        position = cut

    # Same charlist at cut boundaries, for the same reason: the whitespace we
    # discard here is whitespace the server would have discarded anyway.
    return [stripped for part in parts if (stripped := part.strip(_PHP_TRIM_CHARS))]


def _choose_cut(
    offsets: list[int],
    mentions: list[tuple[int, int]],
    position: int,
    limit: int,
) -> int:
    """Pick where to cut in ``(position, limit]``, preferring the rightmost offset.

    Always returns a value greater than ``position`` so packing terminates.
    """
    index = bisect.bisect_right(offsets, limit)
    while index > 0:
        offset = offsets[index - 1]
        if offset <= position:
            break
        if _splits_mention(mentions, offset) is None:
            return offset
        index -= 1

    # No boundary in range -- the text here is one unbroken run. Cut at the
    # limit, backing off to the start of a mention rather than severing it.
    mention = _splits_mention(mentions, limit)
    if mention is None:
        return limit
    if mention[0] > position:
        return mention[0]

    # A single mention is longer than the whole budget. The length invariant
    # wins: an over-length part is rejected outright by the server, whereas a
    # severed mention merely renders as plain text.
    logger.debug(
        "Cutting inside a mention at offset %d: the token exceeds the "
        "%d-character budget",
        limit,
        limit - position,
    )
    return limit


def _splits_mention(
    mentions: list[tuple[int, int]], offset: int
) -> tuple[int, int] | None:
    """Return the mention span ``offset`` falls strictly inside, if any."""
    for start, end in mentions:
        if start < offset < end:
            return start, end
        if start >= offset:
            break
    return None
