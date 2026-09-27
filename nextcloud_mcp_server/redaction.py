"""Third-party redaction for SAR export archives (ADR-040).

"Detect once, match everywhere": NER yields the set of person names across every
document going into an archive, and redaction is word-boundary matching of that
set over each piece of text written to it: document bodies, titles, filenames and
the operator's inclusion reasons. Matching by surface form, rather than by stored
character spans, is what lets one detection pass cover all of them, and one
:class:`Redactor` per archive numbers each person consistently across it.

Two recall aids, both biased toward over-redaction, which is the acceptable
failure for a disclosure:

* **propagation** — a name detected once is redacted at every occurrence,
  including ones the model missed in context;
* **token expansion** — each token (3+ chars, not an honorific) of a
  multi-token name is redacted on its own, so a later bare "Smith" is caught.
  One deliberate exception: role and relationship words ("student", "Father")
  are never names, and a detection containing one ("Academic Mentor", "Father
  Brown") is redacted as a whole phrase only. Its other words are not expanded,
  so a later bare "Brown" relies on the model detecting it there.

Addresses are detected by NER too and propagate the same way, but as whole
phrases only: their words are never expanded, since redacting every "Street" or
"Road" would destroy the text. A single-word address ("Harbourvale") identifies no one
and is left alone. UK postcodes are also matched by pattern, so a postcode on
its own is redacted even where NER saw no address.

Emails, phone numbers and UK National Insurance numbers are found by pattern in
each text, not by NER, and are redacted wherever they occur.

The data subject passes through via ``keep``: their names, aliases, emails,
phone numbers, NI numbers and addresses. A detected address is kept when it is a
kept address or its leading part (house number first), never a bare street.
Kept names match first (longest match wins) and
are left as written. Tokens of a detected name that is itself kept are not
expanded, so a bare "Jane" survives when only "Jane Doe" is the subject — unless
"Jane" is also a token of some third party's name, in which case it is redacted.

Detection needs the embedding gateway's ``POST /v1/ner``, so redaction is
available only with ``EMBEDDING_GATEWAY_URL`` configured.
"""
# ponytail: dates of birth and staff/student IDs are not detected yet; add
# labels (GLiNER is zero-shot) when the auditor asks for them.

import re
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

import anyio

from nextcloud_mcp_server.providers.gateway import build_gateway_token_provider
from nextcloud_mcp_server.providers.ner import (
    ADDRESS_LABEL,
    PERSON_LABEL,
    NerClient,
    windows,
)

PERSON = "PERSON"
ADDRESS = "ADDRESS"
EMAIL = "EMAIL"
PHONE = "PHONE"
NI = "NI"

_MIN_TOKEN_CHARS = 3
# Titles that precede a name but are not part of it. A missed one only costs a
# needless token expansion (over-redaction), so this need not be exhaustive.
_HONORIFICS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "miss",
        "mx",
        "dr",
        "prof",
        "sir",
        "dame",
        "lord",
        "lady",
        "rev",
        "revd",
        "hon",
        "capt",
        "col",
        "sgt",
        "fr",
        "sr",
        "jr",
    }
)
# Roles and relationships the model tags as people ("student", "Father"). They
# are never a name, and one registered as a name token would be redacted
# wherever the word occurs, so they are never names or name tokens. Closed on
# purpose: it holds no names, so it cannot hide a person.
_ROLE_WORDS = frozenset(
    {
        "student",
        "students",
        "pupil",
        "pupils",
        "teacher",
        "teachers",
        "tutor",
        "headteacher",
        "parent",
        "parents",
        "mother",
        "father",
        "mum",
        "dad",
        "guardian",
        "carer",
        "child",
        "children",
        "son",
        "daughter",
        "brother",
        "sister",
        "aunt",
        "uncle",
        "grandmother",
        "grandfather",
        "husband",
        "wife",
        "partner",
        "doctor",
        "nurse",
        "patient",
        "client",
        "employee",
        "manager",
        "colleague",
        "applicant",
        "subject",
        "mentor",
        "coach",
        "coordinator",
        "counsellor",
        "assistant",
        "governor",
        "officer",
        "secretary",
        "chaplain",
    }
)
_NOT_NAMES = _HONORIFICS | _ROLE_WORDS
# Between the tokens of a multi-token name: whitespace (including the line
# breaks OCR and markdown introduce) and the separators filenames and email
# local-parts use ("KAREN_SMITH.pdf", "karen.smith@").
_TOKEN_SEPARATOR = r"[\s_.\-]+"
# "Not preceded/followed by a letter or digit". Deliberately not \b: an
# underscore is a word character to \b, so "KAREN_SMITH" would not match.
_LEFT = r"(?<![^\W_])"
_RIGHT = r"(?![^\W_])"

# Pattern-detected identifiers, applied in this order: an email goes first so
# "karen.smith@example.org" becomes one [EMAIL_n] rather than a name plus a
# domain.
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# UK NI number: two prefix letters, six digits, suffix A-D; spaced or not.
_NI_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D](?![A-Za-z0-9])"
)
_NI_INVALID_PREFIXES = frozenset({"BG", "GB", "NK", "KN", "TN", "NT", "ZZ"})
# ponytail: a 10-15 digit run starting with "+" or "0" counts as a phone
# number, so a reference number formatted the same way is redacted too
# (over-redaction). Swap for a phone-number library if that proves too noisy.
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+|0)[\d\s()-]{8,}\d(?!\w)")
_PHONE_DIGITS = range(10, 16)
# A date followed by a time ("01-02-2021 14:30") has as many digits as a phone
# number; never read one as a phone.
_DATE_RE = re.compile(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{4}-\d{2}-\d{2}")
# UK postcode, e.g. "XA9 8QT", "SW1A 1AA". Upper case only: lower-case
# look-alikes are far likelier to be codes or words than a postcode.
_POSTCODE_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}(?![A-Za-z0-9])"
)
# Between the words of an address, which PDFs and forms break across lines.
_ADDRESS_SEPARATOR = r"[\s,]+"
_MIN_ADDRESS_WORDS = 2

_client: NerClient | None = None
_client_lock: anyio.Lock | None = None


def _reset_ner_state() -> None:
    """Drop the cached client. Test hook, mirrors ``search.rerank``."""
    global _client, _client_lock
    _client = None
    _client_lock = None


def ner_endpoint(settings: Any) -> str | None:
    """``<gateway>/v1/ner``, or ``None`` without a gateway."""
    gateway = getattr(settings, "embedding_gateway_url", None)
    if not gateway:
        return None
    base = gateway.rstrip("/")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return f"{base}/ner"


def redaction_available(settings: Any) -> bool:
    """Whether names can be detected, i.e. an embedding gateway is configured."""
    return ner_endpoint(settings) is not None


def sar_available(settings: Any) -> bool:
    """Whether SAR cases are served (ADR-040): the deployment opted in with
    ``SAR_ENABLED``, and has what they need (the index to search and read, and
    the embedding gateway to detect names)."""
    return (
        bool(getattr(settings, "sar_enabled", False))
        and bool(settings.vector_sync_enabled)
        and redaction_available(settings)
    )


async def get_ner_client(settings: Any) -> NerClient:
    """The shared NER client. Call only when :func:`redaction_available`."""
    global _client, _client_lock
    url = ner_endpoint(settings)
    if url is None:
        raise RuntimeError("redaction requested without EMBEDDING_GATEWAY_URL")
    if _client is not None:
        return _client
    if _client_lock is None:
        _client_lock = anyio.Lock()
    async with _client_lock:
        if _client is None:
            _client = NerClient(
                url=url,
                model=settings.ner_model,
                token_provider=build_gateway_token_provider(settings),
                threshold=float(settings.ner_threshold),
                timeout_seconds=float(settings.ner_timeout_seconds),
                batch_size=int(settings.ner_batch_size),
            )
    return _client


async def detect_entities(
    client: NerClient, texts: Iterable[str]
) -> tuple[set[str], set[str]]:
    """Every person name and every address in ``texts`` (windowed as needed).

    Raises:
        NerError: detection failed; the caller must not export the text.
    """
    names: set[str] = set()
    addresses: set[str] = set()
    slices = [w for t in texts if t for w in windows(t)]
    if slices:
        for found in await client.detect(slices, (PERSON_LABEL, ADDRESS_LABEL)):
            for label, surface in found:
                (names if label == PERSON_LABEL else addresses).add(surface)
    return names, addresses


def _key(name: str) -> str:
    """Canonical form: tokens split on any name separator, casefolded.

    Used both to build the alternatives and to look a match up again, so
    "Smith-Jones", "SMITH JONES" and "smith_jones" are one person.
    """
    return " ".join(t for t in re.split(_TOKEN_SEPARATOR, name) if t).casefold()


def _tokens(key: str) -> list[str]:
    return [
        t for t in key.split() if len(t) >= _MIN_TOKEN_CHARS and t not in _NOT_NAMES
    ]


def _pattern(key: str) -> str:
    return _TOKEN_SEPARATOR.join(re.escape(t) for t in key.split())


def _phone_key(raw: str) -> str | None:
    if _DATE_RE.search(raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) not in _PHONE_DIGITS:
        return None
    # +44 7700 900123 and 07700 900123 are the same number.
    if digits.startswith("44") and len(digits) >= 12:
        digits = "0" + digits[2:]
    return digits


def _address_key(raw: str) -> str:
    return " ".join(w for w in re.split(_ADDRESS_SEPARATOR, raw) if w).casefold()


def _postcode_key(raw: str) -> str:
    return re.sub(r"\s", "", raw).upper()


def _ni_key(raw: str) -> str | None:
    key = re.sub(r"\s", "", raw).upper()
    return None if key[:2] in _NI_INVALID_PREFIXES else key


def _alternation(
    keys: Iterable[str], pattern: Callable[[str], str]
) -> re.Pattern[str] | None:
    """One case-insensitive regex matching any key, longest first, or None."""
    ordered = sorted(keys, key=len, reverse=True)
    if not ordered:
        return None
    body = "|".join(map(pattern, ordered))
    return re.compile(_LEFT + "(?:" + body + ")" + _RIGHT, re.IGNORECASE)


def _address_pattern(
    addresses: Iterable[str], keep: list[str]
) -> re.Pattern[str] | None:
    """Detected addresses of two or more words, minus the subject's own: a kept
    address, or its leading part ("3 Oak Road" of "3 Oak Road, Harbourvale")."""
    # Leading part only, and padded to whole words: a neighbour's "Oak Road,
    # Harbourvale" (no house number) is someone else's address, and "2 High St"
    # is not "12 High St".
    kept = [f" {_address_key(k)} " for k in keep if "@" not in k]
    keys = {
        key
        for a in addresses
        if len((key := _address_key(a)).split()) >= _MIN_ADDRESS_WORDS
        and not any(k.startswith(f" {key} ") for k in kept)
    }
    return _alternation(
        keys, lambda key: _ADDRESS_SEPARATOR.join(map(re.escape, key.split()))
    )


def _name_forms(
    names: Iterable[str], kept: set[str]
) -> tuple[set[str], dict[str, set[str]]]:
    """Every matchable form of the detected names, and for each token or
    title-free phrase, the full names it comes from."""
    forms = set(kept)
    owners: dict[str, set[str]] = {}
    for name in names:
        if not (key := _key(name)):
            continue
        # "student", "Mrs Mother": nothing but titles and roles.
        if all(t in _NOT_NAMES for t in key.split()):
            continue
        forms.add(key)
        # A phrase with a role in it is a job title ("Academic Mentor") or a
        # title-led name ("Father Brown"): redact it whole, but never expand it
        # into bare words, which would redact "academic" wherever it occurs.
        if key in kept or any(t in _ROLE_WORDS for t in key.split()):
            continue
        tokens = _tokens(key)
        # "Rev Tom Brown" recurring as plain "Tom Brown" is one person, so the
        # title-free phrase is a form of its own, numbered like a bare token.
        phrase = [" ".join(tokens)] if len(tokens) > 1 else []
        for token in [*tokens, *phrase]:
            forms.add(token)
            if token != key:
                owners.setdefault(token, set()).add(key)
    return forms, owners


class Redactor:
    """Replaces third parties' names and identifiers with ``[LABEL_n]``.

    Use one instance per archive so numbering is consistent across every
    document, title, filename and reason it redacts: the same person is
    ``[PERSON_2]`` everywhere.
    """

    def __init__(
        self,
        names: Iterable[str],
        keep: Iterable[str] = (),
        *,
        addresses: Iterable[str] = (),
    ) -> None:
        keep = [k for k in keep if k and k.strip()]
        self._keep_postcodes = {
            _postcode_key(m) for k in keep for m in _POSTCODE_RE.findall(k)
        }
        self._addresses_re = _address_pattern(addresses, keep)
        self._keep_emails = {k.strip().casefold() for k in keep if "@" in k}
        self._keep_phones = {p for k in keep if (p := _phone_key(k))}
        self._keep_ni = {
            n for k in keep if _NI_RE.fullmatch(k.strip()) and (n := _ni_key(k))
        }
        self._keep = {key for k in keep if "@" not in k and (key := _key(k))}
        forms, owners = _name_forms(names, self._keep)
        # A bare token shares its full name's number only when it is
        # unambiguous: it belongs to exactly one detected third party and to
        # none of the subject's kept names. "Doe" shared by the subject "Jane
        # Doe" and a third party "John Doe" keeps its own number, so the
        # archive never attributes an ambiguous mention to a specific person.
        # ponytail: string-level alias resolution; a person-entity layer
        # (Deck P9) would reconcile initials and nicknames too.
        kept_tokens = {t for k in self._keep for t in k.split()}
        self._canonical = {
            token: next(iter(full))
            for token, full in owners.items()
            if len(full) == 1 and token not in kept_tokens
        }
        self._numbers: dict[tuple[str, str], int] = {}
        self._counters: Counter[str] = Counter()
        # Longest first, so a kept "Jane Doe" wins over a redacted "Doe". Sorting
        # by canonical key rather than by matched text is enough: two
        # alternatives only compete at one position when one's tokens are a
        # prefix of the other's, and then the longer key is also the longer
        # match whatever separators the text uses.
        self._names_re = _alternation(forms, _pattern)
        # Forms that are only keep aliases can still match; they are left alone.
        self._redactable = forms - self._keep

    def _placeholder(
        self, label: str, key: str, seen: set[tuple[str, str]] | None
    ) -> str:
        ident = (label, key)
        if ident not in self._numbers:
            self._counters[label] += 1
            self._numbers[ident] = self._counters[label]
        if seen is not None:
            seen.add(ident)
        return f"[{label}_{self._numbers[ident]}]"

    def redact(
        self, text: str | None, seen: set[tuple[str, str]] | None = None
    ) -> str | None:
        """``text`` with third parties replaced.

        ``seen``, when given, collects the ``(label, key)`` of every entity
        replaced in this call, so a caller can count per document.
        """
        if not text:
            return text

        def email(m: re.Match[str]) -> str:
            key = m.group(0).casefold()
            if key in self._keep_emails:
                return m.group(0)
            return self._placeholder(EMAIL, key, seen)

        def address(m: re.Match[str]) -> str:
            return self._placeholder(ADDRESS, _address_key(m.group(0)), seen)

        def postcode(m: re.Match[str]) -> str:
            key = _postcode_key(m.group(0))
            if key in self._keep_postcodes:
                return m.group(0)
            return self._placeholder(ADDRESS, key, seen)

        def ni(m: re.Match[str]) -> str:
            key = _ni_key(m.group(0))
            if key is None or key in self._keep_ni:
                return m.group(0)
            return self._placeholder(NI, key, seen)

        def phone(m: re.Match[str]) -> str:
            key = _phone_key(m.group(0))
            if key is None or key in self._keep_phones:
                return m.group(0)
            return self._placeholder(PHONE, key, seen)

        def name(m: re.Match[str]) -> str:
            key = _key(m.group(0))
            if key not in self._redactable:
                return m.group(0)
            return self._placeholder(PERSON, self._canonical.get(key, key), seen)

        text = _EMAIL_RE.sub(email, text)
        # A whole address before its parts: its postcode is then already gone,
        # and a street named after a person stays one address.
        if self._addresses_re is not None:
            text = self._addresses_re.sub(address, text)
        text = _POSTCODE_RE.sub(postcode, text)
        text = _NI_RE.sub(ni, text)
        text = _PHONE_RE.sub(phone, text)
        if self._names_re is not None:
            text = self._names_re.sub(name, text)
        return text


def counts(seen: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Distinct entities per label, e.g. ``{"PERSON": 3, "EMAIL": 1}``."""
    return dict(Counter(label for label, _ in seen))
