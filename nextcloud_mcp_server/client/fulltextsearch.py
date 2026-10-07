"""Client for the Nextcloud FullTextSearch app HTTP API.

Wraps the ``fulltextsearch`` app's ``Api#searchFromRemote`` route
(``GET /apps/fulltextsearch/v1/remote``), which takes a single ``request`` query
parameter holding a JSON-encoded ``SearchRequest`` and returns hits grouped by
whichever search providers are enabled on the instance.

``/v1/remote`` is chosen over the browser-oriented ``/v1/search`` because it is
the app's ``@NoCSRFRequired`` handler: ``/v1/search`` sits behind Nextcloud's CSRF
middleware and answers API-auth requests with ``HTTP 412 (CSRF check failed)``.
Both handlers share the same ``request`` parameter, resolve the user through
``IUserSession`` (so app-password / bearer auth works), and return the identical
result envelope.

``providers`` semantics are easy to trip over: the endpoint's filter expects
either concrete provider ids (``files``, ``deck``, …) or the literal string
``all``. A JSON empty list means *zero* providers and answers HTTP 200 /
status 1 with no hits (``ProviderService::getFilteredProviders`` checks
``in_array('all', $providerList)``, then unions only the named ids). This
client therefore maps an unset provider filter onto ``["all"]``.

Prerequisites on the server side, none of which this client can arrange:

* the ``fulltextsearch`` app is installed and enabled,
* a search platform (Electra / Elasticsearch / *) is configured, and
* content has been indexed, and
* a provider that actually indexes *files* is installed — ``files`` from
  ``fulltextsearch``'s ``files_fulltextsearch`` sibling app indexes file
  contents and is the usual one. Without such a provider the endpoint answers
  but returns no file rows.
"""

import json
import logging
from typing import Any

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)

_SEARCH_PATH = "/apps/fulltextsearch/v1/remote"


class FullTextSearchClient(BaseNextcloudClient):
    """Talk to the Nextcloud FullTextSearch app's search endpoint."""

    app_name = "fulltextsearch"

    async def search(
        self,
        term: str,
        providers: list[str] | None = None,
        page: int = 1,
        size: int = 10,
    ) -> list[dict[str, Any]]:
        """Run a full-text search and return flattened, provider-aware hits.

        Args:
            term: Free-text query matched against indexed content.
            providers: Restrict to these provider ids. ``None`` / empty searches
                every provider the instance has configured (serialized as the
                app's ``all`` sentinel — a literal empty list selects nothing).
            page: 1-based result page (the app clamps anything below 1 to 1).
            size: Results per page.

        Returns:
            A flat list of normalised hit dicts with keys ``provider``, ``title``,
            ``path``, ``url``, ``subtitle``, ``score``, ``reference``,
            ``attributes``. Entries the app reports but that carry neither a path
            nor a url are still returned (their fields are simply ``None``).
        """
        search_request = {
            # ``providers`` is read by SearchRequest::importFromArray via direct
            # array access, so it must always be present; and FTS treats a
            # literal empty list as ZERO providers, so "no filter" must be sent
            # as its "all" sentinel or every search silently returns nothing.
            "providers": list(providers) if providers else ["all"],
            "search": term,
            "page": max(page, 1),
            "size": size,
        }

        response = await self._make_request(
            "GET",
            _SEARCH_PATH,
            params={"request": json.dumps(search_request)},
        )
        payload = response.json()

        status = payload.get("status")
        if status != 1:
            message = (
                payload.get("message") or payload.get("exception") or "unknown error"
            )
            logger.warning("FullTextSearch returned status %s: %s", status, message)
            return []

        return self._flatten(payload.get("result"))

    @staticmethod
    def _flatten(result: Any) -> list[dict[str, Any]]:
        """Flatten the app's provider-grouped result into a list of hit dicts.

        The ``result`` envelope shape varies by app version and platform, so
        this tolerates the observed forms:

        * a list of per-provider groups keyed by ``documents`` (current FTS,
          ``[{"provider": {"id", ...}, "documents": [...]}, ...]``) or by
          ``entries`` (older releases),
        * a single group dict, or
        * a mapping of ``provider_id -> group``.
        """
        if not result:
            return []

        groups: list[tuple[str, list[Any]]] = []

        if isinstance(result, dict):
            if "entries" in result or "documents" in result:
                groups.append((_group_provider_id(result), _group_items(result)))
            else:
                for provider_id, group in result.items():
                    if isinstance(group, dict):
                        groups.append((provider_id, _group_items(group)))
                    elif isinstance(group, list):
                        groups.append((provider_id, group))
        elif isinstance(result, list):
            for item in result:
                if not isinstance(item, dict):
                    continue
                if "entries" in item or "documents" in item:
                    groups.append((_group_provider_id(item), _group_items(item)))
                else:
                    groups.append((_group_provider_id(item), [item]))

        hits: list[dict[str, Any]] = []
        for provider_id, entries in groups:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                hits.append(_entry_to_hit(provider_id, entry))
        return hits


def _group_provider_id(group: dict[str, Any]) -> str:
    """Extract a provider id from a result group.

    Current FTS nests it as ``{"provider": {"id": ..., "name": ...}}``; older
    shapes carry ``provider``/``app`` as plain strings.
    """
    provider = group.get("provider") or group.get("app") or ""
    if isinstance(provider, dict):
        provider = provider.get("id") or provider.get("name") or ""
    return provider if isinstance(provider, str) else ""


def _group_items(group: dict[str, Any]) -> list[Any]:
    """Entries of a result group under whichever key this FTS version uses."""
    return group.get("documents") or group.get("entries") or []


def _first_str(entry: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _entry_to_hit(provider_id: str | None, entry: dict[str, Any]) -> dict[str, Any]:
    """Map one serialised FTS entry to the normalised hit dict.

    Field names drift across FTS versions, so each promoted field probes several
    aliases and falls back to the metadata bag (``attributes``, or ``info`` on
    current FTS).
    """
    attributes_raw = entry.get("attributes")
    if not isinstance(attributes_raw, dict):
        # Current FTS file documents carry path/mime/size metadata under "info".
        attributes_raw = entry.get("info")
    attributes: dict[str, str] = (
        {k: str(v) for k, v in attributes_raw.items()}
        if isinstance(attributes_raw, dict)
        else {}
    )

    route = entry.get("route")
    route_url = route.get("url") if isinstance(route, dict) else None

    url = _first_str(entry, "url", "link") or route_url or attributes.get("url")
    path = attributes.get("path") or attributes.get("source")
    title = _first_str(entry, "title", "name") or ""
    subtitle = _first_str(entry, "sub_title", "subtitle", "info") or _best_excerpt(
        entry
    )
    reference = _first_str(entry, "reference", "id", "documentId")

    return {
        "provider": provider_id
        or _first_str(entry, "providerId", "provider", "app")
        or "",
        "title": title,
        "path": path,
        "url": url,
        "subtitle": subtitle,
        "score": _coerce_score(entry.get("score")),
        "reference": reference,
        "attributes": attributes,
    }


def _best_excerpt(entry: dict[str, Any]) -> str | None:
    """The highest-ranked highlighted snippet, when a platform attached one."""
    excerpts = entry.get("excerpts")
    if isinstance(excerpts, list):
        for excerpt in excerpts:
            if isinstance(excerpt, dict):
                text = excerpt.get("excerpt")
                if isinstance(text, str) and text.strip():
                    return text.strip()
    return None


def _coerce_score(raw: Any) -> float | None:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        # The SQL platform serializes relevance scores as decimal strings.
        try:
            return float(raw)
        except ValueError:
            return None
    return None
