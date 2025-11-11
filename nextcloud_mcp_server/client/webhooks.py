"""Client for Nextcloud Webhook Listeners API operations."""

from typing import Any, Dict, List, Optional

from nextcloud_mcp_server.client.base import BaseNextcloudClient


class WebhooksClient(BaseNextcloudClient):
    """Client for Nextcloud webhook_listeners app API operations."""

    app_name = "webhooks"

    def _get_webhook_headers(
        self, additional_headers: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """Get standard headers required for Webhook Listeners API calls."""
        headers = {"OCS-APIRequest": "true", "Accept": "application/json"}
        if additional_headers:
            headers.update(additional_headers)
        return headers

    async def list_webhooks(self) -> List[Dict[str, Any]]:
        """List all registered webhooks for the current user.

        Returns:
            List of webhook registrations with id, uri, event, filters, etc.
        """
        headers = self._get_webhook_headers()
        response = await self._make_request(
            "GET",
            "/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks",
            headers=headers,
        )
        data = response.json()["ocs"]["data"]
        return data if isinstance(data, list) else []

    async def create_webhook(
        self,
        event: str,
        uri: str,
        http_method: str = "POST",
        auth_method: str = "none",
        headers: Optional[Dict[str, str]] = None,
        event_filter: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Register a new webhook for the specified event.

        Args:
            event: Fully qualified event class name (e.g., "OCP\\Files\\Events\\Node\\NodeCreatedEvent")
            uri: Webhook endpoint URL to receive event notifications
            http_method: HTTP method for webhook delivery (default: "POST")
            auth_method: Authentication method ("none", "bearer", etc.)
            headers: Custom headers to include in webhook requests (e.g., Authorization header)
            event_filter: JSON object specifying event filters (e.g., {"user.uid": "bob"})

        Returns:
            Webhook registration details including webhook ID
        """
        data: Dict[str, Any] = {
            "httpMethod": http_method,
            "uri": uri,
            "event": event,
            "authMethod": auth_method,
        }

        if headers:
            data["headers"] = headers

        if event_filter:
            data["eventFilter"] = event_filter

        request_headers = self._get_webhook_headers()
        response = await self._make_request(
            "POST",
            "/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks",
            json=data,
            headers=request_headers,
        )
        return response.json()["ocs"]["data"]

    async def delete_webhook(self, webhook_id: int) -> None:
        """Delete a webhook registration.

        Args:
            webhook_id: ID of the webhook to delete
        """
        headers = self._get_webhook_headers()
        await self._make_request(
            "DELETE",
            f"/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks/{webhook_id}",
            headers=headers,
        )

    async def get_webhook(self, webhook_id: int) -> Dict[str, Any]:
        """Get details of a specific webhook registration.

        Args:
            webhook_id: ID of the webhook to retrieve

        Returns:
            Webhook registration details
        """
        headers = self._get_webhook_headers()
        response = await self._make_request(
            "GET",
            f"/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks/{webhook_id}",
            headers=headers,
        )
        return response.json()["ocs"]["data"]
