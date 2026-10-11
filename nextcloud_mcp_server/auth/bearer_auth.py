"""Bearer token authentication for httpx."""

from httpx import Auth, Request


class BearerAuth(Auth):
    """
    Bearer token authentication flow for httpx.

    This auth class adds the Authorization: Bearer <token> header
    to all outgoing requests.
    """

    def __init__(self, token: str):
        """
        Initialize bearer authentication.

        Args:
            token: The bearer token to use for authentication
        """
        self.token = token

    def auth_flow(self, request: Request):
        """
        Add Authorization header to the request.

        Args:
            request: The outgoing HTTP request

        Yields:
            The modified request with Authorization header
        """
        request.headers["Authorization"] = f"Bearer {self.token}"
        # Nextcloud only honours a bearer token on index.php API routes (Deck's
        # /apps/deck/api/..., Notes, Tables, ...) when the request also carries
        # OCS-APIRequest: true; without it every such call is a 401, while Basic
        # auth works either way. Several client methods (e.g. Deck get_stack,
        # get_archived_stacks, archive/assignLabel) send no app headers, so the
        # header is set here for every bearer request instead of per method.
        # setdefault: an explicit per-request value still wins.
        request.headers.setdefault("OCS-APIRequest", "true")
        yield request
