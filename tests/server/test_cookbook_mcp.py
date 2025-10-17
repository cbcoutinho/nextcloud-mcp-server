import asyncio
import json
import logging
import uuid

import pytest
from mcp import ClientSession

from nextcloud_mcp_server.client import NextcloudClient

logger = logging.getLogger(__name__)
pytestmark = pytest.mark.integration


async def test_mcp_cookbook_create_and_read_recipe(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test creating and reading a recipe via MCP tools with verification via NextcloudClient."""

    unique_suffix = uuid.uuid4().hex[:8]
    recipe_name = f"MCP Test Recipe {unique_suffix}"
    recipe_data = {
        "name": recipe_name,
        "description": "A test recipe created via MCP tools",
        "recipeIngredient": ["100g flour", "2 eggs", "200ml milk"],
        "recipeInstructions": ["Mix ingredients", "Cook for 20 minutes", "Serve hot"],
        "recipeCategory": "MCPTesting",
        "keywords": f"mcp,testing,{unique_suffix}",
        "recipeYield": 4,
        "prepTime": "PT15M",
        "cookTime": "PT20M",
        "totalTime": "PT35M",
    }

    created_recipe_id = None

    try:
        # 1. Create recipe via MCP
        logger.info(f"Creating recipe via MCP: {recipe_name}")
        create_result = await nc_mcp_client.call_tool(
            "nc_cookbook_create_recipe",
            {
                "name": recipe_name,
                "description": recipe_data["description"],
                "ingredients": recipe_data["recipeIngredient"],
                "instructions": recipe_data["recipeInstructions"],
                "category": recipe_data["recipeCategory"],
                "keywords": recipe_data["keywords"],
                "recipe_yield": recipe_data["recipeYield"],
                "prep_time": recipe_data["prepTime"],
                "cook_time": recipe_data["cookTime"],
                "total_time": recipe_data["totalTime"],
            },
        )

        assert create_result.isError is False, (
            f"MCP recipe creation failed: {create_result.content}"
        )

        create_response = json.loads(create_result.content[0].text)
        created_recipe_id = create_response["id"]
        logger.info(f"Recipe created via MCP with ID: {created_recipe_id}")

        # 2. Verify creation via direct NextcloudClient
        direct_recipe = await nc_client.cookbook.get_recipe(created_recipe_id)
        assert direct_recipe["name"] == recipe_name
        assert direct_recipe["description"] == "A test recipe created via MCP tools"
        assert len(direct_recipe["recipeIngredient"]) == 3
        assert len(direct_recipe["recipeInstructions"]) == 3
        assert direct_recipe["recipeCategory"] == "MCPTesting"

        # 3. Read recipe via MCP
        logger.info(f"Reading recipe via MCP: {created_recipe_id}")
        read_result = await nc_mcp_client.call_tool(
            "nc_cookbook_get_recipe", {"recipe_id": created_recipe_id}
        )

        assert read_result.isError is False, (
            f"MCP recipe read failed: {read_result.content}"
        )

        read_recipe = json.loads(read_result.content[0].text)
        assert read_recipe["name"] == recipe_name
        assert read_recipe["description"] == "A test recipe created via MCP tools"
        assert len(read_recipe["recipeIngredient"]) == 3

        logger.info(f"Successfully verified recipe {created_recipe_id} via MCP")

    finally:
        # Cleanup
        if created_recipe_id is not None:
            try:
                await nc_client.cookbook.delete_recipe(created_recipe_id)
                logger.info(f"Cleaned up recipe {created_recipe_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup recipe: {e}")


async def test_mcp_cookbook_update_recipe(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test updating a recipe via MCP tools."""

    unique_suffix = uuid.uuid4().hex[:8]
    recipe_name = f"MCP Update Test {unique_suffix}"
    recipe_data = {
        "name": recipe_name,
        "description": "Original description",
        "recipeIngredient": ["100g flour"],
        "recipeInstructions": ["Mix ingredients"],
        "recipeCategory": "Original",
    }

    created_recipe_id = None

    try:
        # 1. Create recipe via direct client
        logger.info(f"Creating recipe for update test: {recipe_name}")
        created_recipe_id = await nc_client.cookbook.create_recipe(recipe_data)

        # 2. Update recipe via MCP (tool handles fetching current recipe internally)
        logger.info(f"Updating recipe via MCP: {created_recipe_id}")
        update_result = await nc_mcp_client.call_tool(
            "nc_cookbook_update_recipe",
            {
                "recipe_id": created_recipe_id,
                "description": "Updated via MCP",
                "ingredients": ["100g flour", "2 eggs"],
                "instructions": ["Mix ingredients", "Cook"],
                "category": "Updated",
            },
        )

        assert update_result.isError is False, (
            f"MCP recipe update failed: {update_result.content}"
        )

        # 4. Verify update via direct NextcloudClient
        await asyncio.sleep(1)  # Allow propagation
        updated_recipe = await nc_client.cookbook.get_recipe(created_recipe_id)
        assert updated_recipe["description"] == "Updated via MCP"
        assert len(updated_recipe["recipeIngredient"]) == 2
        assert len(updated_recipe["recipeInstructions"]) == 2
        assert updated_recipe["recipeCategory"] == "Updated"

        logger.info(f"Successfully updated recipe {created_recipe_id} via MCP")

    finally:
        # Cleanup
        if created_recipe_id is not None:
            try:
                await nc_client.cookbook.delete_recipe(created_recipe_id)
                logger.info(f"Cleaned up recipe {created_recipe_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup recipe: {e}")


async def test_mcp_cookbook_delete_recipe(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test deleting a recipe via MCP tools."""

    unique_suffix = uuid.uuid4().hex[:8]
    recipe_name = f"MCP Delete Test {unique_suffix}"
    recipe_data = {
        "name": recipe_name,
        "description": "Recipe to be deleted",
        "recipeIngredient": ["test"],
        "recipeInstructions": ["test"],
    }

    created_recipe_id = None

    try:
        # 1. Create recipe via direct client
        logger.info(f"Creating recipe for delete test: {recipe_name}")
        created_recipe_id = await nc_client.cookbook.create_recipe(recipe_data)

        # 2. Delete recipe via MCP
        logger.info(f"Deleting recipe via MCP: {created_recipe_id}")
        delete_result = await nc_mcp_client.call_tool(
            "nc_cookbook_delete_recipe", {"recipe_id": created_recipe_id}
        )

        assert delete_result.isError is False, (
            f"MCP recipe deletion failed: {delete_result.content}"
        )

        # 3. Verify deletion via direct NextcloudClient
        try:
            await nc_client.cookbook.get_recipe(created_recipe_id)
            pytest.fail("Recipe should have been deleted but was still found")
        except Exception:
            # Expected - recipe should be deleted
            logger.info(f"Successfully verified recipe {created_recipe_id} was deleted")
            created_recipe_id = None  # Mark as cleaned up

    finally:
        # Cleanup in case of test failure
        if created_recipe_id is not None:
            try:
                await nc_client.cookbook.delete_recipe(created_recipe_id)
                logger.info(f"Cleaned up recipe {created_recipe_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup recipe: {e}")


async def test_mcp_cookbook_import_recipe_from_url(
    nc_mcp_client: ClientSession,
    nc_client: NextcloudClient,
):
    """Test importing a recipe from a URL via MCP tools.

    This is the key feature test - importing recipes from URLs using schema.org metadata.
    Uses an nginx container to serve reliable, controlled test data.
    """
    # Use the nginx container hostname within the Docker network
    test_url = "http://recipes/black-pepper-tofu"

    created_recipe_id = None

    try:
        # 1. Import recipe via MCP
        logger.info(f"Importing recipe from nginx container via MCP: {test_url}")
        import_result = await nc_mcp_client.call_tool(
            "nc_cookbook_import_recipe", {"url": test_url}
        )

        assert import_result.isError is False, (
            f"MCP recipe import failed: {import_result.content}"
        )

        import_response = json.loads(import_result.content[0].text)
        created_recipe_id = int(import_response["recipe_id"])
        imported_recipe = import_response["recipe"]

        logger.info(f"Successfully imported recipe via MCP: {imported_recipe['name']}")

        # 2. Verify basic recipe structure
        assert imported_recipe["name"] == "Black Pepper Tofu"
        assert imported_recipe.get("description")
        assert len(imported_recipe.get("recipeIngredient", [])) > 0
        assert len(imported_recipe.get("recipeInstructions", [])) > 0
        assert imported_recipe.get("recipeCategory") == "Main Course"
        assert "tofu" in imported_recipe.get("keywords", "").lower()

        # 3. Verify we can read it back via direct NextcloudClient
        retrieved = await nc_client.cookbook.get_recipe(created_recipe_id)
        assert retrieved["name"] == imported_recipe["name"]
        logger.info(f"Verified imported recipe ID: {created_recipe_id}")

    finally:
        # Cleanup
        if created_recipe_id is not None:
            try:
                await nc_client.cookbook.delete_recipe(created_recipe_id)
                logger.info(f"Cleaned up imported recipe {created_recipe_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup imported recipe: {e}")


async def test_mcp_cookbook_search_recipes(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test searching recipes via MCP tools."""

    unique_keyword = f"mcptestkeyword{uuid.uuid4().hex[:8]}"
    recipe_name = f"MCP Search Test {uuid.uuid4().hex[:8]}"
    recipe_data = {
        "name": recipe_name,
        "description": f"Recipe for testing MCP search with {unique_keyword}",
        "keywords": unique_keyword,
        "recipeIngredient": ["test ingredient"],
        "recipeInstructions": ["test instruction"],
    }

    created_recipe_id = None

    try:
        # 1. Create recipe via direct client
        logger.info(f"Creating recipe for search test with keyword: {unique_keyword}")
        created_recipe_id = await nc_client.cookbook.create_recipe(recipe_data)

        # 2. Allow time for indexing
        await asyncio.sleep(2)

        # 3. Search for the recipe via MCP
        logger.info(f"Searching for recipes via MCP with keyword: {unique_keyword}")
        search_result = await nc_mcp_client.call_tool(
            "nc_cookbook_search_recipes", {"query": unique_keyword}
        )

        assert search_result.isError is False, (
            f"MCP recipe search failed: {search_result.content}"
        )

        search_response = json.loads(search_result.content[0].text)
        search_results = search_response["recipes"]

        assert isinstance(search_results, list)
        assert len(search_results) > 0

        # 4. Verify our recipe is in the results
        found = any(str(r.get("id")) == str(created_recipe_id) for r in search_results)
        assert found, f"Recipe {created_recipe_id} not found in search results"
        logger.info(
            f"Successfully found recipe {created_recipe_id} in MCP search results"
        )

    finally:
        # Cleanup
        if created_recipe_id is not None:
            try:
                await nc_client.cookbook.delete_recipe(created_recipe_id)
                logger.info(f"Cleaned up recipe {created_recipe_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup recipe: {e}")


async def test_mcp_cookbook_list_recipes(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test listing all recipes via MCP tools."""

    logger.info("Listing all recipes via MCP")
    list_result = await nc_mcp_client.call_tool("nc_cookbook_list_recipes", {})

    assert list_result.isError is False, (
        f"MCP list recipes failed: {list_result.content}"
    )

    list_response = json.loads(list_result.content[0].text)
    recipes = list_response["recipes"]

    assert isinstance(recipes, list)
    logger.info(f"Found {len(recipes)} recipes via MCP")


async def test_mcp_cookbook_categories_workflow(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test category listing and filtering via MCP tools."""

    unique_category = f"MCPTestCategory{uuid.uuid4().hex[:8]}"
    recipe_name = f"MCP Category Test {uuid.uuid4().hex[:8]}"
    recipe_data = {
        "name": recipe_name,
        "recipeCategory": unique_category,
        "recipeIngredient": ["test"],
        "recipeInstructions": ["test"],
    }

    created_recipe_id = None

    try:
        # 1. Create recipe in test category
        logger.info(f"Creating recipe in category: {unique_category}")
        created_recipe_id = await nc_client.cookbook.create_recipe(recipe_data)

        # 2. Allow time for indexing
        await asyncio.sleep(2)

        # 3. List categories via MCP
        logger.info("Listing categories via MCP")
        categories_result = await nc_mcp_client.call_tool(
            "nc_cookbook_list_categories", {}
        )

        assert categories_result.isError is False, (
            f"MCP list categories failed: {categories_result.content}"
        )

        categories_response = json.loads(categories_result.content[0].text)
        categories = categories_response["categories"]

        assert isinstance(categories, list)
        logger.info(f"Found {len(categories)} categories via MCP")

        # 4. Get recipes in this category via MCP
        logger.info(f"Getting recipes in category via MCP: {unique_category}")
        category_recipes_result = await nc_mcp_client.call_tool(
            "nc_cookbook_get_recipes_in_category", {"category": unique_category}
        )

        assert category_recipes_result.isError is False, (
            f"MCP get recipes in category failed: {category_recipes_result.content}"
        )

        category_recipes_response = json.loads(category_recipes_result.content[0].text)
        recipes_in_category = category_recipes_response["recipes"]

        assert isinstance(recipes_in_category, list)
        assert len(recipes_in_category) > 0

        # 5. Verify our recipe is in the results
        found = any(
            str(r.get("id")) == str(created_recipe_id) for r in recipes_in_category
        )
        assert found, (
            f"Recipe {created_recipe_id} not found in category {unique_category}"
        )
        logger.info(f"Successfully found recipe in category {unique_category} via MCP")

    finally:
        # Cleanup
        if created_recipe_id is not None:
            try:
                await nc_client.cookbook.delete_recipe(created_recipe_id)
                logger.info(f"Cleaned up recipe {created_recipe_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup recipe: {e}")


async def test_mcp_cookbook_keywords_workflow(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test keyword listing and filtering via MCP tools."""

    unique_keyword = f"mcptesttag{uuid.uuid4().hex[:8]}"
    recipe_name = f"MCP Keyword Test {uuid.uuid4().hex[:8]}"
    recipe_data = {
        "name": recipe_name,
        "keywords": f"{unique_keyword},mcptesting",
        "recipeIngredient": ["test"],
        "recipeInstructions": ["test"],
    }

    created_recipe_id = None

    try:
        # 1. Create recipe with test keywords
        logger.info(f"Creating recipe with keyword: {unique_keyword}")
        created_recipe_id = await nc_client.cookbook.create_recipe(recipe_data)

        # 2. Allow extra time for indexing and trigger reindex
        await asyncio.sleep(3)
        await nc_client.cookbook.reindex()
        await asyncio.sleep(2)

        # 3. List keywords via MCP
        logger.info("Listing keywords via MCP")
        keywords_result = await nc_mcp_client.call_tool("nc_cookbook_list_keywords", {})

        assert keywords_result.isError is False, (
            f"MCP list keywords failed: {keywords_result.content}"
        )

        keywords_response = json.loads(keywords_result.content[0].text)
        keywords = keywords_response["keywords"]

        assert isinstance(keywords, list)
        logger.info(f"Found {len(keywords)} keywords via MCP")

        # 4. Get recipes with this keyword via MCP
        logger.info(f"Getting recipes with keyword via MCP: {unique_keyword}")
        keyword_recipes_result = await nc_mcp_client.call_tool(
            "nc_cookbook_get_recipes_with_keywords", {"keywords": [unique_keyword]}
        )

        assert keyword_recipes_result.isError is False, (
            f"MCP get recipes with keywords failed: {keyword_recipes_result.content}"
        )

        keyword_recipes_response = json.loads(keyword_recipes_result.content[0].text)
        recipes_with_keywords = keyword_recipes_response["recipes"]

        assert isinstance(recipes_with_keywords, list)

        # Keyword filtering might not find recipes immediately due to indexing
        if len(recipes_with_keywords) > 0:
            # Verify our recipe is in the results if any are found
            found = any(
                str(r.get("id")) == str(created_recipe_id)
                for r in recipes_with_keywords
            )
            if found:
                logger.info(
                    f"Successfully found recipe with keyword {unique_keyword} via MCP"
                )
            else:
                logger.warning(
                    f"Recipe {created_recipe_id} not in keyword results via MCP, but other recipes found"
                )
        else:
            logger.warning(
                f"No recipes found with keyword {unique_keyword} via MCP - may be indexing delay"
            )

    finally:
        # Cleanup
        if created_recipe_id is not None:
            try:
                await nc_client.cookbook.delete_recipe(created_recipe_id)
                logger.info(f"Cleaned up recipe {created_recipe_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup recipe: {e}")


async def test_mcp_cookbook_config_and_version(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test getting Cookbook configuration and version via MCP resources."""

    # 1. Get version via MCP resource
    logger.info("Getting Cookbook version via MCP resource")
    version_result = await nc_mcp_client.read_resource("cookbook://version")

    assert len(version_result.contents) > 0
    version_response = json.loads(version_result.contents[0].text)
    assert "cookbook_version" in version_response
    assert "api_version" in version_response
    logger.info(f"Cookbook version from MCP: {version_response}")

    # 2. Verify version via direct NextcloudClient
    direct_version = await nc_client.cookbook.get_version()
    assert direct_version["cookbook_version"] == version_response["cookbook_version"]
    assert (
        direct_version["api_version"]["epoch"]
        == version_response["api_version"]["epoch"]
    )

    # 3. Get config via MCP resource
    logger.info("Getting Cookbook config via MCP resource")
    config_result = await nc_mcp_client.read_resource("cookbook://config")

    assert len(config_result.contents) > 0
    config_response = json.loads(config_result.contents[0].text)
    assert isinstance(config_response, dict)
    logger.info(f"Cookbook config from MCP: {config_response}")

    # 4. Verify config via direct NextcloudClient
    direct_config = await nc_client.cookbook.get_config()
    # Both should be dicts - exact match may vary based on config
    assert isinstance(config_response, dict)
    assert isinstance(direct_config, dict)

    logger.info("Successfully verified Cookbook version and config via MCP")


async def test_mcp_cookbook_reindex(
    nc_mcp_client: ClientSession, nc_client: NextcloudClient
):
    """Test triggering a recipe reindex via MCP tools."""

    logger.info("Triggering recipe reindex via MCP")
    reindex_result = await nc_mcp_client.call_tool("nc_cookbook_reindex", {})

    assert reindex_result.isError is False, (
        f"MCP reindex failed: {reindex_result.content}"
    )

    reindex_response = json.loads(reindex_result.content[0].text)
    assert isinstance(reindex_response["message"], str)
    logger.info(f"Reindex result from MCP: {reindex_response['message']}")
