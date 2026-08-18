"""Error-formatting helpers for the vector-sync pipeline.

Vector-sync work runs inside anyio task groups, so a failure in a child task
surfaces as a ``BaseExceptionGroup`` whose default ``str()`` is the useless
``"unhandled errors in a TaskGroup (N sub-exception)"`` -- it hides the real
``ConnectError`` / ``APIConnectionError`` that operators need to triage embed
drops (card 309). ``format_exception_group`` flattens the group to the leaf
exceptions so log lines name the actual cause in a single concise line -- no
``exc_info``/traceback. Tracebacks are reserved for genuinely unhandled
exceptions; handled errors log the leaf cause only.

Leaves are also *described* rather than plain ``repr``-ed. An httpx transport
error usually carries an empty message, so the bare repr degrades to
``ReadTimeout('')`` -- which says nothing about what timed out (GH #1345: an
operator seeing that line could not tell the embedding endpoint from Nextcloud
or Qdrant). ``_describe`` appends the request's method/host/path and any HTTP
status, so the line names the endpoint that failed.

Credentials are never logged: the URL is rebuilt from scheme/host/port/path
rather than stringified, because ``str(httpx.URL)`` renders inline userinfo in
the clear (only ``repr`` obfuscates it) and the query string can carry tokens.
"""


def format_exception_group(exc: BaseException) -> str:
    """Return a concise, leaf-naming string for ``exc``.

    For a (possibly nested) ``BaseExceptionGroup`` this joins a description of
    each leaf exception; for an ordinary exception it describes that one. The
    result is meant for the human-readable portion of a log message, not for
    parsing.
    """
    if not isinstance(exc, BaseExceptionGroup):
        return _describe(exc)
    leaves = _flatten(exc)
    noun = "sub-exception" if len(leaves) == 1 else "sub-exceptions"
    return f"{len(leaves)} {noun}: " + "; ".join(_describe(e) for e in leaves)


def _endpoint(exc: BaseException) -> str | None:
    """``"POST ollama:11434/api/embed"`` for an httpx error, else ``None``.

    Deliberately not ``str(request.url)``: httpx renders inline userinfo in the
    clear there (only ``repr`` obfuscates it) and the query string may carry a
    token, so the URL is rebuilt from the safe components only.

    ``httpx.RequestError.request`` RAISES ``RuntimeError`` when unset rather than
    returning None, so a plain ``getattr(exc, "request", None)`` is not enough --
    it would propagate out of a logging helper. Best-effort throughout: anything
    unexpected degrades to no endpoint rather than breaking the log line.
    """
    try:
        request = exc.request  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]  # httpx-only, guarded by the except
        method = request.method
        url = request.url
        host = url.host
        if not host:
            return None
        port = f":{url.port}" if url.port else ""
        return f"{method} {host}{port}{url.path}"
    except Exception:
        return None


def _describe(exc: BaseException) -> str:
    """``repr(exc)`` plus the endpoint/status context httpx leaves out."""
    parts = [repr(exc)]
    endpoint = _endpoint(exc)
    if endpoint:
        parts.append(f"on {endpoint}")
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        parts.append(f"(HTTP {status})")
    return " ".join(parts)


def _flatten(exc: BaseException) -> list[BaseException]:
    """Depth-first list of the leaf exceptions within ``exc``."""
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(_flatten(sub))
        return leaves
    return [exc]
