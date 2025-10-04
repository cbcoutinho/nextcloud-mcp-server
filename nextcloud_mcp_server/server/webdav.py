import logging

from mcp.server.fastmcp import Context, FastMCP

from nextcloud_mcp_server.client import NextcloudClient
from nextcloud_mcp_server.utils.document_parser import is_parseable_document, parse_document
from nextcloud_mcp_server.config import is_unstructured_parsing_enabled

logger = logging.getLogger(__name__)


def configure_webdav_tools(mcp: FastMCP):
    # WebDAV file system tools
    @mcp.tool()
    async def nc_webdav_list_directory(ctx: Context, path: str = ""):
        """List files and directories in the specified NextCloud path.

        Args:
            path: Directory path to list (empty string for root directory)

        Returns:
            List of items with metadata including name, path, is_directory, size, content_type, last_modified

        Examples:
            # List root directory
            await nc_webdav_list_directory("")

            # List a specific folder
            await nc_webdav_list_directory("Documents/Projects")
        """
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.webdav.list_directory(path)

    @mcp.tool()
    async def nc_webdav_read_file(path: str, ctx: Context):
        """Read the content of a file from NextCloud.

        Args:
            path: Full path to the file to read

        Returns:
            Dict with path, content, content_type, size, and optional parsing metadata
            - Text files are decoded to UTF-8
            - Documents (PDF, DOCX, etc.) are parsed and text is extracted
            - Other binary files are base64 encoded

        Examples:
            # Read a text file
            result = await nc_webdav_read_file("Documents/readme.txt")
            logger.info(result['content'])  # Decoded text content

            # Read a PDF document (automatically parsed)
            result = await nc_webdav_read_file("Documents/report.pdf")
            logger.info(result['content'])  # Extracted text from PDF
            logger.info(result['parsing_metadata'])  # Document parsing info

            # Read a binary file
            result = await nc_webdav_read_file("Images/photo.jpg")
            logger.info(result['encoding'])  # 'base64'
        """
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        content, content_type = await client.webdav.read_file(path)

        # Check if this is a parseable document (PDF, DOCX, etc.)
        if (is_unstructured_parsing_enabled() and is_parseable_document(content_type)):
            try:
                logger.info(f"Parsing document '{path}' of type '{content_type}'")
                parsed_text, metadata = await parse_document(
                    content, content_type, filename=path
                )
                return {
                    "path": path,
                    "content": parsed_text,
                    "content_type": content_type,
                    "size": len(content),
                    "parsed": True,
                    "parsing_metadata": metadata,
                }
            except Exception as e:
                logger.warning(
                    f"Failed to parse document '{path}', falling back to base64: {e}"
                )
                # Fall through to base64 encoding on parse failure

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

        Examples:
            # Write a text file
            await nc_webdav_write_file("Documents/notes.md", "# My Notes\nContent here...")

            # Write binary data (base64 encoded)
            await nc_webdav_write_file("files/data.bin", base64_content, "application/octet-stream;base64")
        """
        client: NextcloudClient = ctx.request_context.lifespan_context.client

        # Handle base64 encoded content
        if content_type and "base64" in content_type.lower():
            import base64

            content_bytes = base64.b64decode(content)
            content_type = content_type.replace(";base64", "")
        else:
            content_bytes = content.encode("utf-8")

        return await client.webdav.write_file(path, content_bytes, content_type)

    @mcp.tool()
    async def nc_webdav_create_directory(path: str, ctx: Context):
        """Create a directory in NextCloud.

        Args:
            path: Full path of the directory to create

        Returns:
            Dict with status_code (201 for created, 405 if already exists)

        Examples:
            # Create a single directory
            await nc_webdav_create_directory("NewProject")

            # Create nested directories (parent must exist)
            await nc_webdav_create_directory("Projects/MyApp/docs")
        """
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.webdav.create_directory(path)

    @mcp.tool()
    async def nc_webdav_delete_resource(path: str, ctx: Context):
        """Delete a file or directory in NextCloud.

        Args:
            path: Full path of the file or directory to delete

        Returns:
            Dict with status_code indicating result (404 if not found)

        Examples:
            # Delete a file
            await nc_webdav_delete_resource("old_document.txt")

            # Delete a directory (will delete all contents)
            await nc_webdav_delete_resource("temp_folder")
        """
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.webdav.delete_resource(path)

    @mcp.tool()
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

        Examples:
            # Rename a file
            await nc_webdav_move_resource("document.txt", "new_name.txt")

            # Move a file to another directory
            await nc_webdav_move_resource("document.txt", "Archive/document.txt")

            # Move a directory
            await nc_webdav_move_resource("Projects/OldProject", "Projects/NewProject")

            # Move and overwrite if destination exists
            await nc_webdav_move_resource("document.txt", "Archive/document.txt", overwrite=True)
        """
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.webdav.move_resource(
            source_path, destination_path, overwrite
        )

    @mcp.tool()
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

        Examples:
            # Copy a file
            await nc_webdav_copy_resource("document.txt", "document_copy.txt")

            # Copy a file to another directory
            await nc_webdav_copy_resource("document.txt", "Backup/document.txt")

            # Copy a directory
            await nc_webdav_copy_resource("Projects/ProjectA", "Projects/ProjectA_Backup")

            # Copy and overwrite if destination exists
            await nc_webdav_copy_resource("document.txt", "Backup/document.txt", overwrite=True)
        """
        client: NextcloudClient = ctx.request_context.lifespan_context.client
        return await client.webdav.copy_resource(
            source_path, destination_path, overwrite
        )
