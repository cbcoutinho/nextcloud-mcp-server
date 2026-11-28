"""Hypothesis strategies shared by the response-parsing properties."""

from hypothesis import strategies as st

_MAX_LEAVES = 8
_MAX_ITEMS = 3
_MAX_TOKENS = 8
_LEAF = ("leaf", 0, "")


def _json(floats: st.SearchStrategy[float]) -> st.SearchStrategy:
    """JSON trees of up to 8 scalar leaves and up to 3 items per container.

    Not ``st.recursive``: the shape is drawn first, as 8 preorder tokens
    (leaf, or a list or dict of up to 3 items; nodes past the 8th are
    leaves), then 8 leaves. Unused tokens and leaves are drawn all the same,
    so every draw makes the same choices whatever the shape. When Hypothesis
    mutates an example (it copies one part's choices over another's) the
    example then never needs more choices than it has. With ``st.recursive``
    that happened on ~10% of examples, each discarded as ``gave_up``. For the
    same reason ``None`` is drawn like the other scalars, from one choice,
    rather than from ``st.none()``, which makes none.
    """
    scalars = (
        st.booleans().map(lambda _: None)
        | st.booleans()
        | st.integers()
        | floats
        | st.text()
    )
    tokens = st.tuples(
        st.sampled_from(["leaf", "list", "dict"]),
        st.integers(0, _MAX_ITEMS),
        st.text(),  # its key, if its parent is a dict
    )

    @st.composite
    def json_trees(draw):
        shape = iter([draw(tokens) for _ in range(_MAX_TOKENS)])
        leaves = iter([draw(scalars) for _ in range(_MAX_LEAVES)])

        def build() -> tuple[str, object]:
            """The next node in preorder, as ``(its key, its value)``."""
            kind, size, key = next(shape, _LEAF)  # tokens used up: a leaf
            if kind == "leaf":
                return key, next(leaves)  # StopIteration ends the parent
            items = []
            for _ in range(size):
                try:
                    items.append(build())
                except StopIteration:
                    break
            return key, dict(items) if kind == "dict" else [v for _, v in items]

        return build()[1]  # the first leaf is always left for the root

    return json_trees()


#: Any JSON value, floats (inf/nan) included: ``json.loads`` accepts ``Infinity``.
json_values = _json(st.floats())

#: JSON values without NaN, so a parsed payload can be compared with ``==``.
comparable_json_values = _json(st.floats(allow_nan=False))

#: JSON values ``httpx2.Response(json=...)`` can encode: it rejects inf and nan.
wire_json_values = _json(st.floats(allow_nan=False, allow_infinity=False))


@st.composite
def fixed_draw_lists(
    draw, elements: st.SearchStrategy, *, min_size: int = 0, max_size: int
) -> list:
    """``st.lists(elements, min_size=..., max_size=...)``, drawn as a length
    and always ``max_size`` elements, of which the first ``length`` are kept.

    ``st.lists`` decides element by element whether to go on, so a mutation
    that flips one of those decisions needs more choices than the example has,
    and the example is discarded as ``gave_up``. Here the number of draws is
    fixed.
    """
    length = draw(st.integers(min_size, max_size))
    return [draw(elements) for _ in range(max_size)][:length]


@st.composite
def fixed_draw_one_of(draw, *alternatives: st.SearchStrategy):
    """``st.one_of(*alternatives)``, drawn as an index and every alternative.

    For a ``one_of`` used several times in one example whose alternatives
    make different numbers of choices: a mutation that copies one branch
    index over another then never needs more choices than the example has.

    Every alternative is drawn on every call, so each draw costs the sum of
    all alternatives. Keep the alternatives cheap; use plain ``st.one_of``
    for a heavy one.
    """
    index = draw(st.integers(0, len(alternatives) - 1))
    return [draw(alternative) for alternative in alternatives][index]


@st.composite
def dictionaries_with_optional_keys(
    draw,
    required: dict[str, st.SearchStrategy],
    optional: dict[str, st.SearchStrategy],
) -> dict:
    """``st.fixed_dictionaries(required, optional=optional)``, but every
    optional value is drawn whether its key is kept or not.

    The number of draws then never depends on which keys are present, so
    Hypothesis can flip a key in or out (one of its mutations) without the
    example outgrowing its size cap and being discarded as ``gave_up``.
    """
    out = {key: draw(value) for key, value in required.items()}
    for key, strategy in optional.items():
        value = draw(strategy)
        if draw(st.booleans()):
            out[key] = value
    return out


#: Text a header value can carry: printable ASCII without line breaks.
header_text = st.text(
    st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=20
)
