"""Pydantic models for Nextcloud sharing responses."""

from enum import IntEnum


class ShareType(IntEnum):
    """OCS ``shareType`` values.

    An ``IntEnum`` so it stays interchangeable with the bare ints the OCS API
    and our tool signatures use.

    Every type addresses a named recipient except :attr:`PUBLIC_LINK`, which
    addresses nobody — see ``validate_share_with`` in the sharing client for
    why that distinction has to be enforced before the request is sent.
    """

    USER = 0
    GROUP = 1
    PUBLIC_LINK = 3
    EMAIL = 4
    FEDERATED = 6
    CIRCLE = 7
    TALK_CONVERSATION = 10
    DECK_CARD = 12
