"""Unit tests for the gateway NER client.

The client is strict where the rerank client is lenient: a submitted text the
response does not account for would be served as containing no names, i.e.
unredacted. So every malformed shape must raise rather than be skipped.
"""

import json

import httpx2
import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st

from nextcloud_mcp_server.providers.ner import (
    MAX_TEXT_CHARS,
    NerClient,
    NerError,
    windows,
)

pytestmark = pytest.mark.unit

_URL = "https://gw.example/v1/ner"


def _patch_transport(monkeypatch, handler):
    """Same MockTransport shim as test_rerank_client.py (no respx here)."""
    seen: list[httpx2.Request] = []

    def _handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    original = httpx2.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx2.MockTransport(_handler)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx2, "AsyncClient", _factory)
    return seen


def _ok(payload):
    return lambda request: httpx2.Response(200, json=payload)


def _person(start, end, text="ignored"):
    return {"start": start, "end": end, "text": text, "label": "person", "score": 0.9}


async def test_posts_texts_and_reads_names_from_offsets(monkeypatch):
    seen = _patch_transport(
        monkeypatch,
        _ok(
            {
                "results": [
                    # The echoed "text" is ignored; the name comes from the
                    # submitted text via the offsets.
                    {"index": 0, "entities": [_person(5, 16, "KAREN")]},
                    {"index": 1, "entities": []},
                ]
            }
        ),
    )
    found = await NerClient(_URL, "local/m").detect(["Dear Karen Smith,", "none"])

    body = json.loads(seen[0].content)
    assert body["model"] == "local/m"
    assert body["texts"] == ["Dear Karen Smith,", "none"]
    assert body["labels"] == ["person"]
    assert found == [{("person", "Karen Smith")}, set()]


async def test_requests_and_returns_every_asked_label(monkeypatch):
    text = "Karen Smith, 72 High Street"
    seen = _patch_transport(
        monkeypatch,
        _ok(
            {
                "results": [
                    {
                        "index": 0,
                        "entities": [
                            _person(0, 11),
                            {**_person(13, 27), "label": "address"},
                        ],
                    }
                ]
            }
        ),
    )

    found = await NerClient(_URL, "m").detect([text], ("person", "address"))

    assert json.loads(seen[0].content)["labels"] == ["person", "address"]
    assert found == [{("person", "Karen Smith"), ("address", "72 High Street")}]


async def test_ignores_unasked_labels_and_out_of_range_entities(monkeypatch):
    _patch_transport(
        monkeypatch,
        _ok(
            {
                "results": [
                    {
                        "index": 0,
                        "entities": [
                            {**_person(0, 3), "label": "organization"},
                            _person(0, 999),
                            _person(True, 3),
                        ],
                    }
                ]
            }
        ),
    )
    assert await NerClient(_URL, "m").detect(["ACME Ltd"]) == [set()]


async def test_incomplete_response_raises(monkeypatch):
    """Wiring: detect() applies the strict parse below to the HTTP body."""
    _patch_transport(monkeypatch, _ok({"results": [{"index": 0, "entities": []}]}))
    with pytest.raises(NerError):
        await NerClient(_URL, "m").detect(["a", "b"])


_LABELS = ("person",)
_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=10,
)
_BREAKERS = ["missing", "duplicate", "out_of_range", "bool_index", "no_entities"]


@st.composite
def ner_responses(draw):
    """``(texts, body, complete)``: a response accounting for every text exactly
    once, or one broken in a single way. Result order is shuffled: only
    ``index`` ties a result to its text."""
    texts = draw(st.lists(st.text(max_size=5), min_size=1, max_size=4))
    results = [
        {"index": i, "entities": draw(st.lists(_json, max_size=2))}
        for i in range(len(texts))
    ]
    breaker = draw(st.sampled_from([None, *_BREAKERS]))
    event(f"breaker: {breaker}")
    victim = draw(st.integers(0, len(results) - 1))
    if breaker == "missing":
        del results[victim]
    elif breaker == "duplicate":
        results.append(dict(results[victim]))
    elif breaker == "out_of_range":
        results[victim]["index"] = draw(
            st.integers(max_value=-1) | st.integers(min_value=len(texts))
        )
    elif breaker == "bool_index":  # bool is an int subclass: True is not 1
        results[victim]["index"] = bool(results[victim]["index"])
    elif breaker == "no_entities":
        results[victim]["entities"] = draw(
            st.none() | st.text() | st.dictionaries(st.text(), _json)
        )
    return texts, {"results": draw(st.permutations(results))}, breaker is None


@given(ner_responses())
def test_parse_accepts_exactly_a_response_covering_every_text(case):
    """A text the response does not account for would be served unredacted."""
    texts, body, complete = case
    if not complete:
        with pytest.raises(NerError):
            NerClient._parse(body, texts, _LABELS)
        return
    assert len(NerClient._parse(body, texts, _LABELS)) == len(texts)


@given(_json, st.lists(st.text(max_size=5), min_size=1, max_size=3))
@example({"nope": []}, ["a", "b"])
@example([], ["a", "b"])
@example({"results": [{"index": 0}]}, ["a", "b"])
@example(
    {
        "results": [
            {"index": 0, "entities": [{"label": "person", "start": 0, "end": 1}]}
        ]
    },
    ["a"],
)
def test_parse_raises_only_ner_error(body, texts):
    try:
        found = NerClient._parse(body, texts, _LABELS)
    except NerError:
        return
    assert len(found) == len(texts)


async def test_error_for_a_bad_result_does_not_carry_its_names(monkeypatch):
    """A result with a bad index can still hold real names: the error must
    describe its shape, not repeat them."""
    entity = {**_person(0, 11), "text": "Karen Smith"}
    _patch_transport(
        monkeypatch, _ok({"results": [{"index": 5, "entities": [entity]}]})
    )
    with pytest.raises(NerError) as info:
        await NerClient(_URL, "m").detect(["Karen Smith"])
    assert "Karen" not in str(info.value)
    assert "index 5 with 1 entities" in str(info.value)


async def test_http_error_raises(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx2.Response(503))
    with pytest.raises(NerError, match="HTTP 503"):
        await NerClient(_URL, "m").detect(["a"])


async def test_batches_large_inputs(monkeypatch):
    def handler(request):
        n = len(json.loads(request.content)["texts"])
        return httpx2.Response(
            200, json={"results": [{"index": i, "entities": []} for i in range(n)]}
        )

    seen = _patch_transport(monkeypatch, handler)
    found = await NerClient(_URL, "m", batch_size=32).detect(["t"] * 70)
    assert len(found) == 70
    assert len(seen) == 3
    seen.clear()
    await NerClient(_URL, "m").detect(["t"] * 17)  # default batch of 8
    assert len(seen) == 3


async def test_oversized_text_is_rejected_before_sending(monkeypatch):
    seen = _patch_transport(monkeypatch, _ok({"results": []}))
    with pytest.raises(NerError):
        await NerClient(_URL, "m").detect(["x" * (MAX_TEXT_CHARS + 1)])
    assert seen == []


async def test_bearer_header_from_token_provider(monkeypatch, mocker):
    seen = _patch_transport(
        monkeypatch, _ok({"results": [{"index": 0, "entities": []}]})
    )
    token_provider = mocker.MagicMock()
    token_provider.get_token = mocker.AsyncMock(return_value="tok-123")
    await NerClient(_URL, "m", token_provider).detect(["a"])
    assert seen[0].headers["authorization"] == "Bearer tok-123"


def test_windows_overlap_and_cover_text():
    text = "".join(chr(65 + i % 26) for i in range(5000))
    parts = windows(text)
    assert all(len(p) <= MAX_TEXT_CHARS for p in parts)
    assert parts[0] + parts[1][200:] + parts[2][200:] == text
    assert windows("short") == ["short"]


def test_windows_never_cut_a_word():
    """A word cut in two ("Aca|demic") reads as a name to the model, and a
    detected name is redacted everywhere in the archive."""
    words = [f"Academic{i}" for i in range(1000)]
    text = " ".join(words)
    parts = windows(text)
    assert len(parts) > 1
    assert all(len(p) <= MAX_TEXT_CHARS for p in parts)
    for p in parts:
        assert all(w in words for w in p.split())
    # Consecutive windows overlap, so a name spanning a cut is seen whole.
    for a, b in zip(parts, parts[1:]):
        assert a.split()[-1] in b.split()
    assert " ".join(dict.fromkeys(w for p in parts for w in p.split())) == text


def test_windows_start_before_a_word_longer_than_the_overlap():
    # A 250-character token ends just before the cut, so an overlap reaching
    # back 200 characters lands inside it: the next window must start at the
    # token rather than in the middle of it.
    long = "X" * 250
    text = ("word " * 1000)[:1745] + long + " more words" * 50
    parts = windows(text)
    assert all(len(p) <= MAX_TEXT_CHARS for p in parts)
    for p in parts:
        for w in p.split():
            assert w in {"word", "more", "words", long}, w[:20]
    assert any(p.startswith(long) for p in parts[1:])
