"""Parsers for operator- and peer-supplied scalar strings.

Each of these was previously re-implemented at several call sites with
diverging edge cases (``"1"`` enabling one flag but not another, ``int()`` on a
``Retry-After`` HTTP-date raising mid-retry). One definition, one property test.
"""

from typing import Any

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})


def parse_bool(value: Any, default: bool = False) -> bool:
    """Interpret a flag the way an operator would type it.

    Dynaconf only casts lowercase ``true``/``false``, so ``FLAG=False`` arrives
    as the string ``"False"`` and ``bool("False")`` is ``True``. Recognised
    spellings (any case, surrounding whitespace ignored) map to their meaning;
    anything else yields *default*.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    word = str(value).strip().lower()
    if word in _TRUE:
        return True
    if word in _FALSE:
        return False
    return default


def split_csv(raw: str | None) -> list[str]:
    """Split a comma-separated value into stripped items, dropping blanks."""
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def parse_retry_after(value: str | None) -> int | None:
    """Parse ``Retry-After`` delta-seconds (RFC 9110 §10.2.3: ``1*DIGIT``).

    Returns a positive number of seconds, or None for an absent, zero,
    malformed or HTTP-date value, in which case the caller uses its own
    back-off.
    """
    raw = (value or "").strip()
    if not (raw.isascii() and raw.isdigit()):
        return None
    return int(raw) or None
