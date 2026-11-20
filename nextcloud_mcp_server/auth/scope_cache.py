"""Per-user cache of the scopes stored with an app password.

Lives apart from ``scope_authorization`` so the storage layer can clear an entry
whenever it deletes an app password: ``scope_authorization`` imports the storage,
so the storage cannot import it back.
"""

#: user_id -> (expires_at, scopes)
_scope_cache: dict[str, tuple[float, list[str] | str | None]] = {}
_SCOPE_CACHE_TTL = 300  # 5 minutes


def invalidate_scope_cache(user_id: str) -> None:
    """Remove cached scopes for a user (call when scopes are updated)."""
    _scope_cache.pop(user_id, None)
