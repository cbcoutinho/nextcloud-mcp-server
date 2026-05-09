"""Shared validators for primitive types crossing system boundaries."""

import re

# Nextcloud object IDs are unsigned ints from MySQL AUTO_INCREMENT, which
# starts at 1. Restrict to ASCII positive integers to exclude Unicode digit
# classes (e.g. superscripts, Arabic-Indic numerals) that pass str.isdigit()
# / str.isdecimal() but would never be valid Nextcloud IDs, and to reject "0"
# and leading zeros.
_NEXTCLOUD_DOC_ID_RE = re.compile(r"^[1-9][0-9]*$")


def is_valid_nextcloud_doc_id(value: str) -> bool:
    """True iff `value` is the str form of a positive ASCII integer (>= 1)."""
    return bool(_NEXTCLOUD_DOC_ID_RE.fullmatch(value))
