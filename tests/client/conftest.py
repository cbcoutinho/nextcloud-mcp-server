import httpx

# ============================================================================
# Mock Response Helpers for Unit Tests
# ============================================================================


def create_mock_response(
    status_code: int = 200,
    json_data: dict | list | None = None,
    headers: dict | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    """Create a mock httpx.Response for testing.

    Args:
        status_code: HTTP status code
        json_data: JSON data to return from response.json()
        headers: Response headers
        content: Raw response content (if not using json_data)

    Returns:
        Mock httpx.Response object
    """
    import json as json_module

    if headers is None:
        headers = {}

    # If json_data is provided, serialize it to content
    if json_data is not None:
        content = json_module.dumps(json_data).encode("utf-8")
        headers.setdefault("content-type", "application/json")

    if content is None:
        content = b""

    # Create a mock request
    request = httpx.Request("GET", "http://test.local/api")

    # Create the response
    return httpx.Response(
        status_code=status_code,
        headers=headers,
        content=content,
        request=request,
    )


def create_mock_note_response(
    note_id: int = 1,
    title: str = "Test Note",
    content: str = "Test content",
    category: str = "Test",
    etag: str = "abc123",
    **kwargs,
) -> httpx.Response:
    """Create a mock response for a Nextcloud note.

    Args:
        note_id: Note ID
        title: Note title
        content: Note content
        category: Note category
        etag: ETag header value
        **kwargs: Additional note fields

    Returns:
        Mock httpx.Response with note data
    """
    note_data = {
        "id": note_id,
        "title": title,
        "content": content,
        "category": category,
        "etag": etag,
        "modified": 1234567890,
        "favorite": False,
        **kwargs,
    }

    return create_mock_response(
        status_code=200,
        json_data=note_data,
        headers={"etag": f'"{etag}"'},
    )


def create_mock_error_response(
    status_code: int,
    message: str = "Error",
) -> httpx.Response:
    """Create a mock error response.

    Args:
        status_code: HTTP error status code (e.g., 404, 412)
        message: Error message

    Returns:
        Mock httpx.Response with error
    """
    return create_mock_response(
        status_code=status_code,
        json_data={"message": message},
    )


def create_mock_recipe_response(
    recipe_id: int = 1,
    name: str = "Test Recipe",
    description: str = "Test description",
    recipe_category: str = "Test",
    keywords: str = "test",
    recipe_yield: int = 4,
    **kwargs,
) -> httpx.Response:
    """Create a mock response for a Nextcloud Cookbook recipe.

    Args:
        recipe_id: Recipe ID
        name: Recipe name
        description: Recipe description
        recipe_category: Recipe category
        keywords: Recipe keywords (comma-separated)
        recipe_yield: Recipe yield (number of servings)
        **kwargs: Additional recipe fields (recipeIngredient, recipeInstructions, etc.)

    Returns:
        Mock httpx.Response with recipe data
    """
    recipe_data = {
        "id": recipe_id,
        "name": name,
        "description": description,
        "recipeCategory": recipe_category,
        "keywords": keywords,
        "recipeYield": recipe_yield,
        "recipeIngredient": kwargs.get("recipeIngredient", []),
        "recipeInstructions": kwargs.get("recipeInstructions", []),
        "prepTime": kwargs.get("prepTime", "PT15M"),
        "cookTime": kwargs.get("cookTime", "PT30M"),
        "totalTime": kwargs.get("totalTime", "PT45M"),
        "url": kwargs.get("url", ""),
        **{
            k: v
            for k, v in kwargs.items()
            if k
            not in [
                "recipeIngredient",
                "recipeInstructions",
                "prepTime",
                "cookTime",
                "totalTime",
                "url",
            ]
        },
    }

    return create_mock_response(
        status_code=200,
        json_data=recipe_data,
    )


def create_mock_recipe_list_response(
    recipes: list[dict] = None,
) -> httpx.Response:
    """Create a mock response for a list of recipe stubs.

    Args:
        recipes: List of recipe stub dictionaries. If None, returns empty list.

    Returns:
        Mock httpx.Response with recipe list data
    """
    if recipes is None:
        recipes = []

    return create_mock_response(
        status_code=200,
        json_data=recipes,
    )


def create_mock_deck_board_response(
    board_id: int = 1,
    title: str = "Test Board",
    color: str = "0000FF",
    **kwargs,
) -> httpx.Response:
    """Create a mock response for a Nextcloud Deck board.

    Args:
        board_id: Board ID
        title: Board title
        color: Board color (hex without #)
        **kwargs: Additional board fields

    Returns:
        Mock httpx.Response with board data
    """
    board_data = {
        "id": board_id,
        "title": title,
        "color": color,
        "owner": {
            "primaryKey": "testuser",
            "uid": "testuser",
            "displayname": "Test User",
        },
        "archived": False,
        "labels": [],
        "acl": [],
        "permissions": {
            "PERMISSION_READ": True,
            "PERMISSION_EDIT": True,
            "PERMISSION_MANAGE": True,
            "PERMISSION_SHARE": True,
        },
        "users": [],
        "deletedAt": 0,
        **kwargs,
    }

    return create_mock_response(status_code=200, json_data=board_data)


def create_mock_deck_stack_response(
    stack_id: int = 1,
    title: str = "Test Stack",
    board_id: int = 1,
    order: int = 1,
    **kwargs,
) -> httpx.Response:
    """Create a mock response for a Nextcloud Deck stack.

    Args:
        stack_id: Stack ID
        title: Stack title
        board_id: Parent board ID
        order: Stack order
        **kwargs: Additional stack fields

    Returns:
        Mock httpx.Response with stack data
    """
    stack_data = {
        "id": stack_id,
        "title": title,
        "boardId": board_id,
        "order": order,
        "deletedAt": 0,
        **kwargs,
    }

    return create_mock_response(status_code=200, json_data=stack_data)


def create_mock_deck_card_response(
    card_id: int = 1,
    title: str = "Test Card",
    stack_id: int = 1,
    description: str = "Test description",
    **kwargs,
) -> httpx.Response:
    """Create a mock response for a Nextcloud Deck card.

    Args:
        card_id: Card ID
        title: Card title
        stack_id: Parent stack ID
        description: Card description
        **kwargs: Additional card fields

    Returns:
        Mock httpx.Response with card data
    """
    card_data = {
        "id": card_id,
        "title": title,
        "stackId": stack_id,
        "type": "plain",
        "order": 999,
        "archived": False,
        "owner": "testuser",
        "description": description,
        "labels": [],
        "assignedUsers": [],
        **kwargs,
    }

    return create_mock_response(status_code=200, json_data=card_data)


def create_mock_deck_label_response(
    label_id: int = 1,
    title: str = "Test Label",
    color: str = "FF0000",
    board_id: int = 1,
    **kwargs,
) -> httpx.Response:
    """Create a mock response for a Nextcloud Deck label.

    Args:
        label_id: Label ID
        title: Label title
        color: Label color (hex without #)
        board_id: Parent board ID
        **kwargs: Additional label fields

    Returns:
        Mock httpx.Response with label data
    """
    label_data = {
        "id": label_id,
        "title": title,
        "color": color,
        "boardId": board_id,
        **kwargs,
    }

    return create_mock_response(status_code=200, json_data=label_data)


def create_mock_deck_comment_response(
    comment_id: int = 1,
    message: str = "Test comment",
    card_id: int = 1,
    **kwargs,
) -> httpx.Response:
    """Create a mock response for a Nextcloud Deck comment (OCS format).

    Args:
        comment_id: Comment ID
        message: Comment message
        card_id: Parent card ID
        **kwargs: Additional comment fields

    Returns:
        Mock httpx.Response with comment data in OCS format
    """
    comment_data = {
        "id": comment_id,
        "objectId": card_id,
        "message": message,
        "actorId": "testuser",
        "actorDisplayName": "Test User",
        "actorType": "users",
        "creationDateTime": "2024-01-01T00:00:00+00:00",
        "mentions": [],  # Required field
        **kwargs,
    }

    # Wrap in OCS format
    ocs_response = {"ocs": {"meta": {"status": "ok"}, "data": comment_data}}

    return create_mock_response(status_code=200, json_data=ocs_response)


def create_mock_tables_list_response(
    tables: list[dict] = None,
) -> httpx.Response:
    """Create a mock response for list of Nextcloud Tables (OCS format).

    Args:
        tables: List of table dictionaries. If None, returns empty list.

    Returns:
        Mock httpx.Response with tables list data in OCS format
    """
    if tables is None:
        tables = []

    ocs_response = {"ocs": {"meta": {"status": "ok"}, "data": tables}}

    return create_mock_response(status_code=200, json_data=ocs_response)


def create_mock_table_schema_response(
    table_id: int = 1,
    columns: list[dict] = None,
    **kwargs,
) -> httpx.Response:
    """Create a mock response for Nextcloud Tables schema.

    Args:
        table_id: Table ID
        columns: List of column definitions. If None, creates sample columns.
        **kwargs: Additional schema fields

    Returns:
        Mock httpx.Response with table schema data
    """
    if columns is None:
        columns = [
            {"id": 1, "title": "Column 1", "type": "text"},
            {"id": 2, "title": "Column 2", "type": "number"},
        ]

    schema_data = {
        "id": table_id,
        "columns": columns,
        **kwargs,
    }

    return create_mock_response(status_code=200, json_data=schema_data)


def create_mock_table_row_response(
    row_id: int = 1,
    table_id: int = 1,
    data: list[dict] = None,
    **kwargs,
) -> httpx.Response:
    """Create a mock response for Nextcloud Tables row.

    Args:
        row_id: Row ID
        table_id: Table ID
        data: List of column data dicts. If None, creates sample data.
        **kwargs: Additional row fields

    Returns:
        Mock httpx.Response with row data
    """
    if data is None:
        data = [
            {"columnId": 1, "value": "Test value"},
            {"columnId": 2, "value": 42},
        ]

    row_data = {
        "id": row_id,
        "tableId": table_id,
        "createdBy": "testuser",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "lastEditBy": "testuser",
        "lastEditAt": "2024-01-01T00:00:00+00:00",
        "data": data,
        **kwargs,
    }

    return create_mock_response(status_code=200, json_data=row_data)


def create_mock_table_row_ocs_response(
    row_id: int = 1,
    table_id: int = 1,
    data: list[dict] = None,
    **kwargs,
) -> httpx.Response:
    """Create a mock OCS response for Nextcloud Tables row (used by create_row).

    Args:
        row_id: Row ID
        table_id: Table ID
        data: List of column data dicts. If None, creates sample data.
        **kwargs: Additional row fields

    Returns:
        Mock httpx.Response with row data in OCS format
    """
    if data is None:
        data = [
            {"columnId": 1, "value": "Test value"},
            {"columnId": 2, "value": 42},
        ]

    row_data = {
        "id": row_id,
        "tableId": table_id,
        "createdBy": "testuser",
        "createdAt": "2024-01-01T00:00:00+00:00",
        "lastEditBy": "testuser",
        "lastEditAt": "2024-01-01T00:00:00+00:00",
        "data": data,
        **kwargs,
    }

    ocs_response = {"ocs": {"meta": {"status": "ok"}, "data": row_data}}

    return create_mock_response(status_code=200, json_data=ocs_response)


# ============================================================================
# News Mock Response Helpers
# ============================================================================


def create_mock_news_folders_response(
    folders: list[dict] | None = None,
) -> httpx.Response:
    """Create a mock response for News folders list.

    Args:
        folders: List of folder dictionaries. If None, returns empty list.

    Returns:
        Mock httpx.Response with folders data
    """
    if folders is None:
        folders = []

    return create_mock_response(status_code=200, json_data={"folders": folders})


def create_mock_news_folder_response(
    folder_id: int = 1,
    name: str = "Test Folder",
    **kwargs,
) -> httpx.Response:
    """Create a mock response for a News folder.

    Args:
        folder_id: Folder ID
        name: Folder name
        **kwargs: Additional folder fields

    Returns:
        Mock httpx.Response with folder data
    """
    folder_data = {
        "id": folder_id,
        "name": name,
        **kwargs,
    }

    return create_mock_response(status_code=200, json_data={"folders": [folder_data]})


def create_mock_news_feeds_response(
    feeds: list[dict] | None = None,
    starred_count: int = 0,
    newest_item_id: int | None = None,
) -> httpx.Response:
    """Create a mock response for News feeds list.

    Args:
        feeds: List of feed dictionaries. If None, returns empty list.
        starred_count: Number of starred items
        newest_item_id: ID of newest item

    Returns:
        Mock httpx.Response with feeds data
    """
    if feeds is None:
        feeds = []

    data = {
        "feeds": feeds,
        "starredCount": starred_count,
    }
    if newest_item_id is not None:
        data["newestItemId"] = newest_item_id

    return create_mock_response(status_code=200, json_data=data)


def create_mock_news_feed_response(
    feed_id: int = 1,
    url: str = "https://example.com/feed",
    title: str = "Test Feed",
    favicon_link: str | None = None,
    folder_id: int | None = None,
    unread_count: int = 0,
    **kwargs,
) -> httpx.Response:
    """Create a mock response for a News feed.

    Args:
        feed_id: Feed ID
        url: Feed URL
        title: Feed title
        favicon_link: Favicon URL
        folder_id: Parent folder ID
        unread_count: Number of unread items
        **kwargs: Additional feed fields

    Returns:
        Mock httpx.Response with feed data
    """
    feed_data = {
        "id": feed_id,
        "url": url,
        "title": title,
        "faviconLink": favicon_link,
        "folderId": folder_id,
        "unreadCount": unread_count,
        "link": kwargs.get("link", "https://example.com"),
        "added": kwargs.get("added", 1700000000),
        "updateErrorCount": kwargs.get("updateErrorCount", 0),
        "lastUpdateError": kwargs.get("lastUpdateError"),
        **{
            k: v
            for k, v in kwargs.items()
            if k not in ["link", "added", "updateErrorCount", "lastUpdateError"]
        },
    }

    return create_mock_response(status_code=200, json_data={"feeds": [feed_data]})


def create_mock_news_items_response(
    items: list[dict] | None = None,
) -> httpx.Response:
    """Create a mock response for News items list.

    Args:
        items: List of item dictionaries. If None, returns empty list.

    Returns:
        Mock httpx.Response with items data
    """
    if items is None:
        items = []

    return create_mock_response(status_code=200, json_data={"items": items})


def create_mock_news_item(
    item_id: int = 1,
    feed_id: int = 1,
    title: str = "Test Article",
    body: str = "<p>Test content</p>",
    url: str = "https://example.com/article",
    author: str | None = "Test Author",
    pub_date: int = 1700000000,
    unread: bool = True,
    starred: bool = False,
    **kwargs,
) -> dict:
    """Create a mock News item dictionary.

    Args:
        item_id: Item ID
        feed_id: Parent feed ID
        title: Article title
        body: Article body (HTML)
        url: Article URL
        author: Article author
        pub_date: Publication timestamp (Unix)
        unread: Whether item is unread
        starred: Whether item is starred
        **kwargs: Additional item fields

    Returns:
        Item dictionary
    """
    return {
        "id": item_id,
        "feedId": feed_id,
        "title": title,
        "body": body,
        "url": url,
        "author": author,
        "pubDate": pub_date,
        "unread": unread,
        "starred": starred,
        "guid": kwargs.get("guid", f"guid-{item_id}"),
        "guidHash": kwargs.get("guidHash", f"hash-{item_id}"),
        "lastModified": kwargs.get("lastModified", pub_date * 1000000),
        "enclosureLink": kwargs.get("enclosureLink"),
        "enclosureMime": kwargs.get("enclosureMime"),
        "fingerprint": kwargs.get("fingerprint", f"fp-{item_id}"),
        "contentHash": kwargs.get("contentHash", f"ch-{item_id}"),
        **{
            k: v
            for k, v in kwargs.items()
            if k
            not in [
                "guid",
                "guidHash",
                "lastModified",
                "enclosureLink",
                "enclosureMime",
                "fingerprint",
                "contentHash",
            ]
        },
    }


def create_mock_news_status_response(
    version: str = "25.0.0",
    warnings: dict | None = None,
) -> httpx.Response:
    """Create a mock response for News status.

    Args:
        version: News app version
        warnings: Warning messages

    Returns:
        Mock httpx.Response with status data
    """
    data = {
        "version": version,
        "warnings": warnings or {},
    }

    return create_mock_response(status_code=200, json_data=data)
