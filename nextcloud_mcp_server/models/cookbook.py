"""Pydantic models for Cookbook app responses."""

from typing import Any, List, Optional, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from .base import BaseResponse, IdResponse, StatusResponse


def _none_replacement(field: FieldInfo) -> Any:
    """Best stand-in for an explicit ``null`` where the type forbids None.

    Prefers the field's own default/default_factory. A field with neither
    (a required field, e.g. ``name``) falls back to a type-appropriate
    empty value instead of leaving None in place - Pydantic would reject
    that anyway, and an empty string/list is a more useful failure than a
    hard error over a single missing field.
    """
    default = field.get_default(call_default_factory=True)
    if default is not PydanticUndefined:
        return default
    annotation = field.annotation
    if annotation is str:
        return ""
    if annotation is int:
        return 0
    if annotation is bool:
        return False
    if get_origin(annotation) in (list, List):
        return []
    return None


class Nutrition(BaseModel):
    """Nutrition information following schema.org/NutritionInformation."""

    type: str = Field(
        default="NutritionInformation",
        alias="@type",
        description="Schema.org object type",
    )
    calories: Optional[str] = Field(None, description="Calories (e.g., '650 kcal')")
    carbohydrateContent: Optional[str] = Field(
        None, description="Carbohydrates (e.g., '300 g')"
    )
    cholesterolContent: Optional[str] = Field(
        None, description="Cholesterol (e.g., '10 g')"
    )
    fatContent: Optional[str] = Field(None, description="Fat (e.g., '45 g')")
    fiberContent: Optional[str] = Field(None, description="Fiber (e.g., '50 g')")
    proteinContent: Optional[str] = Field(None, description="Protein (e.g., '80 g')")
    saturatedFatContent: Optional[str] = Field(
        None, description="Saturated fat (e.g., '5 g')"
    )
    servingSize: Optional[str] = Field(
        None, description="Serving size description (e.g., 'One plate')"
    )
    sodiumContent: Optional[str] = Field(None, description="Sodium (e.g., '10 mg')")
    sugarContent: Optional[str] = Field(None, description="Sugar (e.g., '5 g')")
    transFatContent: Optional[str] = Field(None, description="Trans fat (e.g., '10 g')")
    unsaturatedFatContent: Optional[str] = Field(
        None, description="Unsaturated fat (e.g., '40 g')"
    )

    model_config = ConfigDict(populate_by_name=True)

    @field_validator(
        "calories",
        "carbohydrateContent",
        "cholesterolContent",
        "fatContent",
        "fiberContent",
        "proteinContent",
        "saturatedFatContent",
        "servingSize",
        "sodiumContent",
        "sugarContent",
        "transFatContent",
        "unsaturatedFatContent",
        mode="before",
    )
    @classmethod
    def coerce_to_str(cls, v: object) -> object:
        """Coerce numeric values to strings.

        The schema.org/NutritionInformation spec allows values as either
        strings (e.g. '650 kcal') or numbers (e.g. 650). Nextcloud Cookbook
        stores whatever the source provided.
        """
        if isinstance(v, bool):
            msg = "boolean values are not valid for nutrition fields"
            raise ValueError(msg)
        if isinstance(v, (int, float)):
            return str(v)
        return v


class RecipeStub(BaseModel):
    """Stub of a recipe with basic information."""

    id: str = Field(description="Recipe ID as string")
    recipe_id: int = Field(description="Recipe ID as integer (deprecated)")
    name: str = Field(description="Recipe name")
    keywords: Optional[str] = Field(default="", description="Comma-separated keywords")
    dateCreated: str = Field(description="Creation date (ISO8601)")
    dateModified: Optional[str] = Field(
        None, description="Last modified date (ISO8601)"
    )
    imageUrl: str = Field(default="", description="URL of the recipe image")
    imagePlaceholderUrl: str = Field(default="", description="URL of placeholder image")


class Recipe(BaseModel):
    """Full recipe following schema.org/Recipe specification."""

    type: str = Field(default="Recipe", alias="@type", description="Schema.org type")
    id: Optional[str] = Field(None, description="Recipe ID")
    name: str = Field(description="Recipe name")
    description: str = Field(default="", description="Recipe description")
    url: str = Field(default="", description="Original recipe URL")
    image: str = Field(default="", description="URL of original recipe image")
    imageUrl: Optional[str] = Field(
        None, description="URL of the recipe image in Nextcloud"
    )
    imagePlaceholderUrl: Optional[str] = Field(
        None, description="URL of placeholder image"
    )
    keywords: str = Field(default="", description="Comma-separated keywords")
    dateCreated: Optional[str] = Field(None, description="Creation date (ISO8601)")
    dateModified: Optional[str] = Field(
        None, description="Last modified date (ISO8601)"
    )
    prepTime: Optional[str] = Field(None, description="Preparation time (ISO8601)")
    cookTime: Optional[str] = Field(None, description="Cooking time (ISO8601)")
    totalTime: Optional[str] = Field(None, description="Total time (ISO8601)")
    recipeYield: Union[int, str] = Field(default=1, description="Number of servings")
    recipeCategory: str = Field(default="", description="Recipe category")
    tool: List[str] = Field(default_factory=list, description="Required tools")
    recipeIngredient: List[str] = Field(
        default_factory=list, description="List of ingredients"
    )
    recipeInstructions: List[str] = Field(
        default_factory=list, description="Cooking instructions"
    )
    nutrition: Optional[Nutrition] = Field(None, description="Nutrition information")

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    # --- Lokaler Patch: null-Felder abfedern --------------------------------
    # Nextcloud Cookbook speichert ein fehlendes Feld manchmal als JSON
    # `null` statt es einfach wegzulassen - live beobachtet an einem Rezept
    # ohne Portionsangabe: {"recipeYield": null, ...}. Pydantic wendet den
    # Default eines Feldes nur an, wenn der Schluessel ganz FEHLT; ein
    # explizites `null` muss trotzdem den deklarierten Typ erfuellen und
    # scheitert:
    #
    #   2 validation errors for Recipe
    #   recipeYield.int   Input should be a valid integer [input_value=None]
    #   recipeYield.str   Input should be a valid string  [input_value=None]
    #
    # Das ist keine Eigenheit von recipeYield - jedes Feld ohne eigenes
    # Optional[...] kann so getroffen werden, sobald der Nutzer es einmal
    # leer gelassen hat. Statt das Feld fuer Feld nachzuruesten, ersetzt
    # dieser Validator jedes explizite `null` vor der Typpruefung durch den
    # Default des jeweiligen Feldes (oder einen typgerechten Leerwert, falls
    # das Feld selbst keinen hat) - siehe _none_replacement() oben. Felder,
    # die bereits Optional[...] deklarieren, sind davon nicht betroffen:
    # dort ist None ohnehin ein gueltiger Wert und bleibt unangetastet.
    @model_validator(mode="before")
    @classmethod
    def null_becomes_default(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for name, field in cls.model_fields.items():
            key = field.alias or name
            if key not in data or data[key] is not None:
                continue
            if type(None) in get_args(field.annotation):
                continue  # Optional[...] - None ist hier ein gueltiger Wert.
            data[key] = _none_replacement(field)
        return data


class Category(BaseModel):
    """A recipe category."""

    name: str = Field(description="Category name")
    recipe_count: int = Field(description="Number of recipes in category")


class Keyword(BaseModel):
    """A recipe keyword/tag."""

    name: str = Field(description="Keyword name")
    recipe_count: int = Field(description="Number of recipes with this keyword")


class VisibleInfoBlocks(BaseModel):
    """Configuration for visible information blocks in the UI."""

    preparation_time: Optional[bool] = Field(
        None, alias="preparation-time", description="Show preparation time"
    )
    cooking_time: Optional[bool] = Field(
        None, alias="cooking-time", description="Show cooking time"
    )
    total_time: Optional[bool] = Field(
        None, alias="total-time", description="Show total time"
    )
    nutrition_information: Optional[bool] = Field(
        None, alias="nutrition-information", description="Show nutrition info"
    )
    tools: Optional[bool] = Field(None, description="Show tools list")

    model_config = ConfigDict(populate_by_name=True)


class CookbookConfig(BaseModel):
    """Cookbook app configuration."""

    folder: Optional[str] = Field(None, description="Recipe folder path")
    update_interval: Optional[int] = Field(
        None, description="Auto-rescan interval in minutes"
    )
    print_image: Optional[bool] = Field(None, description="Print images with recipes")
    visibleInfoBlocks: Optional[VisibleInfoBlocks] = Field(
        None, description="Visible info blocks configuration"
    )


class APIVersion(BaseModel):
    """API version information."""

    epoch: int = Field(description="API epoch")
    major: int = Field(description="Major version")
    minor: int = Field(description="Minor version")


class Version(BaseModel):
    """Version information for Cookbook app and API."""

    cookbook_version: List[int] = Field(description="Cookbook app version")
    api_version: APIVersion = Field(description="API version")


# Response models for MCP tools


class ImportRecipeResponse(BaseResponse):
    """Response model for recipe import."""

    recipe: Recipe = Field(description="The imported recipe")
    recipe_id: str = Field(description="ID of the imported recipe")


class CreateRecipeResponse(IdResponse):
    """Response model for recipe creation."""

    pass


class UpdateRecipeResponse(IdResponse):
    """Response model for recipe update."""

    pass


class DeleteRecipeResponse(StatusResponse):
    """Response model for recipe deletion."""

    deleted_id: int = Field(description="ID of deleted recipe")


class ListRecipesResponse(BaseResponse):
    """Response model for listing recipes."""

    recipes: List[RecipeStub] = Field(description="List of recipe stubs")
    total_count: int = Field(description="Total number of recipes")


class SearchRecipesResponse(BaseResponse):
    """Response model for recipe search."""

    recipes: List[RecipeStub] = Field(description="Matching recipes")
    query: str = Field(description="Search query used")
    total_found: int = Field(description="Number of recipes found")


class ListCategoriesResponse(BaseResponse):
    """Response model for listing categories."""

    categories: List[Category] = Field(description="List of categories")


class ListKeywordsResponse(BaseResponse):
    """Response model for listing keywords."""

    keywords: List[Keyword] = Field(description="List of keywords")


class ReindexResponse(StatusResponse):
    """Response model for reindex operation."""

    pass
