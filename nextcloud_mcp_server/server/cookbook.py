import logging

from httpx import HTTPStatusError, RequestError
from mcp.server.fastmcp import Context, FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from nextcloud_mcp_server.auth import require_scopes
from nextcloud_mcp_server.context import get_client
from nextcloud_mcp_server.models.cookbook import (
    Category,
    CookbookConfig,
    CreateRecipeResponse,
    DeleteRecipeResponse,
    ImportRecipeResponse,
    Keyword,
    ListCategoriesResponse,
    ListKeywordsResponse,
    ListRecipesResponse,
    Recipe,
    RecipeStub,
    ReindexResponse,
    SearchRecipesResponse,
    UpdateRecipeResponse,
    Version,
)

logger = logging.getLogger(__name__)


def configure_cookbook_tools(mcp: FastMCP):
    @mcp.resource("cookbook://version")
    async def cookbook_get_version():
        """Get the Cookbook app and API version"""
        ctx: Context = mcp.get_context()
        client = get_client(ctx)
        version_data = await client.cookbook.get_version()
        return Version(**version_data)

    @mcp.resource("cookbook://config")
    async def cookbook_get_config():
        """Get the Cookbook app configuration"""
        ctx: Context = mcp.get_context()
        client = get_client(ctx)
        config_data = await client.cookbook.get_config()
        return CookbookConfig(**config_data)

    @mcp.resource("nc://Cookbook/{recipe_id}")
    async def nc_cookbook_get_recipe_resource(recipe_id: int):
        """Get a recipe by ID using resource URI"""
        ctx: Context = mcp.get_context()
        client = get_client(ctx)
        try:
            recipe_data = await client.cookbook.get_recipe(recipe_id)
            return Recipe(**recipe_data)
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(
                    ErrorData(code=-1, message=f"Recipe {recipe_id} not found")
                )
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(code=-1, message=f"Access denied to recipe {recipe_id}")
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to retrieve recipe {recipe_id}: {e.response.reason_phrase}",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:write")
    async def nc_cookbook_import_recipe(url: str, ctx: Context) -> ImportRecipeResponse:
        """Import a recipe from a URL using schema.org metadata.

        This extracts recipe data from websites that use schema.org Recipe markup.
        Many popular recipe sites support this standard."""
        client = get_client(ctx)
        try:
            recipe_data = await client.cookbook.import_recipe(url)
            recipe = Recipe(**recipe_data)
            return ImportRecipeResponse(
                recipe=recipe,
                recipe_id=recipe.id or "unknown",
            )
        except RequestError as e:
            # RequestError can have empty str() - get details from exception attributes
            error_detail = (
                str(e)
                or f"{type(e).__name__}: {getattr(e, '__cause__', 'unknown cause')}"
            )
            raise McpError(
                ErrorData(
                    code=-1,
                    message=f"Network error importing recipe from {url}: {error_detail}",
                )
            )
        except HTTPStatusError as e:
            if e.response.status_code == 400:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Invalid URL or missing 'url' field: {url}",
                    )
                )
            elif e.response.status_code == 409:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="A recipe with this name already exists. Import aborted.",
                    )
                )
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to import recipes",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to import recipe from {url}: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:read")
    async def nc_cookbook_list_recipes(ctx: Context) -> ListRecipesResponse:
        """Get all recipes in the database"""
        client = get_client(ctx)
        try:
            recipes_data = await client.cookbook.list_recipes()
            recipes = [RecipeStub(**r) for r in recipes_data]
            return ListRecipesResponse(recipes=recipes, total_count=len(recipes))
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to list recipes",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to list recipes: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:read")
    async def nc_cookbook_get_recipe(recipe_id: int, ctx: Context) -> Recipe:
        """Get a specific recipe by its ID"""
        client = get_client(ctx)
        try:
            recipe_data = await client.cookbook.get_recipe(recipe_id)
            return Recipe(**recipe_data)
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(
                    ErrorData(code=-1, message=f"Recipe {recipe_id} not found")
                )
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(code=-1, message=f"Access denied to recipe {recipe_id}")
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to retrieve recipe {recipe_id}: {e.response.reason_phrase}",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:write")
    async def nc_cookbook_create_recipe(
        name: str,
        description: str | None = None,
        ingredients: list[str] | None = None,
        instructions: list[str] | None = None,
        url: str | None = None,
        prep_time: str | None = None,
        cook_time: str | None = None,
        total_time: str | None = None,
        recipe_yield: int | None = None,
        category: str | None = None,
        keywords: str | None = None,
        ctx: Context = None,
    ) -> CreateRecipeResponse:
        """Create a new recipe.

        Required: name
        Optional: All other recipe fields following schema.org/Recipe format.

        Times should be in ISO8601 duration format (e.g., 'PT30M' for 30 minutes)."""
        client = get_client(ctx)

        recipe_data = {"name": name}
        if description:
            recipe_data["description"] = description
        if ingredients:
            recipe_data["recipeIngredient"] = ingredients
        if instructions:
            recipe_data["recipeInstructions"] = instructions
        if url:
            recipe_data["url"] = url
        if prep_time:
            recipe_data["prepTime"] = prep_time
        if cook_time:
            recipe_data["cookTime"] = cook_time
        if total_time:
            recipe_data["totalTime"] = total_time
        if recipe_yield:
            recipe_data["recipeYield"] = recipe_yield
        if category:
            recipe_data["recipeCategory"] = category
        if keywords:
            recipe_data["keywords"] = keywords

        try:
            recipe_id = await client.cookbook.create_recipe(recipe_data)
            return CreateRecipeResponse(id=recipe_id)
        except HTTPStatusError as e:
            if e.response.status_code == 409:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"A recipe with name '{name}' already exists",
                    )
                )
            elif e.response.status_code == 422:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Recipe name is required and cannot be empty",
                    )
                )
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to create recipes",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to create recipe: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:write")
    async def nc_cookbook_update_recipe(
        recipe_id: int,
        name: str | None = None,
        description: str | None = None,
        ingredients: list[str] | None = None,
        instructions: list[str] | None = None,
        url: str | None = None,
        prep_time: str | None = None,
        cook_time: str | None = None,
        total_time: str | None = None,
        recipe_yield: int | None = None,
        category: str | None = None,
        keywords: str | None = None,
        ctx: Context = None,
    ) -> UpdateRecipeResponse:
        """Update an existing recipe.

        Provide only the fields you want to update. Unspecified fields remain unchanged."""
        client = get_client(ctx)

        # First get the current recipe
        try:
            current_recipe = await client.cookbook.get_recipe(recipe_id)
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(
                    ErrorData(code=-1, message=f"Recipe {recipe_id} not found")
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to fetch recipe {recipe_id}: {e.response.reason_phrase}",
                    )
                )

        # Update only specified fields
        recipe_data = current_recipe.copy()
        if name is not None:
            recipe_data["name"] = name
        if description is not None:
            recipe_data["description"] = description
        if ingredients is not None:
            recipe_data["recipeIngredient"] = ingredients
        if instructions is not None:
            recipe_data["recipeInstructions"] = instructions
        if url is not None:
            recipe_data["url"] = url
        if prep_time is not None:
            recipe_data["prepTime"] = prep_time
        if cook_time is not None:
            recipe_data["cookTime"] = cook_time
        if total_time is not None:
            recipe_data["totalTime"] = total_time
        if recipe_yield is not None:
            recipe_data["recipeYield"] = recipe_yield
        if category is not None:
            recipe_data["recipeCategory"] = category
        if keywords is not None:
            recipe_data["keywords"] = keywords

        try:
            updated_id = await client.cookbook.update_recipe(recipe_id, recipe_data)
            return UpdateRecipeResponse(id=updated_id)
        except HTTPStatusError as e:
            if e.response.status_code == 422:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Recipe name is required and cannot be empty",
                    )
                )
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Access denied: insufficient permissions to update recipe {recipe_id}",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to update recipe {recipe_id}: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:write")
    async def nc_cookbook_delete_recipe(
        recipe_id: int, ctx: Context
    ) -> DeleteRecipeResponse:
        """Delete a recipe permanently"""
        logger.info("Deleting recipe %s", recipe_id)
        client = get_client(ctx)
        try:
            message = await client.cookbook.delete_recipe(recipe_id)
            return DeleteRecipeResponse(
                status_code=200,
                message=message,
                deleted_id=recipe_id,
            )
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise McpError(
                    ErrorData(code=-1, message=f"Recipe {recipe_id} not found")
                )
            elif e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Access denied: insufficient permissions to delete recipe {recipe_id}",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to delete recipe {recipe_id}: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:read")
    async def nc_cookbook_search_recipes(
        query: str, ctx: Context
    ) -> SearchRecipesResponse:
        """Search for recipes by keywords, tags, and categories"""
        client = get_client(ctx)
        try:
            recipes_data = await client.cookbook.search_recipes(query)
            recipes = [RecipeStub(**r) for r in recipes_data]
            return SearchRecipesResponse(
                recipes=recipes, query=query, total_found=len(recipes)
            )
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to search recipes",
                    )
                )
            elif e.response.status_code == 500:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Search failed: server error",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Search failed: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:read")
    async def nc_cookbook_list_categories(ctx: Context) -> ListCategoriesResponse:
        """Get all known categories.

        Note: A category name of '*' indicates recipes with no category."""
        client = get_client(ctx)
        try:
            categories_data = await client.cookbook.list_categories()
            categories = [Category(**c) for c in categories_data]
            return ListCategoriesResponse(categories=categories)
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to list categories",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to list categories: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:read")
    async def nc_cookbook_get_recipes_in_category(
        category: str, ctx: Context
    ) -> ListRecipesResponse:
        """Get all recipes in a specific category.

        Use '_' as the category name to get recipes with no category."""
        client = get_client(ctx)
        try:
            recipes_data = await client.cookbook.get_recipes_in_category(category)
            recipes = [RecipeStub(**r) for r in recipes_data]
            return ListRecipesResponse(recipes=recipes, total_count=len(recipes))
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to access recipes",
                    )
                )
            elif e.response.status_code == 500:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Could not find category '{category}'",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to get recipes in category: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:read")
    async def nc_cookbook_list_keywords(ctx: Context) -> ListKeywordsResponse:
        """Get all known keywords/tags"""
        client = get_client(ctx)
        try:
            keywords_data = await client.cookbook.list_keywords()
            keywords = [Keyword(**k) for k in keywords_data]
            return ListKeywordsResponse(keywords=keywords)
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to list keywords",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to list keywords: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:read")
    async def nc_cookbook_get_recipes_with_keywords(
        keywords: list[str], ctx: Context
    ) -> ListRecipesResponse:
        """Get all recipes that have specific keywords/tags"""
        client = get_client(ctx)
        try:
            recipes_data = await client.cookbook.get_recipes_with_keywords(keywords)
            recipes = [RecipeStub(**r) for r in recipes_data]
            return ListRecipesResponse(recipes=recipes, total_count=len(recipes))
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to access recipes",
                    )
                )
            elif e.response.status_code == 500:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Failed to get recipes with keywords: server error",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to get recipes with keywords: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:write")
    async def nc_cookbook_set_config(
        folder: str | None = None,
        update_interval: int | None = None,
        print_image: bool | None = None,
        ctx: Context = None,
    ) -> ReindexResponse:
        """Set Cookbook app configuration.

        Args:
            folder: Recipe folder path in user's files
            update_interval: Automatic rescan interval in minutes
            print_image: Whether to print images with recipes"""
        client = get_client(ctx)

        config_data = {}
        if folder is not None:
            config_data["folder"] = folder
        if update_interval is not None:
            config_data["update_interval"] = update_interval
        if print_image is not None:
            config_data["print_image"] = print_image

        try:
            result = await client.cookbook.set_config(config_data)
            return ReindexResponse(status_code=200, message=str(result))
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to set configuration",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to set configuration: server error ({e.response.status_code})",
                    )
                )

    @mcp.tool()
    @require_scopes("nc:write")
    async def nc_cookbook_reindex(ctx: Context) -> ReindexResponse:
        """Trigger a rescan of all recipes into the caching database.

        This rebuilds the search index and should be used after manual file changes."""
        client = get_client(ctx)
        try:
            message = await client.cookbook.reindex()
            return ReindexResponse(status_code=200, message=message)
        except HTTPStatusError as e:
            if e.response.status_code == 403:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message="Access denied: insufficient permissions to reindex",
                    )
                )
            else:
                raise McpError(
                    ErrorData(
                        code=-1,
                        message=f"Failed to reindex: server error ({e.response.status_code})",
                    )
                )
