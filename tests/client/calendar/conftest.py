"""Shared fixtures for calendar integration tests.

Note: The temporary_calendar fixture is defined in tests/conftest.py and uses
a shared session-scoped calendar to avoid Nextcloud rate limiting issues.
This conftest.py exists for any calendar-specific fixtures that might be needed
in the future.
"""

import logging

logger = logging.getLogger(__name__)
