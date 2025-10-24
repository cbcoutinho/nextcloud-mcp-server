import logging

from mcp.server.fastmcp import Context, FastMCP

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models import DirectoryListing, FileInfo, SearchFilesResponse

logger = logging.getLogger(__name__)


def configure_webdav_tools(mcp: FastMCP):
    # WebDAV file system tools
    @mcp.tool()
    @require_scopes("files:read")
    async def nc_webdav_list_directory(
        ctx: Context, path: str = ""
    ) -> DirectoryListing:
        """List files and directories in the specified NextCloud path.

        Args:
            path: Directory path to list (empty string for root directory)

        Returns:
            DirectoryListing with files, total_count, directories_count, files_count, and total_size
        """
        client = get_client(ctx)
        items = await client.webdav.list_directory(path)

        # Convert to FileInfo models
        file_infos = [FileInfo(**item) for item in items]

        # Calculate metadata
        directories_count = sum(1 for f in file_infos if f.is_directory)
        files_count = sum(1 for f in file_infos if not f.is_directory)
        total_size = sum(f.size or 0 for f in file_infos if not f.is_directory)

        return DirectoryListing(
            path=path,
            files=file_infos,
            total_count=len(file_infos),
            directories_count=directories_count,
            files_count=files_count,
            total_size=total_size,
        )

    @mcp.tool()
    @require_scopes("files:read")
    async def nc_webdav_read_file(path: str, ctx: Context):
        """Read the content of a file from NextCloud.

        Args:
            path: Full path to the file to read

        Returns:
            Dict with path, content, content_type, size, and encoding (if binary)
            Text files are decoded to UTF-8, binary files are base64 encoded
        """
        client = get_client(ctx)
        content, content_type = await client.webdav.read_file(path)

        # For text files, decode content for easier viewing
        if content_type and content_type.startswith("text/"):
            try:
                decoded_content = content.decode("utf-8")
                return {
                    "path": path,
                    "content": decoded_content,
                    "content_type": content_type,
                    "size": len(content),
                }
            except UnicodeDecodeError:
                pass

        # For binary files, return metadata and base64 encoded content
        import base64

        return {
            "path": path,
            "content": base64.b64encode(content).decode("ascii"),
            "content_type": content_type,
            "size": len(content),
            "encoding": "base64",
        }

    @mcp.tool()
    @require_scopes("files:write")
    async def nc_webdav_write_file(
        path: str, content: str, ctx: Context, content_type: str | None = None
    ):
        """Write content to a file in NextCloud.

        Args:
            path: Full path where to write the file
            content: File content (text or base64 for binary)
            content_type: MIME type (auto-detected if not provided, use 'type;base64' for binary)

        Returns:
            Dict with status_code indicating success
        """
        client = get_client(ctx)

        # Handle base64 encoded content
        if content_type and "base64" in content_type.lower():
            import base64

            content_bytes = base64.b64decode(content)
            content_type = content_type.replace(";base64", "")
        else:
            content_bytes = content.encode("utf-8")

        return await client.webdav.write_file(path, content_bytes, content_type)

    @mcp.tool()
    @require_scopes("files:write")
    async def nc_webdav_create_directory(path: str, ctx: Context):
        """Create a directory in NextCloud.

        Args:
            path: Full path of the directory to create

        Returns:
            Dict with status_code (201 for created, 405 if already exists)
        """
        client = get_client(ctx)
        return await client.webdav.create_directory(path)

    @mcp.tool()
    @require_scopes("files:write")
    async def nc_webdav_delete_resource(path: str, ctx: Context):
        """Delete a file or directory in NextCloud.

        Args:
            path: Full path of the file or directory to delete

        Returns:
            Dict with status_code indicating result (404 if not found)
        """
        client = get_client(ctx)
        return await client.webdav.delete_resource(path)

    @mcp.tool()
    @require_scopes("files:write")
    async def nc_webdav_move_resource(
        source_path: str, destination_path: str, ctx: Context, overwrite: bool = False
    ):
        """Move or rename a file or directory in NextCloud.

        Args:
            source_path: Full path of the file or directory to move
            destination_path: New path for the file or directory
            overwrite: Whether to overwrite the destination if it exists (default: False)

        Returns:
            Dict with status_code indicating result (404 if source not found, 412 if destination exists and overwrite is False)
        """
        client = get_client(ctx)
        return await client.webdav.move_resource(
            source_path, destination_path, overwrite
        )

    @mcp.tool()
    @require_scopes("files:write")
    async def nc_webdav_copy_resource(
        source_path: str, destination_path: str, ctx: Context, overwrite: bool = False
    ):
        """Copy a file or directory in NextCloud.

        Args:
            source_path: Full path of the file or directory to copy
            destination_path: Destination path for the copy
            overwrite: Whether to overwrite the destination if it exists (default: False)

        Returns:
            Dict with status_code indicating result (404 if source not found, 412 if destination exists and overwrite is False)
        """
        client = get_client(ctx)
        return await client.webdav.copy_resource(
            source_path, destination_path, overwrite
        )

    @mcp.tool()
    @require_scopes("files:read")
    async def nc_webdav_search_files(
        ctx: Context,
        scope: str = "",
        name_pattern: str | None = None,
        mime_type: str | None = None,
        only_favorites: bool = False,
        limit: int | None = None,
    ) -> SearchFilesResponse:
        """Search for files in NextCloud using WebDAV SEARCH.

        This is a high-level search tool that supports common search patterns.
        For more complex queries, use the specific search tools.

        Args:
            scope: Directory path to search in (empty string for user root)
            name_pattern: File name pattern (supports % wildcard, e.g., "%.txt" for all text files)
            mime_type: MIME type to filter by (supports % wildcard, e.g., "image/%" for all images)
            only_favorites: If True, only return favorited files
            limit: Maximum number of results to return

        Returns:
            SearchFilesResponse with list of matching files
        """
        client = get_client(ctx)

        # Build where conditions based on filters
        conditions = []

        if name_pattern:
            conditions.append(
                f"""
                <d:like>
                    <d:prop>
                        <d:displayname/>
                    </d:prop>
                    <d:literal>{name_pattern}</d:literal>
                </d:like>
            """
            )

        if mime_type:
            conditions.append(
                f"""
                <d:like>
                    <d:prop>
                        <d:getcontenttype/>
                    </d:prop>
                    <d:literal>{mime_type}</d:literal>
                </d:like>
            """
            )

        if only_favorites:
            conditions.append(
                """
                <d:eq>
                    <d:prop>
                        <oc:favorite/>
                    </d:prop>
                    <d:literal>1</d:literal>
                </d:eq>
            """
            )

        # Combine conditions with AND if multiple
        if len(conditions) > 1:
            where_conditions = f"""
                <d:and>
                    {"".join(conditions)}
                </d:and>
            """
        elif len(conditions) == 1:
            where_conditions = conditions[0]
        else:
            where_conditions = None

        # Include extended properties
        properties = [
            "displayname",
            "getcontentlength",
            "getcontenttype",
            "getlastmodified",
            "resourcetype",
            "getetag",
            "fileid",
            "favorite",
        ]

        results = await client.webdav.search_files(
            scope=scope,
            where_conditions=where_conditions,
            properties=properties,
            limit=limit,
        )

        # Convert to FileInfo models
        file_infos = [FileInfo(**result) for result in results]

        # Build filters applied dict
        filters = {}
        if name_pattern:
            filters["name_pattern"] = name_pattern
        if mime_type:
            filters["mime_type"] = mime_type
        if only_favorites:
            filters["only_favorites"] = True

        return SearchFilesResponse(
            results=file_infos,
            total_found=len(file_infos),
            scope=scope,
            filters_applied=filters if filters else None,
        )

    @mcp.tool()
    @require_scopes("files:read")
    async def nc_webdav_find_by_name(
        pattern: str, ctx: Context, scope: str = "", limit: int | None = None
    ) -> SearchFilesResponse:
        """Find files by name pattern in NextCloud.

        Args:
            pattern: Name pattern to search for (supports % wildcard)
            scope: Directory path to search in (empty string for user root)
            limit: Maximum number of results to return

        Returns:
            SearchFilesResponse with list of matching files
        """
        client = get_client(ctx)
        results = await client.webdav.find_by_name(
            pattern=pattern, scope=scope, limit=limit
        )
        file_infos = [FileInfo(**result) for result in results]
        return SearchFilesResponse(
            results=file_infos,
            total_found=len(file_infos),
            scope=scope,
            filters_applied={"name_pattern": pattern},
        )

    @mcp.tool()
    @require_scopes("files:read")
    async def nc_webdav_find_by_type(
        mime_type: str, ctx: Context, scope: str = "", limit: int | None = None
    ) -> SearchFilesResponse:
        """Find files by MIME type in NextCloud.

        Args:
            mime_type: MIME type to search for (supports % wildcard)
            scope: Directory path to search in (empty string for user root)
            limit: Maximum number of results to return

        Returns:
            SearchFilesResponse with list of matching files
        """
        client = get_client(ctx)
        results = await client.webdav.find_by_type(
            mime_type=mime_type, scope=scope, limit=limit
        )
        file_infos = [FileInfo(**result) for result in results]
        return SearchFilesResponse(
            results=file_infos,
            total_found=len(file_infos),
            scope=scope,
            filters_applied={"mime_type": mime_type},
        )

    @mcp.tool()
    @require_scopes("files:read")
    async def nc_webdav_list_favorites(
        ctx: Context, scope: str = "", limit: int | None = None
    ) -> SearchFilesResponse:
        """List all favorite files in NextCloud.

        Args:
            scope: Directory path to search in (empty string for all favorites)
            limit: Maximum number of results to return

        Returns:
            SearchFilesResponse with list of favorite files
        """
        client = get_client(ctx)
        results = await client.webdav.list_favorites(scope=scope, limit=limit)
        file_infos = [FileInfo(**result) for result in results]
        return SearchFilesResponse(
            results=file_infos,
            total_found=len(file_infos),
            scope=scope,
            filters_applied={"only_favorites": True},
        )
