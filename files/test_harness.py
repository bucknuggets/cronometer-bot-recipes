"""
Test harness for cronometer_bot.py
-----------------------------------
Runs the recipe-extraction -> USDA lookup -> scaling -> reply-formatting
pipeline on a sample recipe comment, without touching Reddit at all. Use this
to sanity-check the nutrition math and reply formatting before going live.

Requires USDA_API_KEY and a running local Ollama instance with OLLAMA_MODEL
pulled (no Reddit credentials needed).
If GITHUB_TOKEN, GITHUB_OWNER, and GITHUB_REPO are also set, it will publish
real test pages to your GitHub Pages repo so you can check those too —
otherwise it skips that step automatically.

Run:
    python test_harness.py
"""

import os
import json

# Reuse the pipeline functions from the bot itself.
from cronometer_bot import (
    extract_recipe,
    lookup_food,
    scale_nutrients,
    sum_nutrient_dicts,
    generate_summary,
    format_reply,
)

GITHUB_CONFIGURED = all(
    os.environ.get(var) for var in ("GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO")
)
if GITHUB_CONFIGURED:
    from github_publish import publish_recipe_page

# ---------------------------------------------------------------------------
# Sample recipe comments to test with. Add your own to try edge cases —
# messy formatting, missing serving counts, unusual ingredients, etc.
# ---------------------------------------------------------------------------

SAMPLE_RECIPES = {
    "lentil_soup": """
        This is my go-to lentil soup, makes about 4 servings:
        - 1 cup dried red lentils
        - 1 tbsp olive oil
        - 1 medium onion, diced
        - 2 cloves garlic, minced
        - 1 carrot, diced
        - 4 cups vegetable broth
        - 1 tsp cumin
        - salt and pepper to taste

        Saute onion, garlic, and carrot in the olive oil until soft. Add
        lentils, broth, and cumin. Simmer 25 minutes until lentils are tender.
    """,
    "no_serving_count": """
        Overnight oats: 1/2 cup rolled oats, 1/2 cup milk, 1 tbsp chia seeds,
        1 tbsp honey, handful of blueberries. Mix and refrigerate overnight.
    """,
    "not_a_recipe": """
        Great post, I've been meaning to try this restaurant for weeks!
    """,
}


def run_pipeline(recipe_text: str, requested_servings: int):
    print("=" * 70)
    print(f"Requested servings: {requested_servings}")
    print("-" * 70)

    recipe_data = extract_recipe(recipe_text)
    print("Extracted recipe data:")
    print(json.dumps(recipe_data, indent=2))
    print("-" * 70)

    if "error" in recipe_data:
        print("No recipe found — bot would reply with a fallback message.")
        return

    original_servings = recipe_data.get("original_servings", 1) or 1
    ingredients = recipe_data["ingredients"]

    per_ingredient_totals = []
    ingredient_names = []
    unmatched = []
    for ing in ingredients:
        per_100g = lookup_food(ing["name"])
        if per_100g is None:
            unmatched.append(ing["name"])
            continue
        per_ingredient_totals.append(scale_nutrients(per_100g, ing["grams"]))
        ingredient_names.append(ing["name"])

    if unmatched:
        print(f"WARNING - could not match to USDA data: {unmatched}")

    if not per_ingredient_totals:
        print("No ingredients matched — bot would reply with a fallback message.")
        return

    batch_totals = sum_nutrient_dicts(per_ingredient_totals)
    scale_factor = requested_servings / original_servings
    per_serving = {
        k: (v * scale_factor) / requested_servings for k, v in batch_totals.items()
    }

    print("Per-serving nutrition:")
    print(json.dumps({k: round(v, 2) for k, v in per_serving.items()}, indent=2))
    print("-" * 70)

    summary = generate_summary(ingredient_names, per_serving, requested_servings)

    page_url = None
    if GITHUB_CONFIGURED:
        page_url = publish_recipe_page(
            comment_id="test123",
            title=recipe_data.get("title", "Test Recipe"),
            ingredient_names=ingredient_names,
            per_serving=per_serving,
            servings=requested_servings,
            summary=summary,
        )
        print(f"Published test page: {page_url}")
        print("-" * 70)
    else:
        print("GITHUB_TOKEN/GITHUB_OWNER/GITHUB_REPO not set — skipping GitHub Pages publish test.")
        print("-" * 70)

    reply_text = format_reply(per_serving, requested_servings, summary, page_url)

    print("FORMATTED REPLY (what would be posted to Reddit):\n")
    print(reply_text)


def main():
    # Quick check that required keys are set before burning API calls.
    for var in ("USDA_API_KEY",):
        if not os.environ.get(var):
            raise SystemExit(f"Missing required env var: {var}")

    run_pipeline(SAMPLE_RECIPES["lentil_soup"], requested_servings=2)
    run_pipeline(SAMPLE_RECIPES["no_serving_count"], requested_servings=1)
    run_pipeline(SAMPLE_RECIPES["not_a_recipe"], requested_servings=2)


if __name__ == "__main__":
    main()
