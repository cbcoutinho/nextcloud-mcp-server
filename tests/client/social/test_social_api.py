"""Mocked unit tests for the Nextcloud Social client.

The mock sits on the shared ``httpx.AsyncClient``, below ``_make_request``, so
each test sees the request exactly as it leaves the process: the ``/index.php``
entry point ``_resolve_url`` adds, the ``OCS-APIRequest`` header Social needs to
accept a Basic-auth caller, and the ``id[]`` / ``types[]`` array parameters PHP
expects.
"""

import httpx
import pytest

from nextcloud_mcp_server.client.social import (
    SocialClient,
    validate_account,
    validate_hashtag,
    validate_status_id,
)
from tests.client.conftest import create_mock_response

pytestmark = pytest.mark.unit

API = "/index.php/apps/social/api/v1"

ACCOUNT = {
    "id": "7",
    "username": "alice",
    "acct": "alice",
    "display_name": "Alice",
    "locked": False,
    "bot": True,
    "note": "<p>Agent &amp; friend</p>",
    "url": "https://cloud.example/apps/social/@alice",
    "followers_count": 3,
    "following_count": 4,
    "statuses_count": 5,
    "created_at": "2026-09-01T10:00:00.000Z",
    "last_status_at": None,
    "emojis": [],
    "fields": [],
}

STATUS = {
    "id": "101",
    "nid": 101,
    "created_at": "2026-09-27T10:00:00.000Z",
    "edited_at": None,
    "content": '<p>Hello <a href="https://cloud.example/tags/mcp">#<span>mcp</span></a></p>',
    "spoiler_text": "",
    "visibility": "private",
    "language": "en",
    "in_reply_to_id": None,
    "in_reply_to_account_id": None,
    "url": "https://cloud.example/apps/social/@alice/101",
    "replies_count": 0,
    "reblogs_count": 0,
    "favourites_count": 1,
    "favourited": True,
    "reblogged": False,
    "mentions": [],
    "tags": [{"name": "mcp", "url": "https://cloud.example/tags/mcp"}],
    "media_attachments": [],
    "reblog": None,
    "account": ACCOUNT,
    "emojis": [],
    "card": None,
}

RELATIONSHIP = {
    "id": "8",
    "following": True,
    "showing_reblogs": True,
    "notifying": False,
    "followed_by": False,
    "blocking": False,
    "blocked_by": False,
    "muting": False,
    "muting_notifications": False,
    "requested": False,
    "domain_blocking": False,
    "endorsed": False,
    "requested_by": False,
    "note": "",
}

NOTIFICATION = {
    "id": "55",
    "type": "mention",
    "created_at": "2026-09-27T10:00:00.000Z",
    "account": ACCOUNT,
    "status": STATUS,
}

LINK = (
    "<https://cloud.example/index.php/apps/social/api/v1/timelines/home/"
    '?limit=20&max_id=90>; rel="next", '
    "<https://cloud.example/index.php/apps/social/api/v1/timelines/home/"
    '?limit=20&min_id=101>; rel="prev"'
)


def _client(mocker, json_data, headers=None):
    """A SocialClient over a mocked AsyncClient, plus the mock's ``request``."""
    http = mocker.AsyncMock(spec=httpx.AsyncClient)
    http.request.return_value = create_mock_response(
        json_data=json_data, headers=headers
    )
    return SocialClient(http, "alice"), http.request


def _sent(request):
    """(method, url, kwargs) of the last request."""
    method, url = request.call_args.args
    return method, url, request.call_args.kwargs


# Transport contract


async def test_every_request_sends_the_ocs_header_to_the_index_php_route(mocker):
    """Without OCS-APIRequest Social answers 401 'the access_token was revoked'."""
    client, request = _client(mocker, ACCOUNT)

    await client.verify_credentials()

    method, url, kwargs = _sent(request)
    assert (method, url) == ("GET", f"{API}/accounts/verify_credentials")
    assert kwargs["headers"]["OCS-APIRequest"] == "true"
    assert kwargs["headers"]["Accept"] == "application/json"


async def test_write_requests_send_the_ocs_header_too(mocker):
    client, request = _client(mocker, STATUS)

    await client.post_status("hi")

    assert _sent(request)[2]["headers"]["OCS-APIRequest"] == "true"


async def test_http_errors_propagate(mocker):
    """The tool layer turns these into messages; the client must not swallow them."""
    http = mocker.AsyncMock(spec=httpx.AsyncClient)
    http.request.return_value = create_mock_response(
        status_code=401, json_data={"error": "the access_token was revoked"}
    )
    client = SocialClient(http, "alice")

    with pytest.raises(httpx.HTTPStatusError):
        await client.verify_credentials()


# Accounts


async def test_verify_credentials_parses_the_account(mocker):
    client, _ = _client(mocker, ACCOUNT)

    account = await client.verify_credentials()

    assert account.id == "7"
    assert account.note == "Agent & friend"
    assert account.bot is True


async def test_get_account_accepts_a_remote_handle(mocker):
    client, request = _client(mocker, ACCOUNT)

    await client.get_account("@bob@example.org")

    assert _sent(request)[1] == f"{API}/accounts/@bob@example.org"


async def test_search_accounts_clamps_limit_and_sends_flags_only_when_set(mocker):
    client, request = _client(mocker, [ACCOUNT])

    accounts = await client.search_accounts("ali", limit=500)

    assert [a.acct for a in accounts] == ["alice"]
    method, url, kwargs = _sent(request)
    assert (method, url) == ("GET", f"{API}/accounts/search")
    assert kwargs["params"] == {"q": "ali", "limit": 80}

    await client.search_accounts("@bob@example.org", resolve=True, following=True)
    assert _sent(request)[2]["params"] == {
        "q": "@bob@example.org",
        "limit": 40,
        "resolve": "true",
        "following": "true",
    }


async def test_relationships_sends_the_php_array_form(mocker):
    """Social answers the singular ``?id=X`` with 400; it must be ``id[]=X``."""
    client, request = _client(mocker, [RELATIONSHIP])

    relationships = await client.get_relationships(["8", "9"])

    assert relationships[0].following is True
    method, url, kwargs = _sent(request)
    assert (method, url) == ("GET", f"{API}/accounts/relationships")
    assert kwargs["params"] == [("id[]", "8"), ("id[]", "9")]


async def test_relationships_rejects_a_handle(mocker):
    """A handle would silently drop out of Social's answer rather than fail."""
    client, request = _client(mocker, [])

    with pytest.raises(ValueError, match="account id"):
        await client.get_relationships(["alice"])

    request.assert_not_called()


async def test_followers_and_following_page_with_max_id(mocker):
    client, request = _client(mocker, [ACCOUNT], headers={"Link": LINK})

    accounts, cursors = await client.get_followers("7", limit=10, max_id="30")

    assert accounts[0].id == "7"
    assert cursors.next_max_id == "90"
    method, url, kwargs = _sent(request)
    assert (method, url) == ("GET", f"{API}/accounts/7/followers")
    assert kwargs["params"] == {"limit": 10, "max_id": "30"}

    await client.get_following("alice@example.org")
    assert _sent(request)[1] == f"{API}/accounts/alice@example.org/following"


async def test_follow_and_unfollow_return_the_relationship(mocker):
    client, request = _client(mocker, RELATIONSHIP)

    relationship = await client.follow("bob@example.org")
    assert relationship.following is True
    assert _sent(request)[:2] == ("POST", f"{API}/accounts/bob@example.org/follow")

    await client.unfollow("8")
    assert _sent(request)[:2] == ("POST", f"{API}/accounts/8/unfollow")


# Timelines


async def test_home_timeline_route_keeps_its_trailing_slash(mocker):
    """Social declares ``/api/v1/timelines/{timeline}/`` with the slash."""
    client, request = _client(mocker, [STATUS], headers={"Link": LINK})

    statuses, cursors = await client.get_timeline("home")

    assert statuses[0].id == "101"
    assert (cursors.next_max_id, cursors.prev_min_id) == ("90", "101")
    method, url, kwargs = _sent(request)
    assert (method, url) == ("GET", f"{API}/timelines/home/")
    assert kwargs["params"] == {"limit": 20}


async def test_local_timeline_is_public_with_local_true(mocker):
    client, request = _client(mocker, [])

    await client.get_timeline("local", limit=5, since_id="100")

    method, url, kwargs = _sent(request)
    assert url == f"{API}/timelines/public/"
    assert kwargs["params"] == {"limit": 5, "local": "true", "since_id": "100"}


async def test_public_timeline_sends_no_local_flag(mocker):
    client, request = _client(mocker, [])

    await client.get_timeline("public")

    assert _sent(request)[1] == f"{API}/timelines/public/"
    assert "local" not in _sent(request)[2]["params"]


async def test_timeline_clamps_limit_and_forwards_every_cursor(mocker):
    client, request = _client(mocker, [])

    await client.get_timeline("home", limit=999, max_id="9", min_id="3", since_id="2")

    assert _sent(request)[2]["params"] == {
        "limit": 50,
        "max_id": "9",
        "min_id": "3",
        "since_id": "2",
    }


async def test_empty_page_has_no_cursors(mocker):
    """No Link header on an empty page: the caller keeps its old cursor."""
    client, _ = _client(mocker, [])

    statuses, cursors = await client.get_timeline("home")

    assert statuses == []
    assert (cursors.next_max_id, cursors.prev_min_id) == (None, None)


async def test_non_numeric_cursor_never_reaches_the_wire(mocker):
    client, request = _client(mocker, [])

    with pytest.raises(ValueError, match="max_id"):
        await client.get_timeline("home", max_id="abc")

    request.assert_not_called()


async def test_hashtag_timeline_strips_the_hash_and_encodes_the_tag(mocker):
    client, request = _client(mocker, [STATUS])

    await client.get_hashtag_timeline("#café", local=True, limit=3)

    method, url, kwargs = _sent(request)
    assert url == f"{API}/timelines/tag/caf%C3%A9"
    assert kwargs["params"] == {"limit": 3, "local": "true"}


async def test_account_statuses_route(mocker):
    client, request = _client(mocker, [STATUS])

    await client.get_account_statuses("7", min_id="100")

    method, url, kwargs = _sent(request)
    assert url == f"{API}/accounts/7/statuses"
    assert kwargs["params"] == {"limit": 20, "min_id": "100"}


# Statuses


async def test_get_status_and_context(mocker):
    client, request = _client(mocker, STATUS)

    status = await client.get_status("101")
    assert status.text == "Hello #mcp"
    assert status.tags == ["mcp"]
    assert _sent(request)[:2] == ("GET", f"{API}/statuses/101")

    request.return_value = create_mock_response(
        json_data={"ancestors": [STATUS], "descendants": []}
    )
    context = await client.get_status_context("101")
    assert context.ancestors[0].id == "101"
    assert _sent(request)[:2] == ("GET", f"{API}/statuses/101/context")


async def test_post_status_sends_only_the_text_when_nothing_else_is_set(mocker):
    """No visibility on the wire: Social then applies the account's default."""
    client, request = _client(mocker, STATUS)

    await client.post_status("hello @bob")

    method, url, kwargs = _sent(request)
    assert (method, url) == ("POST", f"{API}/statuses")
    assert kwargs["json"] == {"status": "hello @bob"}


async def test_post_status_sends_every_field_it_was_given(mocker):
    client, request = _client(mocker, STATUS)

    await client.post_status(
        "reply",
        visibility="direct",
        in_reply_to_id="100",
        spoiler_text="cw",
        language="en",
    )

    assert _sent(request)[2]["json"] == {
        "status": "reply",
        "visibility": "direct",
        "in_reply_to_id": "100",
        "spoiler_text": "cw",
        "language": "en",
    }


async def test_delete_status(mocker):
    client, request = _client(mocker, STATUS)

    deleted = await client.delete_status("101")

    assert deleted.id == "101"
    assert _sent(request)[:2] == ("DELETE", f"{API}/statuses/101")


@pytest.mark.parametrize("action", ["favourite", "unfavourite", "reblog", "unreblog"])
async def test_status_actions_use_socials_verbs(mocker, action):
    client, request = _client(mocker, STATUS)

    await client.status_action("101", action)

    assert _sent(request)[:2] == ("POST", f"{API}/statuses/101/{action}")


async def test_status_action_rejects_an_unknown_verb(mocker):
    """Social refuses 'boost' -- the verb is 'reblog'."""
    client, request = _client(mocker, STATUS)

    with pytest.raises(ValueError, match="Unsupported status action"):
        await client.status_action("101", "boost")

    request.assert_not_called()


# Notifications


async def test_notifications_send_type_filters_as_php_arrays(mocker):
    client, request = _client(mocker, [NOTIFICATION], headers={"Link": LINK})

    notifications, cursors = await client.get_notifications(
        limit=10,
        since_id="50",
        types=["mention", "follow"],
        exclude_types=["favourite"],
    )

    assert notifications[0].type == "mention"
    assert notifications[0].status is not None
    assert notifications[0].status.id == "101"
    assert cursors.prev_min_id == "101"
    method, url, kwargs = _sent(request)
    assert (method, url) == ("GET", f"{API}/notifications")
    assert kwargs["params"] == [
        ("limit", "10"),
        ("since_id", "50"),
        ("types[]", "mention"),
        ("types[]", "follow"),
        ("exclude_types[]", "favourite"),
    ]


# Input validation


@pytest.mark.parametrize("bad", ["", "0", "-1", "12a", "../1", "1/2", " 1"])
def test_validate_status_id_rejects(bad):
    with pytest.raises(ValueError, match="status id"):
        validate_status_id(bad)


@pytest.mark.parametrize(
    "good", ["7", "alice", "@alice", "alice@example.org", "a.b_c-d@host:8443"]
)
def test_validate_account_accepts(good):
    assert validate_account(good) == good


@pytest.mark.parametrize(
    "bad",
    ["", "..", "../etc", "a/b", "alice@", "@", "https://example.org/users/a", "a b"],
)
def test_validate_account_rejects(bad):
    with pytest.raises(ValueError, match="Invalid account"):
        validate_account(bad)


@pytest.mark.parametrize("good", ["mcp", "#mcp", "café", "日本"])
def test_validate_hashtag_accepts(good):
    assert validate_hashtag(good) == good.removeprefix("#")


@pytest.mark.parametrize("bad", ["", "#", "..", "a/b", "a b", "a?b", "a%2F"])
def test_validate_hashtag_rejects(bad):
    with pytest.raises(ValueError, match="Invalid hashtag"):
        validate_hashtag(bad)


async def test_path_traversal_never_reaches_the_wire(mocker):
    client, request = _client(mocker, STATUS)

    with pytest.raises(ValueError):
        await client.get_status("../../ocs")
    with pytest.raises(ValueError):
        await client.get_account("../admin")

    request.assert_not_called()
