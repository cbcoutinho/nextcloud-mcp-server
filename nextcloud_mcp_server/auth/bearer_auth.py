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
        yield request
