"""Hypothesis strategies shared by the response-parsing properties."""

from hypothesis import strategies as st

#: Any JSON value, floats (inf/nan) included: ``json.loads`` accepts ``Infinity``.
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(), inner, max_size=3)
    ),
    max_leaves=8,
)

#: Text a header value can carry: printable ASCII without line breaks.
header_text = st.text(
    st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=20
)

#: JSON values ``httpx2.Response(json=...)`` can encode: it rejects inf and nan.
wire_json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(), inner, max_size=3)
    ),
    max_leaves=8,
)
