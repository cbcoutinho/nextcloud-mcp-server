"""MCP tools for Nextcloud News app."""

import logging

from httpx import HTTPStatusError, RequestError
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.client.news import NewsItemType
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.news import (
    FeedHealthResponse,
    GetItemResponse,
    GetStatusResponse,
    ListFeedsResponse,
    ListFoldersResponse,
    ListItemsResponse,
    NewsFeed,
    NewsFolder,
    NewsItem,
    NewsItemSummary,
)
from nextcloud_mcp_server.observability.metrics import instrument_tool

logger = logging.getLogger(__name__)


def configure_news_tools(mcp: FastMCP):
    """Configure News app MCP tools."""

    @mcp.tool()
    @require_scopes("news:read")
    @instrument_tool
    async def nc_news_list_folders(ctx: Context) -> ListFoldersResponse:
        """List all News folders (requires news:read scope)."""
        client = await get_client(ctx)
        try:
            folders_data = await client.news.get_folders()
            folders = [NewsFolder(**f) for f in folders_data]
            return ListFoldersResponse(results=folders, total_count=len(folders))
        except RequestError as e:
            raise McpError(
                ErrorData(code=-1, message=f"Network error listing folders: {str(e)}")
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to list folders: {e.response.status_code}",
                )
            )

    @mcp.tool()
    @require_scopes("news:read")
    @instrument_tool
    async def nc_news_list_feeds(ctx: Context) -> ListFeedsResponse:
        """List all News feeds with metadata (requires news:read scope).

        Returns feeds with unread counts, error status, and overall starred count.
        """
        client = await get_client(ctx)
        try:
            data = await client.news.get_feeds()
            feeds = [NewsFeed(**f) for f in data.get("feeds", [])]
            return ListFeedsResponse(
                results=feeds,
                starred_count=data.get("starredCount", 0),
                newest_item_id=data.get("newestItemId"),
                total_count=len(feeds),
            )
        except RequestError as e:
            raise McpError(
                ErrorData(code=-1, message=f"Network error listing feeds: {str(e)}")
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to list feeds: {e.response.status_code}",
                )
            )

    @mcp.tool()
    @require_scopes("news:read")
    @instrument_tool
    async def nc_news_list_items(
        ctx: Context,
        feed_id: int | None = None,
        folder_id: int | None = None,
        starred_only: bool = False,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> ListItemsResponse:
        """List News items (articles) with optional filtering (requires news:read scope).

        Args:
            feed_id: Filter by specific feed ID
            folder_id: Filter by specific folder ID
            starred_only: Return only starred items
            unread_only: Return only unread items
            limit: Maximum number of items to return (default 50, -1 for all)
            offset: Item ID to start after (for pagination)

        Returns:
            ListItemsResponse with items, count, and pagination info
        """
        client = await get_client(ctx)

        # Determine item type filter
        type_ = NewsItemType.ALL
        id_ = 0
        if starred_only:
            type_ = NewsItemType.STARRED
        elif feed_id is not None:
            type_ = NewsItemType.FEED
            id_ = feed_id
        elif folder_id is not None:
            type_ = NewsItemType.FOLDER
            id_ = folder_id

        try:
            items_data = await client.news.get_items(
                batch_size=limit,
                offset=offset,
                type_=type_,
                id_=id_,
                get_read=not unread_only,
            )
            items = [NewsItemSummary(**i) for i in items_data]

            # Determine pagination info
            oldest_id = min((i.id for i in items), default=None) if items else None
            has_more = len(items) == limit and limit > 0

            return ListItemsResponse(
                results=items,
                total_count=len(items),
                has_more=has_more,
                oldest_id=oldest_id,
            )
        except RequestError as e:
            raise McpError(
                ErrorData(code=-1, message=f"Network error listing items: {str(e)}")
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to list items: {e.response.status_code}",
                )
            )

    @mcp.tool()
    @require_scopes("news:read")
    @instrument_tool
    async def nc_news_get_item(item_id: int, ctx: Context) -> GetItemResponse:
        """Get a specific News item by ID with full content (requires news:read scope).

        Args:
            item_id: Item ID

        Returns:
            GetItemResponse with full item details including HTML body
        """
        client = await get_client(ctx)
        try:
            item_data = await client.news.get_item(item_id)
            item = NewsItem(**item_data)
            return GetItemResponse(item=item)
        except ValueError as e:
            raise McpError(ErrorData(code=-1, message=str(e)))
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1, message=f"Network error getting item {item_id}: {str(e)}"
                )
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(ErrorData(code=-1, message=f"Item {item_id} not found"))
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to get item {item_id}: {e.response.status_code}",
                )
            )

    @mcp.tool()
    @require_scopes("news:read")
    @instrument_tool
    async def nc_news_get_starred_items(
        ctx: Context, limit: int = 50, offset: int = 0
    ) -> ListItemsResponse:
        """Get starred (favorited) News items (requires news:read scope).

        Convenience method for retrieving user's starred articles.

        Args:
            limit: Maximum number of items to return (default 50, -1 for all)
            offset: Item ID to start after (for pagination)

        Returns:
            ListItemsResponse with starred items
        """
        client = await get_client(ctx)
        try:
            items_data = await client.news.get_items(
                batch_size=limit,
                offset=offset,
                type_=NewsItemType.STARRED,
                get_read=True,  # Include read starred items
            )
            items = [NewsItemSummary(**i) for i in items_data]

            oldest_id = min((i.id for i in items), default=None) if items else None
            has_more = len(items) == limit and limit > 0

            return ListItemsResponse(
                results=items,
                total_count=len(items),
                has_more=has_more,
                oldest_id=oldest_id,
            )
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1, message=f"Network error getting starred items: {str(e)}"
                )
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to get starred items: {e.response.status_code}",
                )
            )

    @mcp.tool()
    @require_scopes("news:read")
    @instrument_tool
    async def nc_news_get_unread_items(
        ctx: Context, limit: int = 50, offset: int = 0
    ) -> ListItemsResponse:
        """Get unread News items (requires news:read scope).

        Convenience method for retrieving unread articles across all feeds.

        Args:
            limit: Maximum number of items to return (default 50, -1 for all)
            offset: Item ID to start after (for pagination)

        Returns:
            ListItemsResponse with unread items
        """
        client = await get_client(ctx)
        try:
            items_data = await client.news.get_items(
                batch_size=limit,
                offset=offset,
                type_=NewsItemType.ALL,
                get_read=False,  # Only unread items
            )
            items = [NewsItemSummary(**i) for i in items_data]

            oldest_id = min((i.id for i in items), default=None) if items else None
            has_more = len(items) == limit and limit > 0

            return ListItemsResponse(
                results=items,
                total_count=len(items),
                has_more=has_more,
                oldest_id=oldest_id,
            )
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1, message=f"Network error getting unread items: {str(e)}"
                )
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to get unread items: {e.response.status_code}",
                )
            )

    @mcp.tool()
    @require_scopes("news:read")
    @instrument_tool
    async def nc_news_get_feed_health(feed_id: int, ctx: Context) -> FeedHealthResponse:
        """Get health status for a specific feed (requires news:read scope).

        Returns error count and last error message if the feed has update issues.

        Args:
            feed_id: Feed ID to check

        Returns:
            FeedHealthResponse with error status
        """
        client = await get_client(ctx)
        try:
            data = await client.news.get_feeds()
            for feed_data in data.get("feeds", []):
                if feed_data.get("id") == feed_id:
                    feed = NewsFeed(**feed_data)
                    return FeedHealthResponse(
                        feed_id=feed.id,
                        title=feed.title,
                        url=feed.url,
                        has_errors=feed.has_errors,
                        error_count=feed.update_error_count,
                        last_error=feed.last_update_error,
                    )
            raise McpError(ErrorData(code=-1, message=f"Feed {feed_id} not found"))
        except RequestError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Network error getting feed health: {str(e)}",
                )
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to get feed health: {e.response.status_code}",
                )
            )

    @mcp.tool()
    @require_scopes("news:read")
    @instrument_tool
    async def nc_news_get_status(ctx: Context) -> GetStatusResponse:
        """Get News app status and version (requires news:read scope).

        Returns version information and any configuration warnings.
        """
        client = await get_client(ctx)
        try:
            status_data = await client.news.get_status()
            return GetStatusResponse(
                version=status_data.get("version", "unknown"),
                warnings=status_data.get("warnings", {}),
            )
        except RequestError as e:
            raise McpError(
                ErrorData(code=-1, message=f"Network error getting status: {str(e)}")
            )
        except HTTPStatusError as e:
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Failed to get status: {e.response.status_code}",
                )
            )
