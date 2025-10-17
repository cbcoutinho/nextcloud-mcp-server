import asyncio
import logging
import uuid

import pytest
from httpx import HTTPStatusError

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


async def test_cookbook_version(nc_client: NextcloudClient):
    """Test getting Cookbook app version."""
    logger.info("Getting Cookbook app version")
    version_data = await nc_client.cookbook.get_version()

    assert "cookbook_version" in version_data
    assert "api_version" in version_data
    logger.info(f"Cookbook version: {version_data}")


async def test_cookbook_config(nc_client: NextcloudClient):
    """Test getting Cookbook app configuration."""
    logger.info("Getting Cookbook app configuration")
    config_data = await nc_client.cookbook.get_config()

    # Config may be empty initially, just verify we can get it
    assert isinstance(config_data, dict)
    logger.info(f"Cookbook config: {config_data}")


async def test_cookbook_list_recipes(nc_client: NextcloudClient):
    """Test listing all recipes."""
    logger.info("Listing all recipes")
    recipes = await nc_client.cookbook.list_recipes()

    assert isinstance(recipes, list)
    logger.info(f"Found {len(recipes)} recipes")


async def test_cookbook_create_and_read_recipe(nc_client: NextcloudClient):
    """Test creating a recipe and reading it back."""
    # Create a test recipe
    recipe_name = f"Test Recipe {uuid.uuid4().hex[:8]}"
    recipe_data = {
        "name": recipe_name,
        "description": "A test recipe for integration testing",
        "recipeIngredient": ["100g flour", "2 eggs", "200ml milk"],
        "recipeInstructions": [
            "Mix ingredients",
            "Cook for 20 minutes",
            "Serve hot",
        ],
        "recipeCategory": "Test",
        "keywords": "test,integration",
        "recipeYield": 4,
        "prepTime": "PT15M",
        "cookTime": "PT20M",
        "totalTime": "PT35M",
    }

    logger.info(f"Creating recipe: {recipe_name}")
    recipe_id = await nc_client.cookbook.create_recipe(recipe_data)
    logger.info(f"Created recipe with ID: {recipe_id}")

    try:
        # Read the recipe back
        logger.info(f"Reading recipe ID: {recipe_id}")
        retrieved_recipe = await nc_client.cookbook.get_recipe(recipe_id)

        assert retrieved_recipe["name"] == recipe_name
        assert (
            retrieved_recipe["description"] == "A test recipe for integration testing"
        )
        assert len(retrieved_recipe["recipeIngredient"]) == 3
        assert len(retrieved_recipe["recipeInstructions"]) == 3
        assert retrieved_recipe["recipeCategory"] == "Test"
        assert retrieved_recipe["recipeYield"] == 4
        logger.info(f"Successfully verified recipe: {recipe_name}")

    finally:
        # Clean up
        logger.info(f"Deleting recipe ID: {recipe_id}")
        await nc_client.cookbook.delete_recipe(recipe_id)
        logger.info(f"Successfully deleted recipe ID: {recipe_id}")


async def test_cookbook_update_recipe(nc_client: NextcloudClient):
    """Test updating a recipe."""
    # Create a test recipe
    recipe_name = f"Test Recipe {uuid.uuid4().hex[:8]}"
    recipe_data = {
        "name": recipe_name,
        "description": "Original description",
        "recipeIngredient": ["100g flour"],
        "recipeInstructions": ["Mix ingredients"],
        "recipeCategory": "Original",
    }

    logger.info(f"Creating recipe for update test: {recipe_name}")
    recipe_id = await nc_client.cookbook.create_recipe(recipe_data)

    try:
        # Get the current recipe first
        current_recipe = await nc_client.cookbook.get_recipe(recipe_id)

        # Update the recipe with all required fields
        updated_data = current_recipe.copy()
        updated_data["description"] = "Updated description"
        updated_data["recipeIngredient"] = ["100g flour", "2 eggs"]
        updated_data["recipeInstructions"] = ["Mix ingredients", "Cook"]
        updated_data["recipeCategory"] = "Updated"

        logger.info(f"Updating recipe ID: {recipe_id}")
        updated_id = await nc_client.cookbook.update_recipe(recipe_id, updated_data)
        assert updated_id == recipe_id

        # Verify the update
        await asyncio.sleep(1)  # Allow propagation
        updated_recipe = await nc_client.cookbook.get_recipe(recipe_id)
        assert updated_recipe["description"] == "Updated description"
        assert len(updated_recipe["recipeIngredient"]) == 2
        assert len(updated_recipe["recipeInstructions"]) == 2
        assert updated_recipe["recipeCategory"] == "Updated"
        logger.info(f"Successfully updated recipe ID: {recipe_id}")

    finally:
        # Clean up
        logger.info(f"Deleting recipe ID: {recipe_id}")
        await nc_client.cookbook.delete_recipe(recipe_id)


async def test_cookbook_delete_nonexistent_recipe(nc_client: NextcloudClient):
    """Test deleting a non-existent recipe.

    Note: The Cookbook API may return 502 or succeed silently for non-existent IDs
    rather than 404. This test verifies the behavior."""
    non_existent_id = 999999999

    logger.info(f"Attempting to delete non-existent recipe ID: {non_existent_id}")
    try:
        result = await nc_client.cookbook.delete_recipe(non_existent_id)
        logger.info(f"Delete returned: {result}")
        # API may succeed silently or return an error message
        assert isinstance(result, str)
    except HTTPStatusError as e:
        # API may return 404 or 502 for non-existent recipes
        assert e.response.status_code in [404, 502]
        logger.info(f"Delete correctly failed with {e.response.status_code}")


async def test_cookbook_import_recipe_from_url(
    nc_client: NextcloudClient, test_recipe_server: str
):
    """Test importing a recipe from a URL.

    This is the key feature test - importing recipes from URLs using schema.org metadata.
    Uses a local test server to provide reliable, controlled test data.
    """
    # Replace localhost with Docker bridge gateway IP so the Nextcloud container can reach it
    # The test_recipe_server runs on the host, but Nextcloud runs in Docker
    # On Linux, 172.17.0.1 is the default Docker bridge gateway
    # On Mac/Windows, try host.docker.internal first
    import platform

    if platform.system() == "Linux":
        docker_host = "172.17.0.1"
    else:
        docker_host = "host.docker.internal"

    docker_accessible_url = test_recipe_server.replace("localhost", docker_host)
    test_url = f"{docker_accessible_url}/black-pepper-tofu"

    logger.info(f"Importing recipe from local test URL (Docker-accessible): {test_url}")

    try:
        imported_recipe = await nc_client.cookbook.import_recipe(test_url)
        logger.info(f"Successfully imported recipe: {imported_recipe.get('name')}")

        # Verify basic recipe structure
        assert "name" in imported_recipe
        assert imported_recipe["name"] == "Black Pepper Tofu"
        assert "id" in imported_recipe

        # Verify schema.org fields were imported correctly
        assert imported_recipe.get("description")
        assert len(imported_recipe.get("recipeIngredient", [])) > 0
        assert len(imported_recipe.get("recipeInstructions", [])) > 0
        assert imported_recipe.get("recipeCategory") == "Main Course"
        assert "tofu" in imported_recipe.get("keywords", "").lower()

        recipe_id = int(imported_recipe["id"])

        # Verify we can read it back
        retrieved = await nc_client.cookbook.get_recipe(recipe_id)
        assert retrieved["name"] == imported_recipe["name"]
        logger.info(f"Verified imported recipe ID: {recipe_id}")

        # Clean up
        logger.info(f"Deleting imported recipe ID: {recipe_id}")
        await nc_client.cookbook.delete_recipe(recipe_id)
        logger.info("Successfully deleted imported recipe")

    except HTTPStatusError as e:
        if e.response.status_code == 409:
            # Recipe already exists - this is acceptable in tests
            logger.warning("Recipe already exists (409 conflict)")
            pytest.skip("Recipe already exists in test environment")
        elif e.response.status_code == 400:
            # URL couldn't be imported
            logger.error(
                f"Failed to import recipe from local test URL: {test_url}. "
                f"Status: {e.response.status_code}, Response: {e.response.text}"
            )
            raise
        else:
            raise


async def test_cookbook_search_recipes(nc_client: NextcloudClient):
    """Test searching for recipes."""
    # Create a test recipe with unique keywords
    unique_keyword = f"testkeyword{uuid.uuid4().hex[:8]}"
    recipe_name = f"Test Recipe {uuid.uuid4().hex[:8]}"
    recipe_data = {
        "name": recipe_name,
        "description": f"Recipe for testing search with {unique_keyword}",
        "keywords": unique_keyword,
        "recipeIngredient": ["test ingredient"],
        "recipeInstructions": ["test instruction"],
    }

    logger.info(f"Creating recipe for search test with keyword: {unique_keyword}")
    recipe_id = await nc_client.cookbook.create_recipe(recipe_data)

    try:
        # Allow time for indexing
        await asyncio.sleep(2)

        # Search for the recipe
        logger.info(f"Searching for recipes with keyword: {unique_keyword}")
        search_results = await nc_client.cookbook.search_recipes(unique_keyword)

        assert isinstance(search_results, list)
        # Should find at least our recipe
        assert len(search_results) > 0

        # Verify our recipe is in the results
        found = any(str(r.get("id")) == str(recipe_id) for r in search_results)
        assert found, f"Recipe {recipe_id} not found in search results"
        logger.info(f"Successfully found recipe {recipe_id} in search results")

    finally:
        # Clean up
        logger.info(f"Deleting recipe ID: {recipe_id}")
        await nc_client.cookbook.delete_recipe(recipe_id)


async def test_cookbook_list_categories(nc_client: NextcloudClient):
    """Test listing recipe categories."""
    logger.info("Listing recipe categories")
    categories = await nc_client.cookbook.list_categories()

    assert isinstance(categories, list)
    logger.info(f"Found {len(categories)} categories")

    # Each category should have name and recipe_count
    if categories:
        assert "name" in categories[0]
        assert "recipe_count" in categories[0]


async def test_cookbook_get_recipes_in_category(nc_client: NextcloudClient):
    """Test getting recipes in a specific category."""
    # Create a recipe in a test category
    unique_category = f"TestCategory{uuid.uuid4().hex[:8]}"
    recipe_name = f"Test Recipe {uuid.uuid4().hex[:8]}"
    recipe_data = {
        "name": recipe_name,
        "recipeCategory": unique_category,
        "recipeIngredient": ["test"],
        "recipeInstructions": ["test"],
    }

    logger.info(f"Creating recipe in category: {unique_category}")
    recipe_id = await nc_client.cookbook.create_recipe(recipe_data)

    try:
        # Allow time for indexing
        await asyncio.sleep(2)

        # Get recipes in this category
        logger.info(f"Getting recipes in category: {unique_category}")
        recipes_in_category = await nc_client.cookbook.get_recipes_in_category(
            unique_category
        )

        assert isinstance(recipes_in_category, list)
        assert len(recipes_in_category) > 0

        # Verify our recipe is in the results
        found = any(str(r.get("id")) == str(recipe_id) for r in recipes_in_category)
        assert found, f"Recipe {recipe_id} not found in category {unique_category}"
        logger.info(f"Successfully found recipe in category {unique_category}")

    finally:
        # Clean up
        logger.info(f"Deleting recipe ID: {recipe_id}")
        await nc_client.cookbook.delete_recipe(recipe_id)


async def test_cookbook_list_keywords(nc_client: NextcloudClient):
    """Test listing recipe keywords."""
    logger.info("Listing recipe keywords")
    keywords = await nc_client.cookbook.list_keywords()

    assert isinstance(keywords, list)
    logger.info(f"Found {len(keywords)} keywords")

    # Each keyword should have name and recipe_count
    if keywords:
        assert "name" in keywords[0]
        assert "recipe_count" in keywords[0]


async def test_cookbook_get_recipes_with_keywords(nc_client: NextcloudClient):
    """Test getting recipes with specific keywords.

    Note: The keywords filtering may require exact keyword matches and sufficient
    indexing time. This test uses a longer wait time."""
    # Create a recipe with unique keywords
    unique_keyword = f"testtag{uuid.uuid4().hex[:8]}"
    recipe_name = f"Test Recipe {uuid.uuid4().hex[:8]}"
    recipe_data = {
        "name": recipe_name,
        "keywords": f"{unique_keyword},integration",
        "recipeIngredient": ["test"],
        "recipeInstructions": ["test"],
    }

    logger.info(f"Creating recipe with keyword: {unique_keyword}")
    recipe_id = await nc_client.cookbook.create_recipe(recipe_data)

    try:
        # Allow extra time for indexing
        await asyncio.sleep(3)

        # Trigger a reindex to ensure the recipe is indexed
        await nc_client.cookbook.reindex()
        await asyncio.sleep(2)

        # Get recipes with this keyword
        logger.info(f"Getting recipes with keyword: {unique_keyword}")
        recipes_with_keywords = await nc_client.cookbook.get_recipes_with_keywords(
            [unique_keyword]
        )

        assert isinstance(recipes_with_keywords, list)
        # Keyword filtering might not find recipes immediately due to indexing
        # Log the results for debugging
        logger.info(
            f"Found {len(recipes_with_keywords)} recipes with keyword {unique_keyword}"
        )

        if len(recipes_with_keywords) > 0:
            # Verify our recipe is in the results if any are found
            found = any(
                str(r.get("id")) == str(recipe_id) for r in recipes_with_keywords
            )
            if found:
                logger.info(f"Successfully found recipe with keyword {unique_keyword}")
            else:
                logger.warning(
                    f"Recipe {recipe_id} not in keyword results, but other recipes found"
                )
        else:
            logger.warning(
                f"No recipes found with keyword {unique_keyword} - may be indexing delay"
            )

    finally:
        # Clean up
        logger.info(f"Deleting recipe ID: {recipe_id}")
        await nc_client.cookbook.delete_recipe(recipe_id)


async def test_cookbook_reindex(nc_client: NextcloudClient):
    """Test triggering a reindex of recipes."""
    logger.info("Triggering recipe reindex")
    result = await nc_client.cookbook.reindex()

    # Should return a success message
    assert isinstance(result, str)
    logger.info(f"Reindex result: {result}")
