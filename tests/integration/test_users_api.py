import pytest
from nextcloud_mcp_server.client import NextcloudClient


@pytest.mark.asyncio
async def test_create_and_delete_user(nc_client: NextcloudClient):
    userid = "testuser1"
    password = "testpassword1"
    display_name = "Test User One"
    email = "test1@example.com"

    # Create user
    await nc_client.users.create_user(
        userid=userid,
        password=password,
        display_name=display_name,
        email=email,
    )

    # Verify user exists
    users = await nc_client.users.search_users(search=userid)
    assert userid in users

    user_details = await nc_client.users.get_user_details(userid)
    assert user_details.id == userid
    assert user_details.displayname == display_name
    assert user_details.email == email

    # Delete user
    await nc_client.users.delete_user(userid)

    # Verify user is deleted
    users = await nc_client.users.search_users(search=userid)
    assert userid not in users


@pytest.mark.asyncio
async def test_update_user_field(nc_client: NextcloudClient):
    userid = "testuser2"
    password = "testpassword2"
    display_name = "Test User Two"
    email = "test2@example.com"

    await nc_client.users.create_user(
        userid=userid,
        password=password,
        display_name=display_name,
        email=email,
    )

    new_email = "new.test2@example.com"
    await nc_client.users.update_user_field(userid, "email", new_email)

    user_details = await nc_client.users.get_user_details(userid)
    assert user_details.email == new_email

    await nc_client.users.delete_user(userid)


@pytest.mark.asyncio
async def test_user_groups(nc_client: NextcloudClient):
    userid = "testuser3"
    password = "testpassword3"
    groupid = "testgroup"

    await nc_client.users.create_user(userid=userid, password=password)

    # Add user to group
    await nc_client.users.add_user_to_group(userid, groupid)
    groups = await nc_client.users.get_user_groups(userid)
    assert groupid in groups

    # Remove user from group
    await nc_client.users.remove_user_from_group(userid, groupid)
    groups = await nc_client.users.get_user_groups(userid)
    assert groupid not in groups

    await nc_client.users.delete_user(userid)


@pytest.mark.asyncio
async def test_user_subadmins(nc_client: NextcloudClient):
    userid = "testuser4"
    password = "testpassword4"
    groupid = "subadmingroup"

    await nc_client.users.create_user(userid=userid, password=password)

    # Promote to subadmin
    await nc_client.users.promote_user_to_subadmin(userid, groupid)
    subadmin_groups = await nc_client.users.get_user_subadmin_groups(userid)
    assert groupid in subadmin_groups

    # Demote from subadmin
    await nc_client.users.demote_user_from_subadmin(userid, groupid)
    subadmin_groups = await nc_client.users.get_user_subadmin_groups(userid)
    assert groupid not in subadmin_groups

    await nc_client.users.delete_user(userid)


@pytest.mark.asyncio
async def test_disable_enable_user(nc_client: NextcloudClient):
    userid = "testuser5"
    password = "testpassword5"

    await nc_client.users.create_user(userid=userid, password=password)

    # Disable user
    await nc_client.users.disable_user(userid)
    user_details = await nc_client.users.get_user_details(userid)
    assert not user_details.enabled

    # Enable user
    await nc_client.users.enable_user(userid)
    user_details = await nc_client.users.get_user_details(userid)
    assert user_details.enabled

    await nc_client.users.delete_user(userid)


@pytest.mark.asyncio
async def test_get_editable_user_fields(nc_client: NextcloudClient):
    editable_fields = await nc_client.users.get_editable_user_fields()
    assert "displayname" in editable_fields
    assert "email" in editable_fields
