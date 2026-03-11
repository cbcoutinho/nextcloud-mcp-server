"""Path utility functions for safe WebDAV path handling."""

import posixpath


def sanitize_webdav_path(path: str) -> str:
    """Sanitize a WebDAV path to prevent directory traversal attacks.

    Normalizes the path and rejects any containing '..' components.
    Nextcloud blocks traversal server-side, but this provides defense-in-depth.

    Args:
        path: User-provided file/directory path.

    Returns:
        Normalized path without leading slash.

    Raises:
        ValueError: If path contains '..' traversal components.
    """
    normalized = posixpath.normpath(path)
    if ".." in normalized.split("/"):
        raise ValueError(f"Path traversal detected: {path!r}")
    return normalized.lstrip("/")
