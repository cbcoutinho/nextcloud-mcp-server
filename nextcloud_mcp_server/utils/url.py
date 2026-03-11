"""URL validation utilities for SSRF protection."""

import ipaddress
from urllib.parse import urlparse


def validate_external_url(url: str) -> str:
    """Validate that a URL points to an external resource.

    Rejects non-HTTP schemes and URLs pointing to private/loopback/link-local
    IP addresses to prevent SSRF attacks. Hostname-based URLs that resolve to
    private IPs are not caught here (DNS resolution is not performed), but the
    schema and IP-literal checks cover the most common SSRF vectors.

    Args:
        url: URL to validate.

    Returns:
        The URL unchanged if valid.

    Raises:
        ValueError: If the URL scheme is not http/https or points to a
            private/internal IP address.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"URL scheme must be http or https, got {parsed.scheme!r}"
        )
    if parsed.hostname:
        try:
            ip = ipaddress.ip_address(parsed.hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise ValueError(
                    f"URL points to private/internal address: {parsed.hostname}"
                )
        except ValueError as e:
            # ip_address() raises ValueError for non-IP hostnames — that's fine,
            # it means the hostname is a DNS name (not an IP literal)
            if "does not appear to be" not in str(e):
                raise
    return url
