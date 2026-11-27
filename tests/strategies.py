"""Hypothesis strategies shared by the response-parsing properties."""

from hypothesis import strategies as st


def _json(floats: st.SearchStrategy[float]) -> st.SearchStrategy:
    return st.recursive(
        st.none() | st.booleans() | st.integers() | floats | st.text(),
        lambda inner: (
            st.lists(inner, max_size=3) | st.dictionaries(st.text(), inner, max_size=3)
        ),
        max_leaves=8,
    )


#: Any JSON value, floats (inf/nan) included: ``json.loads`` accepts ``Infinity``.
json_values = _json(st.floats())

#: JSON values ``httpx2.Response(json=...)`` can encode: it rejects inf and nan.
wire_json_values = _json(st.floats(allow_nan=False, allow_infinity=False))

#: Text a header value can carry: printable ASCII without line breaks.
header_text = st.text(
    st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=20
)
