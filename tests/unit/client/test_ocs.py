"""Unit tests for the shared OCS envelope handling.

Two things are worth pinning here: that the parser survives every malformed
shape rather than raising a ``KeyError`` the caller cannot act on, and that a
997 is described by its two real causes instead of being reported as a generic
server failure.
"""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.client.ocs import (
    OCS_STATUS_UNAUTHENTICATED,
    describe_ocs_failure,
    parse_ocs_envelope,
)

pytestmark = pytest.mark.unit

#: Any JSON value. Floats include inf/nan: ``json.loads`` accepts ``Infinity``.
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(), inner, max_size=3)
    ),
    max_leaves=10,
)


@given(
    json_values
    | st.fixed_dictionaries(
        {
            "ocs": st.fixed_dictionaries(
                {}, optional={"meta": json_values, "data": json_values}
            )
            | json_values
        }
    )
)
@example(None)
@example("not json")
@example([])
@example({})
@example({"ocs": None})
@example({"ocs": "nope"})
def test_any_body_parses_without_raising(payload):
    """Every caller is already handling a failed request when it gets here, so
    a ``KeyError``/``TypeError`` from parsing would replace a diagnosable server
    error with an opaque one. Whatever arrives, the parser describes it."""
    envelope = parse_ocs_envelope(payload)

    assert isinstance(envelope.status_code, int)
    assert isinstance(envelope.message, str) and envelope.message
    ocs = payload.get("ocs") if isinstance(payload, dict) else None
    if not isinstance(ocs, dict):
        assert not envelope.is_success
    else:
        # has_data separates "no data field" (malformed to collectives) from
        # "data was null"; the distinction is lost if callers test for None.
        assert envelope.has_data == ("data" in ocs)
        assert envelope.data is ocs.get("data")


@st.composite
def statuscodes(draw):
    """A raw ``meta.statuscode`` and the status it must parse to.

    Present-but-falsy (``0``, ``""``, ``None``) defaults to 200, as mail's parser
    did: otherwise it crosses mail's ``>= 400`` gate and turns a success into a
    raised error (#1343). A non-numeric value is not something to report as
    success, so it is 500.
    """
    kind = draw(st.sampled_from(["int", "numeric", "falsy", "junk"]))
    if kind == "int":
        code = draw(st.integers(1, 1000) | st.sampled_from([100, 200, 997]))
        return code, code
    if kind == "numeric":
        code = draw(st.integers(1, 1000))
        return f" {code} ", code
    if kind == "falsy":
        return draw(st.sampled_from([0, "", None, False])), 200
    junk = draw(
        st.text().filter(lambda s: s.strip() and not _parses_as_int(s))
        | st.sampled_from([float("inf"), float("-inf"), float("nan")])
        | st.lists(st.integers(), min_size=1)
    )
    return junk, 500


def _parses_as_int(text: str) -> bool:
    try:
        int(text)
    except ValueError:
        return False
    return True


@given(statuscodes(), st.none() | st.text())
@example((100, 100), "OK")  # OCS v1 success: which one arrives depends on the route
@example((200, 200), "OK")  # OCS v2 success
@example((float("inf"), 500), None)  # int(inf) raised OverflowError
def test_statuscode_decides_success_and_message_falls_back(case, message):
    raw, expected = case
    meta = {"statuscode": raw} | ({} if message is None else {"message": message})

    envelope = parse_ocs_envelope({"ocs": {"meta": meta}})

    assert envelope.status_code == expected
    assert envelope.is_success == (expected in {100, 200})
    # One shared fallback for all three clients, used only when the server sent
    # no message -- a decision on record, not drift.
    assert envelope.message == (message or "Unknown error")


def test_997_names_both_of_its_causes():
    """The whole reason 997 gets special wording.

    Reported generically it sends the reader looking for a server fault that is
    not there -- the cause is either rejected credentials or a missing
    ``OCS-APIRequest`` header, and the message has to say both.
    """
    described = describe_ocs_failure(
        OCS_STATUS_UNAUTHENTICATED, "Current user is not logged in"
    )

    assert "unauthenticated" in described
    assert "OCS-APIRequest" in described
    assert str(OCS_STATUS_UNAUTHENTICATED) in described


def test_other_failures_keep_the_server_message():
    """Only 997 is reworded; everything else reports what the server said."""
    described = describe_ocs_failure(404, "Wrong path, file/folder doesn't exist")

    assert "Wrong path, file/folder doesn't exist" in described
    assert "OCS-APIRequest" not in described


def test_collectives_error_string_is_not_double_prefixed():
    """``str(OCSError)`` must not repeat the status code.

    ``describe_ocs_failure`` already names the code, and ``OCSError`` used to
    prefix it again -- yielding "OCS error 404: OCS API error (code 404): ..."
    in logs and tracebacks. Nothing caught it because the existing collectives
    tests assert on ``.message`` and substring matches, never on ``str(e)``.
    Exercised through the real ``_unwrap_ocs`` failure path rather than by
    constructing the exception directly, since that is where the two layers
    meet. Caught in review of #1343.
    """
    from nextcloud_mcp_server.client.collectives import CollectivesClient, OCSError

    client = CollectivesClient.__new__(CollectivesClient)
    payload = {"ocs": {"meta": {"statuscode": 404, "message": "Not found"}}}

    with pytest.raises(OCSError) as exc_info:
        client._unwrap_ocs(payload)

    rendered = str(exc_info.value)
    assert rendered.count("404") == 1, rendered
    assert not rendered.startswith("OCS error 404:")
    assert exc_info.value.status_code == 404
