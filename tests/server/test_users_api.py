import pytest

from nextcloud_mcp_server.client import NextcloudClient


@pytest.mark.anyio
async def test_create_and_delete_user(nc_client: NextcloudClient, test_user):
    """Test creating a user and verifying deletion (cleanup by fixture)."""
    user_config = test_user

    # Create user
    await nc_client.users.create_user(**user_config)

    # Verify user exists
    users = await nc_client.users.search_users(search=user_config["userid"])
    assert user_config["userid"] in users

    user_details = await nc_client.users.get_user_details(user_config["userid"])
    assert user_details.id == user_config["userid"]
    assert user_details.displayname == user_config["display_name"]
    assert user_details.email == user_config["email"]

    # Test deletion explicitly as part of test functionality
    await nc_client.users.delete_user(user_config["userid"])

    # Verify user is deleted
    users = await nc_client.users.search_users(search=user_config["userid"])
    assert user_config["userid"] not in users
    # Note: Fixture cleanup will also try to delete but handle 404 gracefully


@pytest.mark.anyio
async def test_update_user_field(nc_client: NextcloudClient, test_user):
    """Test updating user fields."""
    user_config = test_user

    await nc_client.users.create_user(**user_config)

    new_email = f"new.{user_config['email']}"
    await nc_client.users.update_user_field(user_config["userid"], "email", new_email)

    user_details = await nc_client.users.get_user_details(user_config["userid"])
    assert user_details.email == new_email
    # Fixture will handle cleanup


@pytest.mark.anyio
async def test_user_groups(nc_client: NextcloudClient, test_user_in_group):
    """Test adding and removing users from groups."""
    user_config, groupid = test_user_in_group
    userid = user_config["userid"]

    # Verify user is in group
    groups = await nc_client.users.get_user_groups(userid)
    assert groupid in groups

    # Remove user from group
    await nc_client.users.remove_user_from_group(userid, groupid)
    groups = await nc_client.users.get_user_groups(userid)
    assert groupid not in groups
    # Fixtures will handle cleanup


@pytest.mark.anyio
async def test_user_subadmins(nc_client: NextcloudClient, test_user, test_group):
    """Test promoting and demoting subadmins."""
    user_config = test_user
    groupid = test_group
    userid = user_config["userid"]

    await nc_client.users.create_user(**user_config)

    # Promote to subadmin
    await nc_client.users.promote_user_to_subadmin(userid, groupid)
    subadmin_groups = await nc_client.users.get_user_subadmin_groups(userid)
    assert groupid in subadmin_groups

    # Demote from subadmin
    await nc_client.users.demote_user_from_subadmin(userid, groupid)
    subadmin_groups = await nc_client.users.get_user_subadmin_groups(userid)
    assert groupid not in subadmin_groups
    # Fixtures will handle cleanup


@pytest.mark.anyio
async def test_disable_enable_user(nc_client: NextcloudClient, test_user):
    """Test disabling and enabling users."""
    user_config = test_user
    userid = user_config["userid"]

    await nc_client.users.create_user(**user_config)

    # Disable user
    await nc_client.users.disable_user(userid)
    user_details = await nc_client.users.get_user_details(userid)
    assert not user_details.enabled

    # Enable user
    await nc_client.users.enable_user(userid)
    user_details = await nc_client.users.get_user_details(userid)
    assert user_details.enabled
    # Fixture will handle cleanup


@pytest.mark.anyio
async def test_get_editable_user_fields(nc_client: NextcloudClient):
    editable_fields = await nc_client.users.get_editable_user_fields()
    assert "displayname" in editable_fields
    assert "email" in editable_fields
