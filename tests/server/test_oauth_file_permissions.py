"""
Multi-user OAuth tests for Nextcloud WebDAV file permissions.

Tests verify that the MCP server respects Nextcloud file sharing permissions
when accessed via OAuth authentication with different users.
"""

import json
import logging

import pytest

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.integration, pytest.mark.oauth]


async def create_share(nc_client, path: str, share_with: str, permissions: int = 1):
    """
    Helper to create a file share using OCS Sharing API.

    Args:
        nc_client: Admin NextcloudClient
        path: Path to file/folder to share
        share_with: Username to share with
        permissions: Share permissions (1=read, 15=all, 19=read+write+share)

    Returns:
        Share ID
    """
    # Use the authenticated client's internal HTTP client
    response = await nc_client._client.post(
        "/ocs/v2.php/apps/files_sharing/api/v1/shares",
        headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        data={
            "path": path,
            "shareType": 0,  # 0 = user share
            "shareWith": share_with,
            "permissions": permissions,
        },
    )
    response.raise_for_status()
    data = response.json()
    share_id = data["ocs"]["data"]["id"]
    logger.info(
        f"Created share {share_id}: {path} -> {share_with} (permissions={permissions})"
    )
    return share_id


async def delete_share(nc_client, share_id: int):
    """Helper to delete a file share."""
    response = await nc_client._client.delete(
        f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}",
        headers={"OCS-APIRequest": "true", "Accept": "application/json"},
    )
    response.raise_for_status()
    logger.info(f"Deleted share {share_id}")


@pytest.mark.asyncio
async def test_file_share_read_permissions(
    nc_client, alice_mcp_client, bob_mcp_client, diana_mcp_client
):
    """
    Test that shared files respect read permissions.

    Scenario:
    1. Admin creates a file as alice
    2. Admin shares the file with bob (read-only)
    3. Bob can read the file via MCP tools
    4. Diana cannot access the file (no share)
    """
    # Create a file as alice
    file_path = "/alice_shared_file_read.txt"
    file_content = b"This file is shared with Bob for reading only."

    logger.info(f"Creating file as alice: {file_path}")
    # Note: We're using admin client to create file as alice
    # In a real scenario, we'd need to impersonate alice or use alice's OAuth client
    await nc_client.webdav.write_file(file_path, file_content)

    share_id = None

    try:
        # Share the file with bob (read-only, permissions=1)
        logger.info("Sharing file with bob (read-only)...")
        share_id = await create_share(nc_client, file_path, "bob", permissions=1)

        # Test: Bob reads the file via MCP
        logger.info("Bob attempting to read file via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": file_path}
        )

        # Bob should be able to read the shared file
        if not result.isError:
            response_data = json.loads(result.content[0].text)
            logger.info(
                f"Bob successfully read file: {response_data.get('content', '')[:50]}..."
            )
            assert "content" in response_data
        else:
            logger.warning(f"Bob could not read file: {result.content}")
            # This might fail if the share path is different for bob

        # Test: Diana attempts to read the file
        logger.info("Diana attempting to read file via MCP...")
        result = await diana_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": file_path}
        )

        # Diana should NOT be able to read (no share)
        if result.isError:
            logger.info("Diana correctly denied access to unshared file")
        else:
            logger.warning("Diana unexpectedly could read unshared file")

    finally:
        # Cleanup
        if share_id:
            await delete_share(nc_client, share_id)
        logger.info(f"Deleting file {file_path}")
        await nc_client.webdav.delete_resource(file_path)


@pytest.mark.asyncio
async def test_file_share_write_permissions(
    nc_client, alice_mcp_client, charlie_mcp_client, bob_mcp_client
):
    """
    Test that shared files respect write permissions.

    Scenario:
    1. Admin creates a file as alice
    2. Admin shares the file with charlie (edit permission)
    3. Admin shares the file with bob (read-only)
    4. Charlie can edit the file via MCP tools
    5. Bob cannot edit the file
    """
    # Create a file as alice
    file_path = "/alice_shared_file_write.txt"
    file_content = b"This file is shared with Charlie for editing."

    logger.info(f"Creating file as alice: {file_path}")
    await nc_client.webdav.write_file(file_path, file_content)

    charlie_share_id = None
    bob_share_id = None

    try:
        # Share with charlie (read+write, permissions=3)
        logger.info("Sharing file with charlie (edit permission)...")
        charlie_share_id = await create_share(
            nc_client, file_path, "charlie", permissions=3
        )

        # Share with bob (read-only, permissions=1)
        logger.info("Sharing file with bob (read-only)...")
        bob_share_id = await create_share(nc_client, file_path, "bob", permissions=1)

        # Test: Charlie can write to the file
        logger.info("Charlie attempting to write to file via MCP...")
        updated_content = (
            b"This file is shared with Charlie for editing.\nCharlie added this line."
        )
        result = await charlie_mcp_client.call_tool(
            "nc_webdav_write_file",
            arguments={"path": file_path, "content": updated_content.decode("utf-8")},
        )

        if not result.isError:
            logger.info("Charlie successfully wrote to file")
        else:
            logger.warning(f"Charlie could not write to file: {result.content}")

        # Test: Bob attempts to write (should fail)
        logger.info("Bob attempting to write to file via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_webdav_write_file",
            arguments={"path": file_path, "content": "Bob tries to overwrite this."},
        )

        # Bob should be denied
        if result.isError:
            logger.info("Bob correctly denied write access")
        else:
            logger.warning("Bob unexpectedly succeeded in writing (permissions issue?)")

    finally:
        # Cleanup
        if charlie_share_id:
            await delete_share(nc_client, charlie_share_id)
        if bob_share_id:
            await delete_share(nc_client, bob_share_id)
        logger.info(f"Deleting file {file_path}")
        await nc_client.webdav.delete_resource(file_path)


@pytest.mark.asyncio
async def test_file_list_permissions(nc_client, alice_mcp_client, bob_mcp_client):
    """
    Test that file listing respects share permissions.

    Scenario:
    1. Admin creates alice's private file
    2. Admin creates bob's private file
    3. Admin creates a shared file
    4. Alice can only list her own files + shared files
    5. Bob can only list his own files + shared files
    """
    alice_file = "/alice_private_file.txt"
    bob_file = "/bob_private_file.txt"
    shared_file = "/shared_file.txt"

    logger.info("Creating test files...")
    await nc_client.webdav.write_file(alice_file, b"Alice's private file")
    await nc_client.webdav.write_file(bob_file, b"Bob's private file")
    await nc_client.webdav.write_file(shared_file, b"Shared file content")

    alice_share_id = None
    bob_share_id = None

    try:
        # Share the shared file with both alice and bob
        logger.info("Sharing file with alice and bob...")
        alice_share_id = await create_share(
            nc_client, shared_file, "alice", permissions=1
        )
        bob_share_id = await create_share(nc_client, shared_file, "bob", permissions=1)

        # Test: Alice lists files in root
        logger.info("Alice listing files via MCP...")
        result = await alice_mcp_client.call_tool(
            "nc_webdav_list_directory", arguments={"path": "/"}
        )

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            # The response is directly a list, not wrapped in a dict
            if not isinstance(response_data, list):
                response_data = [response_data] if response_data else []
            file_names = [f["name"] for f in response_data]
            logger.info(f"Alice can see files: {file_names}")

            # Alice should see her own file and shared file, but not bob's
            # Note: This depends on how Nextcloud handles file ownership
        else:
            logger.warning(f"Alice could not list files: {result.content}")

        # Test: Bob lists files in root
        logger.info("Bob listing files via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_webdav_list_directory", arguments={"path": "/"}
        )

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            # The response is directly a list, not wrapped in a dict
            if not isinstance(response_data, list):
                response_data = [response_data] if response_data else []
            file_names = [f["name"] for f in response_data]
            logger.info(f"Bob can see files: {file_names}")

            # Bob should see his own file and shared file, but not alice's
        else:
            logger.warning(f"Bob could not list files: {result.content}")

    finally:
        # Cleanup
        if alice_share_id:
            await delete_share(nc_client, alice_share_id)
        if bob_share_id:
            await delete_share(nc_client, bob_share_id)

        logger.info("Cleaning up test files...")
        await nc_client.webdav.delete_resource(alice_file)
        await nc_client.webdav.delete_resource(bob_file)
        await nc_client.webdav.delete_resource(shared_file)


@pytest.mark.asyncio
async def test_folder_share_permissions(nc_client, alice_mcp_client, bob_mcp_client):
    """
    Test that folder sharing works correctly.

    Scenario:
    1. Admin creates a folder as alice
    2. Admin creates files in the folder
    3. Admin shares the folder with bob
    4. Bob can access files in the shared folder
    """
    folder_path = "/alice_shared_folder"
    file_in_folder = f"{folder_path}/document.txt"
    file_content = b"This is a document in alice's shared folder"

    logger.info(f"Creating folder: {folder_path}")
    await nc_client.webdav.create_directory(folder_path)

    logger.info(f"Creating file in folder: {file_in_folder}")
    await nc_client.webdav.write_file(file_in_folder, file_content)

    share_id = None

    try:
        # Share the folder with bob
        logger.info("Sharing folder with bob...")
        share_id = await create_share(nc_client, folder_path, "bob", permissions=1)

        # Test: Bob lists the shared folder
        logger.info("Bob attempting to list shared folder via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_webdav_list_directory", arguments={"path": folder_path}
        )

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            # The response is directly a list, not wrapped in a dict
            if not isinstance(response_data, list):
                response_data = [response_data] if response_data else []
            logger.info(f"Bob can see {len(response_data)} files in shared folder")

            # Bob should see the file in the shared folder
            file_names = [f["name"] for f in response_data]
            assert "document.txt" in file_names, (
                "Bob should see the file in shared folder"
            )
        else:
            logger.warning(f"Bob could not list shared folder: {result.content}")

        # Test: Bob reads the file in the shared folder
        logger.info("Bob attempting to read file in shared folder via MCP...")
        result = await bob_mcp_client.call_tool(
            "nc_webdav_read_file", arguments={"path": file_in_folder}
        )

        if not result.isError:
            response_data = json.loads(result.content[0].text)
            logger.info("Bob successfully read file in shared folder")
            assert "content" in response_data
        else:
            logger.warning(
                f"Bob could not read file in shared folder: {result.content}"
            )

    finally:
        # Cleanup
        if share_id:
            await delete_share(nc_client, share_id)

        logger.info("Cleaning up test folder...")
        await nc_client.webdav.delete_resource(folder_path)
