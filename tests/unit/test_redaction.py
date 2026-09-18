"""Unit tests for person-name redaction (ADR-038). Synthetic names only."""

from types import SimpleNamespace

import pytest

from nextcloud_mcp_server.redaction import (
    Redactor,
    detect_names,
    ner_endpoint,
    redaction_mode,
)

pytestmark = pytest.mark.unit


def test_keeps_subject_and_redacts_third_party():
    r = Redactor({"Jane Doe", "Karen Smith"}, keep_names=["Jane Doe"])
    assert r.redact("Jane Doe met Karen Smith.") == "Jane Doe met [PERSON_1]."


def test_propagates_and_expands_tokens():
    # NER saw "Karen Smith" once; the bare surname and a case variant elsewhere
    # are redacted too.
    r = Redactor({"Karen Smith"})
    assert (
        r.redact("Karen Smith wrote. Later SMITH replied; smith agreed.")
        == "[PERSON_1] wrote. Later [PERSON_2] replied; [PERSON_2] agreed."
    )


def test_numbering_is_consistent_across_calls():
    r = Redactor({"Karen Smith", "Tom Brown"})
    assert r.redact("Tom Brown and Karen Smith") == "[PERSON_1] and [PERSON_2]"
    assert r.redact("letter from Karen Smith") == "letter from [PERSON_2]"
    assert r.persons_redacted == 2


def test_matches_across_line_breaks_and_filename_separators():
    r = Redactor({"Karen Smith"})
    assert r.redact("signed Karen\nSmith") == "signed [PERSON_1]"
    assert r.redact("/HR/KAREN_SMITH.pdf") == "/HR/[PERSON_1].pdf"
    assert r.redact("karen.smith@example.org") == "[PERSON_1]@example.org"


def test_hyphenated_name_is_one_person():
    r = Redactor({"Anna Smith-Jones"})
    assert r.redact("Anna Smith-Jones") == "[PERSON_1]"


def test_does_not_match_inside_words():
    r = Redactor({"Ann Lee"})
    assert r.redact("Annual leave for Ann") == "Annual leave for [PERSON_1]"


def test_kept_name_is_not_token_expanded():
    r = Redactor({"Jane Doe"}, keep_names=["Jane Doe"])
    assert r.redact("Dear Jane, re Jane Doe") == "Dear Jane, re Jane Doe"


def test_shared_token_with_third_party_is_redacted():
    # "Doe" is a token of a third party's name, so a bare "Doe" is ambiguous
    # and redacted unless the caller lists it as an alias of the subject.
    r = Redactor({"Jane Doe", "John Doe"}, keep_names=["Jane Doe"])
    assert (
        r.redact("Jane Doe and John Doe; Doe") == "Jane Doe and [PERSON_1]; [PERSON_2]"
    )
    r = Redactor({"Jane Doe", "John Doe"}, keep_names=["Jane Doe", "Doe"])
    assert r.redact("Doe") == "Doe"


def test_honorifics_and_short_tokens_are_not_expanded():
    r = Redactor({"Mrs Al Green"})
    assert r.redact("Mrs Al Green; Mrs Lee; Al") == "[PERSON_1]; Mrs Lee; Al"
    r = Redactor({"Rev Tom Brown"})
    assert r.redact("the Rev spoke; Brown left") == "the Rev spoke; [PERSON_1] left"


def test_keep_alias_with_initial():
    r = Redactor({"J. Doe"}, keep_names=["J. Doe"])
    assert r.redact("J. Doe and J Doe") == "J. Doe and J Doe"


def test_no_names_is_identity():
    assert Redactor(set()).redact("nothing here") == "nothing here"
    assert Redactor({"X Y"}).redact(None) is None


@pytest.mark.parametrize(
    ("configured", "gateway", "expected"),
    [
        ("enforced", "https://gw", "enforced"),
        ("optional", "https://gw/v1", "optional"),
        ("off", "https://gw", "off"),
        # Gateway-only: without one, redaction is unavailable whatever is set.
        ("enforced", None, "off"),
        ("optional", None, "off"),
    ],
)
def test_redaction_mode(configured, gateway, expected):
    settings = SimpleNamespace(
        content_redaction=configured, embedding_gateway_url=gateway
    )
    assert redaction_mode(settings) == expected


def test_ner_endpoint_normalises_v1_suffix():
    assert ner_endpoint(SimpleNamespace(embedding_gateway_url="https://gw/")) == (
        "https://gw/v1/ner"
    )
    assert ner_endpoint(SimpleNamespace(embedding_gateway_url="https://gw/v1")) == (
        "https://gw/v1/ner"
    )


async def test_detect_names_windows_long_text(mocker):
    client = mocker.AsyncMock()
    client.detect.return_value = [{"Karen Smith"}, {"Tom Brown"}, set()]
    names = await detect_names(client, ["x" * 2500, "", "short"])
    (sent,) = client.detect.call_args.args
    assert len(sent) == 3  # 2 windows + "short"; the empty text is skipped
    assert names == {"Karen Smith", "Tom Brown"}


def test_settings_validate_content_redaction():
    from nextcloud_mcp_server.config import Settings

    assert Settings(content_redaction=" Enforced ").content_redaction == "enforced"
    with pytest.raises(ValueError, match="CONTENT_REDACTION"):
        Settings(content_redaction="bogus")
    with pytest.raises(ValueError, match="NER_THRESHOLD"):
        Settings(ner_threshold=0)


async def test_get_ner_client_targets_gateway_and_is_cached():
    from nextcloud_mcp_server import redaction

    redaction._reset_ner_state()
    settings = SimpleNamespace(
        embedding_gateway_url="https://gw",
        embedding_gateway_client_id=None,
        ner_model="local/m",
        ner_timeout_seconds=5,
        ner_threshold=0.3,
    )
    try:
        client = await redaction.get_ner_client(settings)
        assert client._url == "https://gw/v1/ner"
        assert client.model == "local/m"
        assert client._threshold == 0.3
        assert await redaction.get_ner_client(settings) is client
    finally:
        redaction._reset_ner_state()
