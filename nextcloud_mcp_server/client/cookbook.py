"""Client for Nextcloud Cookbook app operations."""

import logging
from typing import Any, Dict, List

from httpx import Timeout

from .base import BaseNextcloudClient

logger = logging.getLogger(__name__)


class CookbookClient(BaseNextcloudClient):
    """Client for Nextcloud Cookbook app operations."""

    async def get_version(self) -> Dict[str, Any]:
        """Get Cookbook app and API version."""
        response = await self._make_request("GET", "/apps/cookbook/api/version")
        return response.json()

    async def get_config(self) -> Dict[str, Any]:
        """Get current Cookbook app configuration."""
        response = await self._make_request("GET", "/apps/cookbook/api/v1/config")
        return response.json()

    async def set_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Set Cookbook app configuration.

        Args:
            config: Configuration dictionary with fields like:
                - folder: Recipe folder path
                - update_interval: Auto-rescan interval in minutes
                - print_image: Whether to print images with recipes
                - visibleInfoBlocks: Visible info blocks configuration

        Returns:
            Response with status message
        """
        response = await self._make_request(
            "POST", "/apps/cookbook/api/v1/config", json=config
        )
        return response.json()

    async def reindex(self) -> str:
        """Trigger a rescan of all recipes into the caching database.

        Returns:
            Success message
        """
        response = await self._make_request("POST", "/apps/cookbook/api/v1/reindex")
        return response.json()

    async def list_recipes(self) -> List[Dict[str, Any]]:
        """Get all recipes in the database.

        Returns:
            List of recipe stubs with basic information
        """
        response = await self._make_request("GET", "/apps/cookbook/api/v1/recipes")
        return response.json()

    async def get_recipe(self, recipe_id: int) -> Dict[str, Any]:
        """Get a single recipe by ID.

        Args:
            recipe_id: The recipe ID

        Returns:
            Full recipe data
        """
        response = await self._make_request(
            "GET", f"/apps/cookbook/api/v1/recipes/{recipe_id}"
        )
        return response.json()

    async def create_recipe(self, recipe_data: Dict[str, Any]) -> int:
        """Create a new recipe.

        Args:
            recipe_data: Recipe data following schema.org/Recipe format.
                Required: name
                Optional: description, ingredients, instructions, etc.

        Returns:
            ID of the newly created recipe
        """
        response = await self._make_request(
            "POST", "/apps/cookbook/api/v1/recipes", json=recipe_data
        )
        return response.json()

    async def update_recipe(self, recipe_id: int, recipe_data: Dict[str, Any]) -> int:
        """Update an existing recipe.

        Args:
            recipe_id: The recipe ID to update
            recipe_data: Updated recipe data

        Returns:
            ID of the updated recipe
        """
        response = await self._make_request(
            "PUT", f"/apps/cookbook/api/v1/recipes/{recipe_id}", json=recipe_data
        )
        return response.json()

    async def delete_recipe(self, recipe_id: int) -> str:
        """Delete a recipe.

        Args:
            recipe_id: The recipe ID to delete

        Returns:
            Success message
        """
        response = await self._make_request(
            "DELETE", f"/apps/cookbook/api/v1/recipes/{recipe_id}"
        )
        return response.json()

    async def import_recipe(self, url: str) -> Dict[str, Any]:
        """Import a recipe from a URL using schema.org metadata.

        Args:
            url: URL of the recipe to import

        Returns:
            Full imported recipe data
        """
        logger.info(f"Importing recipe from URL: {url}")
        response = await self._make_request(
            "POST",
            "/apps/cookbook/api/v1/import",
            json={"url": url},
            timeout=Timeout(60.0),
        )
        return response.json()

    async def get_recipe_image(self, recipe_id: int, size: str = "full") -> bytes:
        """Get the main image of a recipe.

        Args:
            recipe_id: The recipe ID
            size: Image size - "full", "thumb" (250px), or "thumb16" (16px)

        Returns:
            Image bytes
        """
        response = await self._make_request(
            "GET",
            f"/apps/cookbook/api/v1/recipes/{recipe_id}/image",
            params={"size": size},
        )
        return response.content

    async def search_recipes(self, query: str) -> List[Dict[str, Any]]:
        """Search for recipes by keywords, tags, and categories.

        Args:
            query: Search string (URL-encoded, space/comma separated)

        Returns:
            List of matching recipe stubs
        """
        # URL encode the query
        from urllib.parse import quote

        encoded_query = quote(query)
        response = await self._make_request(
            "GET", f"/apps/cookbook/api/v1/search/{encoded_query}"
        )
        return response.json()

    async def list_categories(self) -> List[Dict[str, Any]]:
        """Get all known categories.

        Note: A category name of '*' indicates recipes with no category.

        Returns:
            List of categories with recipe counts
        """
        response = await self._make_request("GET", "/apps/cookbook/api/v1/categories")
        return response.json()

    async def get_recipes_in_category(self, category: str) -> List[Dict[str, Any]]:
        """Get all recipes in a specific category.

        Args:
            category: Category name (use "_" for recipes with no category)

        Returns:
            List of recipe stubs in the category
        """
        from urllib.parse import quote

        encoded_category = quote(category)
        response = await self._make_request(
            "GET", f"/apps/cookbook/api/v1/category/{encoded_category}"
        )
        return response.json()

    async def rename_category(self, old_name: str, new_name: str) -> str:
        """Rename a category.

        Args:
            old_name: Current category name
            new_name: New category name

        Returns:
            New category name
        """
        from urllib.parse import quote

        encoded_old_name = quote(old_name)
        response = await self._make_request(
            "PUT",
            f"/apps/cookbook/api/v1/category/{encoded_old_name}",
            json={"name": new_name},
        )
        return response.json()

    async def list_keywords(self) -> List[Dict[str, Any]]:
        """Get all known keywords/tags.

        Returns:
            List of keywords with recipe counts
        """
        response = await self._make_request("GET", "/apps/cookbook/api/v1/keywords")
        return response.json()

    async def get_recipes_with_keywords(
        self, keywords: List[str]
    ) -> List[Dict[str, Any]]:
        """Get all recipes associated with certain keywords.

        Args:
            keywords: List of keywords to filter by

        Returns:
            List of recipe stubs matching the keywords
        """
        from urllib.parse import quote

        # Join keywords with commas
        keywords_str = ",".join(keywords)
        encoded_keywords = quote(keywords_str)
        response = await self._make_request(
            "GET", f"/apps/cookbook/api/v1/tags/{encoded_keywords}"
        )
        return response.json()
