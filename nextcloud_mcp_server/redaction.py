"""Person-name redaction for SAR workflows (ADR-038).

"Detect once, match everywhere": NER yields the set of person names a document
contains, and redaction is word-boundary matching of that set over whatever text
is about to be returned — a whole parsed file, a search excerpt, its neighbouring
context, a title, a path. Matching by surface form, rather than by stored
character spans, is what lets one detection pass cover text that every surface
slices differently.

Two recall aids, both biased toward over-redaction, which is the acceptable
failure for a disclosure:

* **propagation** — a name detected once is redacted at every occurrence,
  including ones the model missed in context;
* **token expansion** — each token (3+ chars, not an honorific) of a
  multi-token name is redacted on its own, so a later bare "Smith" is caught.

The data subject passes through via ``keep_names``: the caller supplies the
subject's name and aliases, which match first (longest match wins) and are left
as written. Tokens of a detected name that is itself kept are not expanded, so a
bare "Jane" survives when only "Jane Doe" is the subject — unless "Jane" is also
a token of some third party's name, in which case it is redacted.

Redaction is optional and gateway-only: :func:`redaction_mode` is "off" unless
``CONTENT_REDACTION`` asks for it AND ``EMBEDDING_GATEWAY_URL`` is configured.
"""

import re
from collections.abc import Iterable
from typing import Any, Literal

import anyio

from nextcloud_mcp_server.providers.gateway import build_gateway_token_provider
from nextcloud_mcp_server.providers.ner import NerClient, windows

RedactionMode = Literal["off", "optional", "enforced"]

# Scope that exempts a principal from enforced redaction. Checked only when
# redaction_mode() is "enforced"; a no-op otherwise.
UNREDACTED_SCOPE = "content.unredacted"

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
# Between the tokens of a multi-token name: whitespace (including the line
# breaks OCR and markdown introduce) and the separators filenames and email
# local-parts use ("KAREN_SMITH.pdf", "karen.smith@").
_TOKEN_SEPARATOR = r"[\s_.\-]+"
# "Not preceded/followed by a letter or digit". Deliberately not \b: an
# underscore is a word character to \b, so "KAREN_SMITH" would not match.
_LEFT = r"(?<![^\W_])"
_RIGHT = r"(?![^\W_])"

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


def redaction_mode(settings: Any) -> RedactionMode:
    """The EFFECTIVE redaction mode: the configured one, or "off" when there is
    no gateway to detect names with."""
    mode = getattr(settings, "content_redaction", "off")
    if mode not in ("optional", "enforced") or not ner_endpoint(settings):
        return "off"
    return mode


async def get_ner_client(settings: Any) -> NerClient:
    """The shared NER client. Call only when :func:`redaction_mode` is not
    "off"."""
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
            )
    return _client


async def detect_names(client: NerClient, texts: Iterable[str]) -> set[str]:
    """Every person name in ``texts`` (windowed as needed), as one set.

    Raises:
        NerError: detection failed; the caller must not return the text.
    """
    slices = [w for t in texts if t for w in windows(t)]
    if not slices:
        return set()
    return set().union(*await client.detect(slices))


def _key(name: str) -> str:
    """Canonical form: tokens split on any name separator, casefolded.

    Used both to build the alternatives and to look a match up again, so
    "Smith-Jones", "SMITH JONES" and "smith_jones" are one person.
    """
    return " ".join(t for t in re.split(_TOKEN_SEPARATOR, name) if t).casefold()


def _tokens(key: str) -> list[str]:
    return [
        t for t in key.split() if len(t) >= _MIN_TOKEN_CHARS and t not in _HONORIFICS
    ]


def _pattern(key: str) -> str:
    return _TOKEN_SEPARATOR.join(re.escape(t) for t in key.split())


class Redactor:
    """Replaces person names with ``[PERSON_n]``, keeping ``keep_names``.

    Use one instance per response so the numbering is consistent across every
    field and result it redacts: the same person is ``[PERSON_2]`` in an excerpt
    and in the title beside it.
    """

    def __init__(self, names: Iterable[str], keep_names: Iterable[str] = ()) -> None:
        self._keep = {key for k in keep_names if (key := _key(k))}
        forms = set(self._keep)
        for name in names:
            if not (key := _key(name)):
                continue
            forms.add(key)
            if key not in self._keep:
                # ponytail: a bare surname gets its own number rather than the
                # full name's. Reconciling aliases is the person-entity layer's
                # job (Deck P9), not string matching's.
                forms.update(_tokens(key))
        self._numbers: dict[str, int] = {}
        # Longest first, so a kept "Jane Doe" wins over a redacted "Doe".
        alternatives = sorted(forms, key=len, reverse=True)
        self._regex = (
            re.compile(
                _LEFT + "(?:" + "|".join(map(_pattern, alternatives)) + ")" + _RIGHT,
                re.IGNORECASE,
            )
            if alternatives
            else None
        )
        # Forms that are only keep aliases can still match; they are left alone.
        self._redactable = forms - self._keep

    @property
    def persons_redacted(self) -> int:
        """Distinct names replaced so far across every ``redact`` call."""
        return len(self._numbers)

    def _replace(self, match: re.Match[str]) -> str:
        form = _key(match.group(0))
        if form not in self._redactable:
            return match.group(0)
        number = self._numbers.setdefault(form, len(self._numbers) + 1)
        return f"[PERSON_{number}]"

    def redact(self, text: str | None) -> str | None:
        if not text or self._regex is None:
            return text
        return self._regex.sub(self._replace, text)
