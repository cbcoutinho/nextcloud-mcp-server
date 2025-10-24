import logging

import httpx
import pytest

from nextcloud_mcp_server.client.cookbook import CookbookClient
from tests.client.conftest import (
    create_mock_error_response,
    create_mock_recipe_list_response,
    create_mock_recipe_response,
    create_mock_response,
)

logger = logging.getLogger(__name__)

# Mark all tests in this module as unit tests
pytestmark = pytest.mark.unit


async def test_cookbook_version(mocker):
    """Test that get_version correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={
            "cookbook_version": "1.0.0",
            "api_version": "1.0.0",
        },
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    version_data = await client.get_version()

    assert "cookbook_version" in version_data
    assert "api_version" in version_data
    assert version_data["cookbook_version"] == "1.0.0"

    mock_make_request.assert_called_once_with("GET", "/apps/cookbook/api/version")


async def test_cookbook_config(mocker):
    """Test that get_config correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200,
        json_data={
            "folder": "/recipes",
            "update_interval": 60,
            "print_image": True,
        },
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    config_data = await client.get_config()

    assert isinstance(config_data, dict)
    assert config_data["folder"] == "/recipes"

    mock_make_request.assert_called_once_with("GET", "/apps/cookbook/api/v1/config")


async def test_cookbook_list_recipes(mocker):
    """Test that list_recipes correctly parses the API response."""
    mock_response = create_mock_recipe_list_response(
        recipes=[
            {"id": 1, "name": "Recipe 1", "recipeCategory": "Test"},
            {"id": 2, "name": "Recipe 2", "recipeCategory": "Test"},
        ]
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    recipes = await client.list_recipes()

    assert isinstance(recipes, list)
    assert len(recipes) == 2
    assert recipes[0]["name"] == "Recipe 1"

    mock_make_request.assert_called_once_with("GET", "/apps/cookbook/api/v1/recipes")


async def test_cookbook_create_recipe(mocker):
    """Test that create_recipe correctly parses the API response."""
    # Create_recipe returns just the recipe ID
    mock_response = create_mock_response(status_code=200, json_data=123)

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    recipe_data = {
        "name": "Test Recipe",
        "description": "Test description",
        "recipeIngredient": ["100g flour"],
        "recipeInstructions": ["Mix ingredients"],
    }
    recipe_id = await client.create_recipe(recipe_data)

    assert recipe_id == 123

    mock_make_request.assert_called_once_with(
        "POST", "/apps/cookbook/api/v1/recipes", json=recipe_data
    )


async def test_cookbook_get_recipe(mocker):
    """Test that get_recipe correctly parses the API response."""
    mock_response = create_mock_recipe_response(
        recipe_id=123,
        name="Test Recipe",
        description="Test description",
        recipe_category="Test",
        keywords="test,integration",
        recipe_yield=4,
        recipeIngredient=["100g flour", "2 eggs"],
        recipeInstructions=["Mix ingredients", "Cook"],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    recipe = await client.get_recipe(recipe_id=123)

    assert recipe["id"] == 123
    assert recipe["name"] == "Test Recipe"
    assert recipe["description"] == "Test description"
    assert len(recipe["recipeIngredient"]) == 2
    assert len(recipe["recipeInstructions"]) == 2

    mock_make_request.assert_called_once_with(
        "GET", "/apps/cookbook/api/v1/recipes/123"
    )


async def test_cookbook_update_recipe(mocker):
    """Test that update_recipe correctly parses the API response."""
    # Update_recipe returns the recipe ID
    mock_response = create_mock_response(status_code=200, json_data=123)

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    updated_data = {
        "name": "Updated Recipe",
        "description": "Updated description",
        "recipeIngredient": ["100g flour", "2 eggs", "200ml milk"],
        "recipeInstructions": ["Mix ingredients", "Cook", "Serve"],
    }
    updated_id = await client.update_recipe(recipe_id=123, recipe_data=updated_data)

    assert updated_id == 123

    mock_make_request.assert_called_once_with(
        "PUT", "/apps/cookbook/api/v1/recipes/123", json=updated_data
    )


async def test_cookbook_delete_recipe(mocker):
    """Test that delete_recipe correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200, json_data="Recipe deleted successfully"
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    result = await client.delete_recipe(recipe_id=123)

    assert isinstance(result, str)
    assert "deleted" in result.lower()

    mock_make_request.assert_called_once_with(
        "DELETE", "/apps/cookbook/api/v1/recipes/123"
    )


async def test_cookbook_delete_nonexistent_recipe(mocker):
    """Test that deleting a non-existent recipe raises HTTPStatusError."""
    error_response = create_mock_error_response(404, "Recipe not found")

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(CookbookClient, "_make_request")
    mock_make_request.side_effect = httpx.HTTPStatusError(
        "404 Not Found",
        request=httpx.Request("DELETE", "http://test.local"),
        response=error_response,
    )

    client = CookbookClient(mock_client, "testuser")

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.delete_recipe(recipe_id=999999999)

    assert excinfo.value.response.status_code == 404


async def test_cookbook_search_recipes(mocker):
    """Test that search_recipes correctly parses the API response."""
    mock_response = create_mock_recipe_list_response(
        recipes=[
            {"id": 1, "name": "Test Recipe 1", "keywords": "test,search"},
            {"id": 2, "name": "Test Recipe 2", "keywords": "test,search"},
        ]
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    search_results = await client.search_recipes("test")

    assert isinstance(search_results, list)
    assert len(search_results) == 2

    # Verify URL encoding happened
    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args[0]
    assert "/apps/cookbook/api/v1/search/" in call_args[1]


async def test_cookbook_list_categories(mocker):
    """Test that list_categories correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200,
        json_data=[
            {"name": "Desserts", "recipe_count": 5},
            {"name": "Main Course", "recipe_count": 10},
        ],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    categories = await client.list_categories()

    assert isinstance(categories, list)
    assert len(categories) == 2
    assert categories[0]["name"] == "Desserts"
    assert categories[0]["recipe_count"] == 5

    mock_make_request.assert_called_once_with("GET", "/apps/cookbook/api/v1/categories")


async def test_cookbook_get_recipes_in_category(mocker):
    """Test that get_recipes_in_category correctly parses the API response."""
    mock_response = create_mock_recipe_list_response(
        recipes=[
            {"id": 1, "name": "Recipe 1", "recipeCategory": "Desserts"},
            {"id": 2, "name": "Recipe 2", "recipeCategory": "Desserts"},
        ]
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    recipes_in_category = await client.get_recipes_in_category("Desserts")

    assert isinstance(recipes_in_category, list)
    assert len(recipes_in_category) == 2
    assert recipes_in_category[0]["recipeCategory"] == "Desserts"

    # Verify URL encoding happened
    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args[0]
    assert "/apps/cookbook/api/v1/category/" in call_args[1]


async def test_cookbook_list_keywords(mocker):
    """Test that list_keywords correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200,
        json_data=[
            {"name": "vegetarian", "recipe_count": 15},
            {"name": "quick", "recipe_count": 8},
        ],
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    keywords = await client.list_keywords()

    assert isinstance(keywords, list)
    assert len(keywords) == 2
    assert keywords[0]["name"] == "vegetarian"
    assert keywords[0]["recipe_count"] == 15

    mock_make_request.assert_called_once_with("GET", "/apps/cookbook/api/v1/keywords")


async def test_cookbook_get_recipes_with_keywords(mocker):
    """Test that get_recipes_with_keywords correctly parses the API response."""
    mock_response = create_mock_recipe_list_response(
        recipes=[
            {"id": 1, "name": "Recipe 1", "keywords": "vegetarian,quick"},
            {"id": 2, "name": "Recipe 2", "keywords": "vegetarian,healthy"},
        ]
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    recipes_with_keywords = await client.get_recipes_with_keywords(
        ["vegetarian", "quick"]
    )

    assert isinstance(recipes_with_keywords, list)
    assert len(recipes_with_keywords) == 2

    # Verify URL encoding and keyword joining happened
    mock_make_request.assert_called_once()
    call_args = mock_make_request.call_args[0]
    assert "/apps/cookbook/api/v1/tags/" in call_args[1]


async def test_cookbook_reindex(mocker):
    """Test that reindex correctly parses the API response."""
    mock_response = create_mock_response(
        status_code=200,
        json_data="Reindex completed successfully",
    )

    mock_client = mocker.AsyncMock(spec=httpx.AsyncClient)
    mock_make_request = mocker.patch.object(
        CookbookClient, "_make_request", return_value=mock_response
    )

    client = CookbookClient(mock_client, "testuser")
    result = await client.reindex()

    assert isinstance(result, str)
    assert "reindex" in result.lower() or "completed" in result.lower()

    mock_make_request.assert_called_once_with("POST", "/apps/cookbook/api/v1/reindex")
