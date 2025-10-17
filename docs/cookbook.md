# Cookbook App

### Cookbook Tools

| Tool | Description |
|------|-------------|
| `nc_cookbook_import_recipe` | Import a recipe from a URL using schema.org metadata |
| `nc_cookbook_create_recipe` | Create a new recipe with all schema.org fields |
| `nc_cookbook_get_recipe` | Get a specific recipe by ID |
| `nc_cookbook_update_recipe` | Update an existing recipe |
| `nc_cookbook_delete_recipe` | Delete a recipe permanently |
| `nc_cookbook_list_recipes` | Get all recipes in the database |
| `nc_cookbook_search_recipes` | Search for recipes by keywords, tags, and categories |
| `nc_cookbook_list_categories` | Get all known recipe categories |
| `nc_cookbook_get_recipes_in_category` | Get all recipes in a specific category |
| `nc_cookbook_list_keywords` | Get all known recipe keywords/tags |
| `nc_cookbook_get_recipes_with_keywords` | Get all recipes that have specific keywords |
| `nc_cookbook_set_config` | Set Cookbook app configuration |
| `nc_cookbook_reindex` | Trigger a rescan of all recipes into the search database |

### Cookbook Resources

| Resource | Description |
|----------|-------------|
| `cookbook://version` | Get Cookbook app and API version information |
| `cookbook://config` | Get Cookbook app configuration |
| `nc://Cookbook/{recipe_id}` | Get a specific recipe by ID |

## Recipe Management

The server provides complete Nextcloud Cookbook integration, enabling you to manage your recipe collection:

- **Import recipes from websites** using schema.org metadata
- Full CRUD operations for recipes
- Search and organize with categories and keywords
- Support for structured recipe data (ingredients, instructions, nutrition, etc.)
- Configure app settings and trigger reindexing

### Schema.org Recipe Format

The Cookbook app uses the [schema.org/Recipe](https://schema.org/Recipe) specification for structured recipe data. This standard format includes:

- **Basic info**: Name, description, image, URL
- **Timing**: Preparation time, cooking time, total time (ISO8601 format like `PT30M`)
- **Ingredients**: List of ingredients with quantities
- **Instructions**: Step-by-step cooking instructions
- **Metadata**: Category, keywords/tags, yield (servings)
- **Nutrition**: Optional nutrition information

### Usage Examples

#### Import Recipe from URL

Many recipe websites include schema.org metadata. The import tool automatically extracts this data:

```python
# Import from a recipe website
await nc_cookbook_import_recipe(
    url="https://www.example.com/recipes/chocolate-cake"
)
# Returns: Recipe object with all extracted data
```

#### Create Recipe Manually

```python
# Create a new recipe from scratch
await nc_cookbook_create_recipe(
    name="Homemade Pizza",
    description="Classic homemade pizza with fresh ingredients",
    ingredients=[
        "500g pizza dough",
        "200g tomato sauce",
        "300g mozzarella cheese",
        "Fresh basil leaves",
        "Olive oil"
    ],
    instructions=[
        "Preheat oven to 250°C (480°F)",
        "Roll out the pizza dough",
        "Spread tomato sauce evenly",
        "Add mozzarella cheese",
        "Bake for 10-12 minutes",
        "Top with fresh basil and olive oil"
    ],
    category="Main Course",
    keywords="italian,vegetarian,quick",
    prep_time="PT20M",      # 20 minutes
    cook_time="PT12M",      # 12 minutes
    total_time="PT32M",     # 32 minutes
    recipe_yield=4          # 4 servings
)
```

#### Update Recipe

```python
# Update recipe details (only specified fields are changed)
await nc_cookbook_update_recipe(
    recipe_id=123,
    description="Updated: Classic homemade pizza - now with video tutorial!",
    url="https://example.com/videos/pizza-tutorial",
    keywords="italian,vegetarian,quick,video"
)
```

#### Search and Filter

```python
# Search recipes by keyword
results = await nc_cookbook_search_recipes(query="chocolate")

# List all categories
categories = await nc_cookbook_list_categories()
# Returns: [{"name": "Desserts", "recipe_count": 15}, ...]

# Get recipes in a category
desserts = await nc_cookbook_get_recipes_in_category(category="Desserts")

# List all keywords/tags
keywords = await nc_cookbook_list_keywords()
# Returns: [{"name": "chocolate", "recipe_count": 8}, ...]

# Get recipes with specific tags
quick_meals = await nc_cookbook_get_recipes_with_keywords(keywords=["quick", "30min"])
```

#### Manage Configuration

```python
# Configure the Cookbook app
await nc_cookbook_set_config(
    folder="Recipes",           # Folder path in user's files
    update_interval=15,         # Auto-rescan every 15 minutes
    print_image=True           # Print images with recipes
)

# Trigger manual reindex after file changes
await nc_cookbook_reindex()
```

### Time Format (ISO8601 Duration)

Recipe times use ISO8601 duration format:

| Duration | Format | Example |
|----------|--------|---------|
| 15 minutes | `PT15M` | Prep time |
| 1 hour | `PT1H` | Baking time |
| 1 hour 30 minutes | `PT1H30M` | Total time |
| 45 seconds | `PT45S` | Mixing time |
| 2 hours 15 minutes | `PT2H15M` | Slow cooking |

### Tips for Recipe Import

**Best practices for importing recipes from URLs:**

1. **Look for schema.org support**: Most modern recipe sites include schema.org metadata
2. **Check import quality**: Review imported recipes for completeness
3. **Handle duplicates**: The API prevents duplicate imports by recipe name
4. **Edit after import**: Update imported recipes with personal notes or adjustments

**Common recipe websites with good schema.org support:**
- AllRecipes
- Food Network
- BBC Good Food
- Serious Eats
- Bon Appétit
- Many food blogs using recipe plugins

### Organizing Your Recipes

**Categories**: Organize recipes by type (Appetizers, Main Course, Desserts, etc.)
- Use `nc_cookbook_list_categories` to see all categories
- Filter by category with `nc_cookbook_get_recipes_in_category`

**Keywords/Tags**: Tag recipes with searchable terms (vegetarian, quick, spicy, etc.)
- Use `nc_cookbook_list_keywords` to see all tags
- Filter by tags with `nc_cookbook_get_recipes_with_keywords`
- Search across all fields with `nc_cookbook_search_recipes`

**Reindexing**: The Cookbook app maintains a search index
- Automatically scans at configured intervals
- Manually trigger with `nc_cookbook_reindex` after bulk changes
- Required after modifying recipe files directly in WebDAV

## API Reference

For detailed API documentation, see the [Nextcloud Cookbook OpenAPI specification](https://github.com/nextcloud/cookbook/tree/master/docs/dev/api/0.1.2).
