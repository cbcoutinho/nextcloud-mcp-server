import logging
from typing import Any, Callable, NoReturn

from httpx import HTTPStatusError, RequestError
from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations

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
from nextcloud_mcp_server.observability.metrics import instrument_tool
from nextcloud_mcp_server.request_context import current_context

logger = logging.getLogger(__name__)

#: MCP argument name -> the schema.org/Recipe field Cookbook expects.
_RECIPE_FIELDS: dict[str, str] = {
    "name": "name",
    "description": "description",
    "ingredients": "recipeIngredient",
    "instructions": "recipeInstructions",
    "url": "url",
    "prep_time": "prepTime",
    "cook_time": "cookTime",
    "total_time": "totalTime",
    "recipe_yield": "recipeYield",
    "category": "recipeCategory",
    "keywords": "keywords",
}


def _recipe_payload(*, keep: Callable[[Any], bool], **fields: Any) -> dict[str, Any]:
    """Map MCP arguments onto schema.org keys, keeping those ``keep`` accepts.

    Create and update pass different predicates, and the difference is
    deliberate: create keeps only truthy values, so an omitted optional is
    simply not sent, while update keeps anything that is not ``None``, so
    passing an empty string *clears* a field rather than being ignored.
    """
    return {_RECIPE_FIELDS[arg]: value for arg, value in fields.items() if keep(value)}


def _raise_recipe_error(
    exc: HTTPStatusError, by_status: dict[int, str], fallback: str
) -> NoReturn:
    """Re-raise a Cookbook HTTP failure as the message the model should act on.

    A dict beats an if/elif chain here only because the branches differ in
    nothing but the status they match -- keep it a chain if a case ever needs
    real logic.
    """
    raise MCPError(code=-1, message=by_status.get(exc.response.status_code, fallback))


@require_scopes("cookbook.write")
@instrument_tool
async def nc_cookbook_import_recipe(url: str, ctx: Context) -> ImportRecipeResponse:
    """Import a recipe from a URL using schema.org metadata.

    This extracts recipe data from websites that use schema.org Recipe markup.
    Many popular recipe sites support this standard."""
    client = await get_client(ctx)
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
            str(e) or f"{type(e).__name__}: {getattr(e, '__cause__', 'unknown cause')}"
        )
        raise MCPError(
            code=-1,
            message=f"Network error importing recipe from {url}: {error_detail}",
        )
    except HTTPStatusError as e:
        if e.response.status_code == 400:
            raise MCPError(
                code=-1,
                message=f"Invalid URL or missing 'url' field: {url}",
            )
        elif e.response.status_code == 409:
            raise MCPError(
                code=-1,
                message="A recipe with this name already exists. Import aborted.",
            )
        elif e.response.status_code == 403:
            raise MCPError(
                code=-1,
                message="Access denied: insufficient permissions to import recipes",
            )
        else:
            raise MCPError(
                code=-1,
                message=f"Failed to import recipe from {url}: server error ({e.response.status_code})",
            )


@require_scopes("cookbook.read")
@instrument_tool
async def nc_cookbook_list_recipes(ctx: Context) -> ListRecipesResponse:
    """Get all recipes in the database"""
    client = await get_client(ctx)
    try:
        recipes_data = await client.cookbook.list_recipes()
        recipes = [RecipeStub(**r) for r in recipes_data]
        return ListRecipesResponse(recipes=recipes, total_count=len(recipes))
    except HTTPStatusError as e:
        if e.response.status_code == 403:
            raise MCPError(
                code=-1,
                message="Access denied: insufficient permissions to list recipes",
            )
        else:
            raise MCPError(
                code=-1,
                message=f"Failed to list recipes: server error ({e.response.status_code})",
            )


@require_scopes("cookbook.read")
@instrument_tool
async def nc_cookbook_get_recipe(recipe_id: int, ctx: Context) -> Recipe:
    """Get a specific recipe by its ID"""
    client = await get_client(ctx)
    try:
        recipe_data = await client.cookbook.get_recipe(recipe_id)
        return Recipe(**recipe_data)
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            raise MCPError(code=-1, message=f"Recipe {recipe_id} not found")
        elif e.response.status_code == 403:
            raise MCPError(code=-1, message=f"Access denied to recipe {recipe_id}")
        else:
            raise MCPError(
                code=-1,
                message=f"Failed to retrieve recipe {recipe_id}: {e.response.reason_phrase}",
            )


@require_scopes("cookbook.write")
@instrument_tool
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
    ctx: Context = None,  # type: ignore
) -> CreateRecipeResponse:
    """Create a new recipe.

    Required: name
    Optional: All other recipe fields following schema.org/Recipe format.

    Times should be in ISO8601 duration format (e.g., 'PT30M' for 30 minutes)."""
    client = await get_client(ctx)

    # ``name`` is set unconditionally: it is required, and letting the predicate
    # drop an empty one would send a payload Cookbook cannot fault clearly.
    recipe_data = {
        "name": name,
        **_recipe_payload(
            keep=bool,
            description=description,
            ingredients=ingredients,
            instructions=instructions,
            url=url,
            prep_time=prep_time,
            cook_time=cook_time,
            total_time=total_time,
            recipe_yield=recipe_yield,
            category=category,
            keywords=keywords,
        ),
    }

    try:
        recipe_id = await client.cookbook.create_recipe(recipe_data)
        return CreateRecipeResponse(id=recipe_id)
    except HTTPStatusError as e:
        _raise_recipe_error(
            e,
            {
                409: f"A recipe with name '{name}' already exists",
                422: "Recipe name is required and cannot be empty",
                403: "Access denied: insufficient permissions to create recipes",
            },
            f"Failed to create recipe: server error ({e.response.status_code})",
        )


@require_scopes("cookbook.write")
@instrument_tool
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
    ctx: Context = None,  # type: ignore
) -> UpdateRecipeResponse:
    """Update an existing recipe.

    Provide only the fields you want to update. Unspecified fields remain unchanged."""
    client = await get_client(ctx)

    # First get the current recipe
    try:
        current_recipe = await client.cookbook.get_recipe(recipe_id)
    except HTTPStatusError as e:
        _raise_recipe_error(
            e,
            {404: f"Recipe {recipe_id} not found"},
            f"Failed to fetch recipe {recipe_id}: {e.response.reason_phrase}",
        )

    # Update only specified fields. ``is not None`` rather than truthiness, so
    # an explicit "" or 0 clears a field instead of being silently ignored.
    recipe_data = {
        **current_recipe,
        **_recipe_payload(
            keep=lambda value: value is not None,
            name=name,
            description=description,
            ingredients=ingredients,
            instructions=instructions,
            url=url,
            prep_time=prep_time,
            cook_time=cook_time,
            total_time=total_time,
            recipe_yield=recipe_yield,
            category=category,
            keywords=keywords,
        ),
    }

    try:
        updated_id = await client.cookbook.update_recipe(recipe_id, recipe_data)
        return UpdateRecipeResponse(id=updated_id)
    except HTTPStatusError as e:
        _raise_recipe_error(
            e,
            {
                422: "Recipe name is required and cannot be empty",
                403: f"Access denied: insufficient permissions to update recipe {recipe_id}",
            },
            f"Failed to update recipe {recipe_id}: server error ({e.response.status_code})",
        )


@require_scopes("cookbook.write")
@instrument_tool
async def nc_cookbook_delete_recipe(
    recipe_id: int, ctx: Context
) -> DeleteRecipeResponse:
    """Delete a recipe permanently"""
    logger.info("Deleting recipe %s", recipe_id)
    client = await get_client(ctx)
    try:
        message = await client.cookbook.delete_recipe(recipe_id)
        return DeleteRecipeResponse(
            status_code=200,
            message=message,
            deleted_id=recipe_id,
        )
    except HTTPStatusError as e:
        if e.response.status_code == 404:
            raise MCPError(code=-1, message=f"Recipe {recipe_id} not found")
        elif e.response.status_code == 403:
            raise MCPError(
                code=-1,
                message=f"Access denied: insufficient permissions to delete recipe {recipe_id}",
            )
        else:
            raise MCPError(
                code=-1,
                message=f"Failed to delete recipe {recipe_id}: server error ({e.response.status_code})",
            )


@require_scopes("cookbook.read")
@instrument_tool
async def nc_cookbook_search_recipes(query: str, ctx: Context) -> SearchRecipesResponse:
    """Search for recipes by keywords, tags, and categories"""
    client = await get_client(ctx)
    try:
        recipes_data = await client.cookbook.search_recipes(query)
        recipes = [RecipeStub(**r) for r in recipes_data]
        return SearchRecipesResponse(
            recipes=recipes, query=query, total_found=len(recipes)
        )
    except HTTPStatusError as e:
        if e.response.status_code == 403:
            raise MCPError(
                code=-1,
                message="Access denied: insufficient permissions to search recipes",
            )
        elif e.response.status_code == 500:
            raise MCPError(
                code=-1,
                message="Search failed: server error",
            )
        else:
            raise MCPError(
                code=-1,
                message=f"Search failed: server error ({e.response.status_code})",
            )


@require_scopes("cookbook.read")
@instrument_tool
async def nc_cookbook_list_categories(ctx: Context) -> ListCategoriesResponse:
    """Get all known categories.

    Note: A category name of '*' indicates recipes with no category."""
    client = await get_client(ctx)
    try:
        categories_data = await client.cookbook.list_categories()
        categories = [Category(**c) for c in categories_data]
        return ListCategoriesResponse(categories=categories)
    except HTTPStatusError as e:
        if e.response.status_code == 403:
            raise MCPError(
                code=-1,
                message="Access denied: insufficient permissions to list categories",
            )
        else:
            raise MCPError(
                code=-1,
                message=f"Failed to list categories: server error ({e.response.status_code})",
            )


@require_scopes("cookbook.read")
@instrument_tool
async def nc_cookbook_get_recipes_in_category(
    category: str, ctx: Context
) -> ListRecipesResponse:
    """Get all recipes in a specific category.

    Use '_' as the category name to get recipes with no category."""
    client = await get_client(ctx)
    try:
        recipes_data = await client.cookbook.get_recipes_in_category(category)
        recipes = [RecipeStub(**r) for r in recipes_data]
        return ListRecipesResponse(recipes=recipes, total_count=len(recipes))
    except HTTPStatusError as e:
        if e.response.status_code == 403:
            raise MCPError(
                code=-1,
                message="Access denied: insufficient permissions to access recipes",
            )
        elif e.response.status_code == 500:
            raise MCPError(
                code=-1,
                message=f"Could not find category '{category}'",
            )
        else:
            raise MCPError(
                code=-1,
                message=f"Failed to get recipes in category: server error ({e.response.status_code})",
            )


@require_scopes("cookbook.read")
@instrument_tool
async def nc_cookbook_list_keywords(ctx: Context) -> ListKeywordsResponse:
    """Get all known keywords/tags"""
    client = await get_client(ctx)
    try:
        keywords_data = await client.cookbook.list_keywords()
        keywords = [Keyword(**k) for k in keywords_data]
        return ListKeywordsResponse(keywords=keywords)
    except HTTPStatusError as e:
        if e.response.status_code == 403:
            raise MCPError(
                code=-1,
                message="Access denied: insufficient permissions to list keywords",
            )
        else:
            raise MCPError(
                code=-1,
                message=f"Failed to list keywords: server error ({e.response.status_code})",
            )


@require_scopes("cookbook.read")
@instrument_tool
async def nc_cookbook_get_recipes_with_keywords(
    keywords: list[str], ctx: Context
) -> ListRecipesResponse:
    """Get all recipes that have specific keywords/tags"""
    client = await get_client(ctx)
    try:
        recipes_data = await client.cookbook.get_recipes_with_keywords(keywords)
        recipes = [RecipeStub(**r) for r in recipes_data]
        return ListRecipesResponse(recipes=recipes, total_count=len(recipes))
    except HTTPStatusError as e:
        if e.response.status_code == 403:
            raise MCPError(
                code=-1,
                message="Access denied: insufficient permissions to access recipes",
            )
        elif e.response.status_code == 500:
            raise MCPError(
                code=-1,
                message="Failed to get recipes with keywords: server error",
            )
        else:
            raise MCPError(
                code=-1,
                message=f"Failed to get recipes with keywords: server error ({e.response.status_code})",
            )


@require_scopes("cookbook.write")
@instrument_tool
async def nc_cookbook_set_config(
    folder: str | None = None,
    update_interval: int | None = None,
    print_image: bool | None = None,
    ctx: Context = None,  # type: ignore
) -> ReindexResponse:
    """Set Cookbook app configuration.

    Args:
        folder: Recipe folder path in user's files
        update_interval: Automatic rescan interval in minutes
        print_image: Whether to print images with recipes"""
    client = await get_client(ctx)

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
            raise MCPError(
                code=-1,
                message="Access denied: insufficient permissions to set configuration",
            )
        else:
            raise MCPError(
                code=-1,
                message=f"Failed to set configuration: server error ({e.response.status_code})",
            )


@require_scopes("cookbook.write")
@instrument_tool
async def nc_cookbook_reindex(ctx: Context) -> ReindexResponse:
    """Trigger a rescan of all recipes into the caching database.

    This rebuilds the search index and should be used after manual file changes."""
    client = await get_client(ctx)
    try:
        message = await client.cookbook.reindex()
        return ReindexResponse(status_code=200, message=message)
    except HTTPStatusError as e:
        if e.response.status_code == 403:
            raise MCPError(
                code=-1,
                message="Access denied: insufficient permissions to reindex",
            )
        else:
            raise MCPError(
                code=-1,
                message=f"Failed to reindex: server error ({e.response.status_code})",
            )


def configure_cookbook_tools(mcp: MCPServer):
    @mcp.resource("cookbook://version")
    async def cookbook_get_version():
        """Get the Cookbook app and API version"""
        ctx = current_context(mcp)
        client = await get_client(ctx)
        version_data = await client.cookbook.get_version()
        return Version(**version_data)

    @mcp.resource("cookbook://config")
    async def cookbook_get_config():
        """Get the Cookbook app configuration"""
        ctx = current_context(mcp)
        client = await get_client(ctx)
        config_data = await client.cookbook.get_config()
        return CookbookConfig(**config_data)

    @mcp.resource("nc://Cookbook/{recipe_id}")
    async def nc_cookbook_get_recipe_resource(recipe_id: int):
        """Get a recipe by ID using resource URI"""
        ctx = current_context(mcp)
        client = await get_client(ctx)
        try:
            recipe_data = await client.cookbook.get_recipe(recipe_id)
            return Recipe(**recipe_data)
        except HTTPStatusError as e:
            if e.response.status_code == 404:
                raise MCPError(code=-1, message=f"Recipe {recipe_id} not found")
            elif e.response.status_code == 403:
                raise MCPError(code=-1, message=f"Access denied to recipe {recipe_id}")
            else:
                raise MCPError(
                    code=-1,
                    message=f"Failed to retrieve recipe {recipe_id}: {e.response.reason_phrase}",
                )

    mcp.tool(
        title="Import Recipe from URL",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )(nc_cookbook_import_recipe)

    mcp.tool(
        title="List Recipes",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_cookbook_list_recipes)

    mcp.tool(
        title="Get Recipe",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_cookbook_get_recipe)

    mcp.tool(
        title="Create Recipe",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )(nc_cookbook_create_recipe)

    mcp.tool(
        title="Update Recipe",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )(nc_cookbook_update_recipe)

    mcp.tool(
        title="Delete Recipe",
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=True, open_world_hint=True
        ),
    )(nc_cookbook_delete_recipe)

    mcp.tool(
        title="Search Recipes",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_cookbook_search_recipes)

    mcp.tool(
        title="List Recipe Categories",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_cookbook_list_categories)

    mcp.tool(
        title="Get Recipes in Category",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_cookbook_get_recipes_in_category)

    mcp.tool(
        title="List Recipe Keywords",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_cookbook_list_keywords)

    mcp.tool(
        title="Get Recipes with Keywords",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )(nc_cookbook_get_recipes_with_keywords)

    mcp.tool(
        title="Set Cookbook Configuration",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )(nc_cookbook_set_config)

    mcp.tool(
        title="Reindex Recipes",
        annotations=ToolAnnotations(idempotent_hint=False, open_world_hint=True),
    )(nc_cookbook_reindex)
