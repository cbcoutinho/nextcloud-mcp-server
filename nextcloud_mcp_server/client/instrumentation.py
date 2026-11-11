"""Instrumentation hooks for the Nextcloud clients. They do nothing by default.

The clients do not depend on prometheus or OpenTelemetry. An application that
wants metrics or traces subclasses :class:`Instrumentation` and installs an
instance at startup. The MCP server does so in
``nextcloud_mcp_server.observability.metrics``::

    from nextcloud_mcp_server.client import instrumentation

    class MyHooks(instrumentation.Instrumentation):
        def on_api_call(self, app, method, status_code, duration): ...

    instrumentation.hooks = MyHooks()

Call sites read ``instrumentation.hooks`` at call time, never at import time,
so the installed object takes effect for clients that already exist.
"""

from contextlib import AbstractContextManager, nullcontext


class Instrumentation:
    """No-op hooks. Override the ones you need."""

    def on_api_call(
        self, app: str, method: str, status_code: int, duration: float
    ) -> None:
        """One finished request to Nextcloud; ``status_code`` is 0 if none arrived."""

    def on_api_retry(self, app: str, reason: str) -> None:
        """A request about to be retried (``reason`` is e.g. "429" or "423")."""

    def on_download_truncated(self) -> None:
        """A WebDAV GET returned fewer bytes than expected."""

    def on_scan_truncated(self) -> None:
        """A folder-expansion SEARCH hit its result ceiling."""

    def trace_api_call(
        self, app: str, method: str, path: str | None = None
    ) -> AbstractContextManager[object]:
        """Context manager wrapping one request to Nextcloud, e.g. a trace span."""
        return nullcontext()


hooks: Instrumentation = Instrumentation()
