"""Unit tests for SAR export redaction (ADR-040). Synthetic names only."""

from types import SimpleNamespace

import pytest

from nextcloud_mcp_server import sar_plugin
from nextcloud_mcp_server.redaction import (
    Redactor,
    counts,
    detect_entities,
)
from nextcloud_mcp_server.sar_plugin import SarSettings, ner_endpoint, sar_available

pytestmark = pytest.mark.unit


def test_keeps_subject_and_redacts_third_party():
    r = Redactor({"Jane Doe", "Karen Smith"}, keep=["Jane Doe"])
    assert r.redact("Jane Doe met Karen Smith.") == "Jane Doe met [PERSON_1]."


def test_roles_detected_as_people_are_not_names():
    # The model tags "student" in a query and "Father" in a contact list as
    # people; as name forms they would be redacted wherever the word occurs.
    r = Redactor({"student", "Father", "Mrs Mother", "Tom Father"})
    assert (
        r.redact("Student Record. Father: Tom Father. The student's mother.")
        == "Student Record. Father: [PERSON_1]. The student's mother."
    )


def test_job_title_detected_as_person_is_not_split_into_words():
    # "Academic Mentor" tagged as a person must not make "academic" a name.
    r = Redactor({"Academic Mentor", "Father Brown"})
    assert (
        r.redact("Academic Mentor: Brown. 2024 Academic Year. Father Brown said.")
        == "[PERSON_1]: Brown. 2024 Academic Year. [PERSON_2] said."
    )


def test_propagates_and_expands_tokens():
    # NER saw "Karen Smith" once; the bare surname and a case variant elsewhere
    # are redacted too, as the same person: the token belongs to no one else.
    r = Redactor({"Karen Smith"})
    assert (
        r.redact("Karen Smith wrote. Later SMITH replied; Karen agreed.")
        == "[PERSON_1] wrote. Later [PERSON_1] replied; [PERSON_1] agreed."
    )


def test_token_shared_by_two_third_parties_keeps_its_own_number():
    # "Smith" could be either person, so it is redacted without being
    # attributed to one of them.
    r = Redactor({"Karen Smith", "Tom Smith"})
    assert r.redact("Karen Smith, Tom Smith; Smith") == (
        "[PERSON_1], [PERSON_2]; [PERSON_3]"
    )


def test_numbering_is_consistent_across_calls():
    r = Redactor({"Karen Smith", "Tom Brown"})
    assert r.redact("Tom Brown and Karen Smith") == "[PERSON_1] and [PERSON_2]"
    assert r.redact("letter from Karen Smith") == "letter from [PERSON_2]"


def test_matches_across_line_breaks_and_filename_separators():
    r = Redactor({"Karen Smith"})
    assert r.redact("signed Karen\nSmith") == "signed [PERSON_1]"
    assert r.redact("/HR/KAREN_SMITH.pdf") == "/HR/[PERSON_1].pdf"


def test_hyphenated_name_is_one_person():
    r = Redactor({"Anna Smith-Jones"})
    assert r.redact("Anna Smith-Jones") == "[PERSON_1]"


def test_does_not_match_inside_words():
    r = Redactor({"Ann Lee"})
    assert r.redact("Annual leave for Ann") == "Annual leave for [PERSON_1]"


def test_kept_name_is_not_token_expanded():
    r = Redactor({"Jane Doe"}, keep=["Jane Doe"])
    assert r.redact("Dear Jane, re Jane Doe") == "Dear Jane, re Jane Doe"


def test_shared_token_with_third_party_is_redacted():
    # "Doe" is a token of a third party's name, so a bare "Doe" is ambiguous
    # and redacted unless the caller lists it as an alias of the subject.
    r = Redactor({"Jane Doe", "John Doe"}, keep=["Jane Doe"])
    assert (
        r.redact("Jane Doe and John Doe; Doe") == "Jane Doe and [PERSON_1]; [PERSON_2]"
    )
    r = Redactor({"Jane Doe", "John Doe"}, keep=["Jane Doe", "Doe"])
    assert r.redact("Doe") == "Doe"


def test_honorifics_and_short_tokens_are_not_expanded():
    r = Redactor({"Mrs Al Green"})
    assert r.redact("Mrs Al Green; Mrs Lee; Al") == "[PERSON_1]; Mrs Lee; Al"
    r = Redactor({"Rev Tom Brown"})
    assert r.redact("the Rev spoke; Brown left") == "the Rev spoke; [PERSON_1] left"


def test_name_detected_with_a_title_recurring_without_it_is_one_placeholder():
    r = Redactor({"Rev Tom Brown"})
    redacted = r.redact("Rev Tom Brown will officiate. Tom Brown has served 20 years.")
    assert redacted.count("[PERSON_") == 2  # not three: "Tom Brown" is one span
    assert "Tom" not in redacted and "Brown" not in redacted


def test_title_free_phrase_shares_the_full_names_number():
    r = Redactor({"Rev Tom Brown"})
    assert r.redact("Rev Tom Brown spoke. Tom Brown left. Brown sat.") == (
        "[PERSON_1] spoke. [PERSON_1] left. [PERSON_1] sat."
    )


def test_keep_alias_with_initial():
    r = Redactor({"J. Doe"}, keep=["J. Doe"])
    assert r.redact("J. Doe and J Doe") == "J. Doe and J Doe"


def test_no_names_is_identity():
    assert Redactor(set()).redact("nothing here") == "nothing here"
    assert Redactor({"X Y"}).redact(None) is None


def _sar(monkeypatch, **values) -> None:
    monkeypatch.setattr(sar_plugin, "sar_settings", lambda: SarSettings(**values))


@pytest.mark.parametrize("enabled", [True, False])
def test_sar_available_is_opt_in(monkeypatch, enabled):
    """Everything SAR needs is there; it is served only if the deployment asked."""
    _sar(monkeypatch, sar_enabled=enabled)
    settings = SimpleNamespace(vector_sync_enabled=True, embedding_gateway_url="g")
    assert sar_available(settings) is enabled


@pytest.mark.parametrize("vector_sync,gateway", [(False, "https://gw"), (True, None)])
def test_sar_enabled_without_what_it_needs_fails(monkeypatch, vector_sync, gateway):
    """Opting in without the index or the gateway fails startup rather than
    silently advertising nothing."""
    _sar(monkeypatch, sar_enabled=True)
    settings = SimpleNamespace(
        vector_sync_enabled=vector_sync, embedding_gateway_url=gateway
    )
    with pytest.raises(ValueError, match="SAR_ENABLED requires"):
        sar_available(settings)


def test_sar_settings_read_env_and_validate(monkeypatch):
    sar_plugin.sar_settings.cache_clear()
    monkeypatch.setenv("SAR_ENABLED", "true")
    monkeypatch.setenv("NER_BATCH_SIZE", "32")
    try:
        ner = sar_plugin.sar_settings()
        assert ner.sar_enabled is True
        assert ner.ner_batch_size == 32
        assert ner.ner_threshold == pytest.approx(0.5)
    finally:
        sar_plugin.sar_settings.cache_clear()


def test_sar_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SAR_ENABLED", raising=False)
    sar_plugin.sar_settings.cache_clear()
    try:
        assert sar_plugin.sar_settings().sar_enabled is False
    finally:
        sar_plugin.sar_settings.cache_clear()


def test_emails_phones_and_ni_numbers_are_redacted_except_the_subjects():
    r = Redactor(
        set(),
        keep=["jane.doe@example.org", "+44 7700 900111", "AB 12 34 56 C"],
    )
    text = (
        "From jane.doe@example.org to karen.smith@example.org; "
        "call 07700 900111 or 07700 900222; NI AB123456C, CE 12 34 56 D."
    )
    assert r.redact(text) == (
        "From jane.doe@example.org to [EMAIL_1]; "
        "call 07700 900111 or [PHONE_1]; NI AB123456C, [NI_1]."
    )


def test_dates_and_short_numbers_are_not_phones():
    r = Redactor(set())
    text = "On 01-02-2021 14:30, ref 0123 45, amount 1000000."
    assert r.redact(text) == text


def test_invalid_ni_prefix_is_not_redacted():
    assert Redactor(set()).redact("GB123456A") == "GB123456A"


def test_email_is_one_placeholder_not_a_name():
    r = Redactor({"Karen Smith"})
    assert r.redact("karen.smith@example.org") == "[EMAIL_1]"


def test_seen_counts_per_call():
    r = Redactor({"Karen Smith", "Tom Brown"})
    first: set = set()
    r.redact("Karen Smith, karen@example.org", first)
    second: set = set()
    r.redact("Tom Brown and Karen Smith", second)
    assert counts(first) == {"PERSON": 1, "EMAIL": 1}
    assert counts(second) == {"PERSON": 2}
    # Numbering is archive-wide: Karen is [PERSON_1] in both.
    assert r.redact("Karen Smith") == "[PERSON_1]"


def test_ner_endpoint_normalises_v1_suffix():
    assert ner_endpoint(SimpleNamespace(embedding_gateway_url="https://gw/")) == (
        "https://gw/v1/ner"
    )
    assert ner_endpoint(SimpleNamespace(embedding_gateway_url="https://gw/v1")) == (
        "https://gw/v1/ner"
    )


async def test_detect_entities_windows_long_text_and_splits_labels(mocker):
    client = mocker.AsyncMock()
    client.detect.return_value = [
        {("person", "Karen Smith")},
        {("person", "Tom Brown"), ("address", "14 Mill Lane")},
        set(),
    ]
    names, addresses = await detect_entities(client, ["x" * 2500, "", "short"])
    sent, labels = client.detect.call_args.args
    assert len(sent) == 3  # 2 windows + "short"; the empty text is skipped
    assert labels == ("person", "address")
    assert names == {"Karen Smith", "Tom Brown"}
    assert addresses == {"14 Mill Lane"}


def test_address_is_redacted_wherever_it_occurs_across_line_breaks():
    r = Redactor(set(), addresses={"14 Mill Lane, Harbourvale"})

    assert r.redact(
        "Lives at 14 Mill Lane,\nHarbourvale. Also 14 mill lane harbourvale."
    ) == ("Lives at [ADDRESS_1]. Also [ADDRESS_1].")


def test_address_words_are_not_expanded_and_one_word_addresses_ignored():
    r = Redactor(set(), addresses={"14 Mill Lane", "Harbourvale"})

    assert (
        r.redact("Mill Lane School in Harbourvale") == "Mill Lane School in Harbourvale"
    )


def test_standalone_postcodes_are_redacted_but_the_subjects_kept():
    r = Redactor(set(), keep=["Jane Doe", "3 Oak Road, XK1 1AA"])

    assert r.redact("XA9 8QT and XA98QT, not XK1 1AA or xa9 8qt") == (
        "[ADDRESS_1] and [ADDRESS_1], not XK1 1AA or xa9 8qt"
    )


def test_a_fragment_of_the_subjects_address_is_kept():
    r = Redactor(
        set(),
        keep=["3 Oak Road, Harbourvale, XK1 1AA"],
        addresses={"3 Oak Road", "Oak Road, Harbourvale", "13 Oak Road"},
    )

    assert r.redact("3 Oak Road; 13 Oak Road") == "3 Oak Road; [ADDRESS_1]"


def test_a_street_without_the_subjects_house_number_is_redacted():
    """ "Oak Road, Harbourvale" is inside the subject's address as text, but
    without the house number it may be a neighbour's: it is not kept."""
    r = Redactor(
        set(),
        keep=["3 Oak Road, Harbourvale, XK1 1AA"],
        addresses={"Oak Road, Harbourvale"},
    )

    assert r.redact("The neighbour at Oak Road, Harbourvale called.") == (
        "The neighbour at [ADDRESS_1] called."
    )


@pytest.mark.parametrize(
    "field", ["ner_threshold", "ner_batch_size", "ner_timeout_seconds"]
)
def test_settings_validate_ner(field):
    with pytest.raises(ValueError, match=field):
        SarSettings.model_validate({field: 0})


async def test_get_ner_client_targets_gateway_and_is_cached(monkeypatch):
    from nextcloud_mcp_server import redaction

    redaction._reset_ner_state()
    monkeypatch.setattr(
        redaction,
        "sar_settings",
        lambda: SarSettings(
            ner_model="local/m",
            ner_timeout_seconds=5,
            ner_threshold=0.3,
            ner_batch_size=4,
        ),
    )
    settings = SimpleNamespace(
        embedding_gateway_url="https://gw",
        embedding_gateway_client_id=None,
    )
    try:
        client = await redaction.get_ner_client(settings)
        assert client._url == "https://gw/v1/ner"
        assert client.model == "local/m"
        assert client._threshold == 0.3
        assert client._batch_size == 4
        assert await redaction.get_ner_client(settings) is client
    finally:
        redaction._reset_ner_state()
