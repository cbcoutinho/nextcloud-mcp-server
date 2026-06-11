"""Unit tests for the embed-drop classifier (card 309).

``processor._drop_reason`` maps a terminal indexing failure to a metric label
so the transient backend-pod-rollover causes (connection / timeout) are
alertable on ``astrolabe_vector_ingest_dropped_total`` distinctly from
persistent faults.
"""

import httpx
import pytest

from nextcloud_mcp_server.vector import processor


def _req() -> httpx.Request:
    return httpx.Request("POST", "http://gw/v1/embeddings")


@pytest.mark.unit
def test_httpx_connect_and_timeout_classified():
    assert processor._drop_reason(httpx.ConnectError("refused")) == "connection"
    assert processor._drop_reason(httpx.ReadTimeout("slow")) == "timeout"
    assert processor._drop_reason(httpx.ConnectTimeout("slow")) == "timeout"


@pytest.mark.unit
def test_openai_errors_classified():
    from openai import (
        APIConnectionError,
        APITimeoutError,
        InternalServerError,
        RateLimitError,
    )

    req = _req()
    assert processor._drop_reason(APIConnectionError(request=req)) == "connection"
    assert processor._drop_reason(APITimeoutError(request=req)) == "timeout"
    assert (
        processor._drop_reason(
            RateLimitError("rl", response=httpx.Response(429, request=req), body=None)
        )
        == "rate_limit"
    )
    assert (
        processor._drop_reason(
            InternalServerError(
                "boom", response=httpx.Response(503, request=req), body=None
            )
        )
        == "server"
    )


@pytest.mark.unit
def test_exception_group_unwraps_to_leaf():
    group = BaseExceptionGroup(
        "unhandled errors in a TaskGroup", [httpx.ConnectError("refused")]
    )
    assert processor._drop_reason(group) == "connection"


@pytest.mark.unit
def test_nested_exception_group_descends_to_leaf():
    """A doubly-wrapped group must still classify by its leaf, not 'other'."""
    nested = BaseExceptionGroup(
        "outer", [BaseExceptionGroup("inner", [httpx.ReadTimeout("slow")])]
    )
    assert processor._drop_reason(nested) == "timeout"


@pytest.mark.unit
def test_qdrant_namespace_classified():
    from qdrant_client.http.exceptions import UnexpectedResponse

    exc = UnexpectedResponse(500, "err", b"", headers=None)
    assert processor._drop_reason(exc) == "qdrant"


@pytest.mark.unit
def test_unknown_error_falls_back_to_other():
    assert processor._drop_reason(ValueError("nope")) == "other"
