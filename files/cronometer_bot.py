"""
Reddit Nutrition Bot
---------------------
Watches one or more subreddits for comments like "!cronometer 2 servings"
replying to a recipe comment, extracts the recipe's ingredients with a local
Ollama model, looks up nutrition data via USDA FoodData Central, scales it to
the requested serving count, and posts a formatted nutrition breakdown as a
reply.

Setup:
    pip install praw requests
    Ollama must be running locally with OLLAMA_MODEL pulled (default
    qwen2.5:7b): `ollama pull qwen2.5:7b`

Environment variables required (set these before running, e.g. in a .env
file loaded by your shell, or export them directly):
    REDDIT_CLIENT_ID
    REDDIT_CLIENT_SECRET
    REDDIT_USERNAME
    REDDIT_PASSWORD
    REDDIT_USER_AGENT       e.g. "cronometer-bot/0.1 by u/yourusername"
    USDA_API_KEY            get a free key at https://api.data.gov/signup
    SUBREDDIT_NAME          comma-separated, no "r/" prefix,
                            e.g. "cooking,recipes,MealPrepSunday"

Optional:
    OLLAMA_URL              default "http://localhost:11434/api/chat"
    OLLAMA_MODEL            default "qwen2.5:7b"

Run:
    python cronometer_bot.py
"""

from __future__ import annotations

import os
import re
import json
import time
import sqlite3
import requests
import praw
from github_publish import publish_recipe_page

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TRIGGER_RE = re.compile(r"!cronometer\s*(\d+)?\s*(?:serving)?", re.IGNORECASE)
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/chat")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
USDA_API_KEY = os.environ["USDA_API_KEY"]
SEEN_DB_PATH = "seen_comments.sqlite3"

# Your Every.org fundraiser link — create one at every.org so donations
# through this link are tracked and totaled publicly, without money ever
# passing through your own accounts. Replace with your real fundraiser URL.
DONATE_URL = "https://www.every.org/REPLACE-WITH-YOUR-CHARITY/f/REPLACE-WITH-YOUR-FUNDRAISER-SLUG"


def ollama_chat(prompt: str, want_json: bool = False) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if want_json:
        payload["format"] = "json"
    resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()["message"]["content"].strip()


reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    username=os.environ["REDDIT_USERNAME"],
    password=os.environ["REDDIT_PASSWORD"],
    user_agent=os.environ["REDDIT_USER_AGENT"],
)

SUBREDDIT_NAMES = [s.strip() for s in os.environ["SUBREDDIT_NAME"].split(",") if s.strip()]

# ---------------------------------------------------------------------------
# Dedup storage — avoid double-replying if the bot restarts
# ---------------------------------------------------------------------------


def init_seen_db():
    conn = sqlite3.connect(SEEN_DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (comment_id TEXT PRIMARY KEY)")
    conn.commit()
    return conn


def already_replied(conn, comment_id):
    cur = conn.execute("SELECT 1 FROM seen WHERE comment_id = ?", (comment_id,))
    return cur.fetchone() is not None


def mark_replied(conn, comment_id):
    conn.execute("INSERT OR IGNORE INTO seen (comment_id) VALUES (?)", (comment_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Step 1: parse the trigger comment for requested servings
# ---------------------------------------------------------------------------


def parse_requested_servings(trigger_body: str, default: int = 1) -> int:
    match = TRIGGER_RE.search(trigger_body)
    if match and match.group(1):
        return int(match.group(1))
    return default


# ---------------------------------------------------------------------------
# Step 2: extract structured ingredients from the recipe comment via Claude
# ---------------------------------------------------------------------------

EXTRACTION_PROMPT = """You will be given a Reddit comment that contains a recipe, \
possibly mixed in with conversational text. Extract the ingredient list.

For each ingredient, estimate the total weight in grams (use standard \
culinary conversions and your knowledge of typical ingredient densities). \
Also determine how many servings the recipe as written makes (if the \
original comment doesn't state it, make a reasonable estimate based on the \
quantities involved).

Respond with ONLY valid JSON, no other text, in this exact shape:
{
  "title": "<short descriptive name for this dish, e.g. 'Red Lentil Soup'>",
  "original_servings": <number>,
  "ingredients": [
    {"name": "<plain ingredient name for food-database lookup>", "grams": <number>}
  ]
}

If the comment does not appear to contain a recipe, respond with:
{"error": "no_recipe_found"}

Comment:
\"\"\"
{comment_text}
\"\"\"
"""


def extract_recipe(comment_text: str) -> dict:
    prompt = EXTRACTION_PROMPT.replace("{comment_text}", comment_text)
    raw = ollama_chat(prompt, want_json=True)
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Step 3: USDA FoodData Central lookups
# ---------------------------------------------------------------------------

NUTRIENT_IDS = {
    "energy_kcal": 1008,
    "protein_g": 1003,
    "fat_g": 1004,
    "carbs_g": 1005,
    "fiber_g": 1079,
    "sat_fat_g": 1258,
    "sugar_g": 2000,
    "sodium_mg": 1093,
    "iron_mg": 1089,
    "calcium_mg": 1087,
    "vitamin_c_mg": 1162,
    "potassium_mg": 1092,
}


def lookup_food(ingredient_name: str) -> dict | None:
    """Search USDA FDC and return per-100g nutrient values for the best match."""
    params = {
        "api_key": USDA_API_KEY,
        "query": ingredient_name,
        "pageSize": 3,
        "dataType": ["Foundation", "SR Legacy"],  # prefer generic/whole foods
    }
    resp = requests.get(USDA_SEARCH_URL, params=params, timeout=10)
    resp.raise_for_status()
    results = resp.json().get("foods", [])
    if not results:
        return None

    best = results[0]
    nutrients_by_id = {n["nutrientId"]: n.get("value", 0) for n in best.get("foodNutrients", [])}

    per_100g = {}
    for key, nid in NUTRIENT_IDS.items():
        per_100g[key] = nutrients_by_id.get(nid, 0)
    return per_100g


def scale_nutrients(per_100g: dict, grams: float) -> dict:
    factor = grams / 100.0
    return {k: v * factor for k, v in per_100g.items()}


def sum_nutrient_dicts(dicts: list[dict]) -> dict:
    totals = {k: 0.0 for k in NUTRIENT_IDS}
    for d in dicts:
        for k in totals:
            totals[k] += d.get(k, 0)
    return totals


# ---------------------------------------------------------------------------
# Step 4: generate the meal summary + pairing suggestions
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """Here is a recipe and its total nutrition profile (for the \
whole batch, {servings} servings). In 2-3 sentences: (a) briefly describe \
what kind of meal/dish this is and when someone might eat it (e.g. quick \
breakfast, cheap snack, hearty dinner, side dish), and (b) suggest one or two \
complementary foods or dishes to pair with it, from a flavor and nutritional \
completeness standpoint (e.g. if it's carb-heavy and low protein, suggest a \
protein pairing). Pairing suggestions must be vegan / fully plant-based only \
— no meat, fish, dairy, eggs, or honey.

Recipe ingredients: {ingredients}
Nutrition per serving: {nutrition}

Respond with just the 2-3 sentences, no preamble.
"""


def generate_summary(ingredient_names: list[str], per_serving: dict, servings: int) -> str:
    prompt = SUMMARY_PROMPT.format(
        servings=servings,
        ingredients=", ".join(ingredient_names),
        nutrition=json.dumps({k: round(v, 1) for k, v in per_serving.items()}),
    )
    return ollama_chat(prompt)


# ---------------------------------------------------------------------------
# Step 5: format the Reddit reply
# ---------------------------------------------------------------------------


def format_reply(per_serving: dict, servings: int, summary: str, page_url: str | None = None) -> str:
    fiber_note = ""
    if per_serving["fiber_g"] >= 4:
        fiber_note = f" — a good source of fiber ({per_serving['fiber_g']:.1f}g)"

    micros = []
    if per_serving["iron_mg"] >= 2:
        micros.append(f"Iron: {per_serving['iron_mg']:.1f}mg")
    if per_serving["calcium_mg"] >= 100:
        micros.append(f"Calcium: {per_serving['calcium_mg']:.0f}mg")
    if per_serving["vitamin_c_mg"] >= 10:
        micros.append(f"Vitamin C: {per_serving['vitamin_c_mg']:.0f}mg")
    if per_serving["potassium_mg"] >= 300:
        micros.append(f"Potassium: {per_serving['potassium_mg']:.0f}mg")
    micros_line = f"\n**Notable micronutrients:** {', '.join(micros)}" if micros else ""
    saved_recipe_line = f"\n\n[Save this recipe]({page_url}) — pastes into most trackers via URL import." if page_url else ""

    return f"""**Nutrition estimate — {servings} serving{'s' if servings != 1 else ''}** *(per serving)*

| | |
|---|---|
| Energy | {per_serving['energy_kcal']:.0f} kcal |
| Protein | {per_serving['protein_g']:.1f} g |
| Carbs | {per_serving['carbs_g']:.1f} g |
| Fat | {per_serving['fat_g']:.1f} g (**Sat. fat: {per_serving['sat_fat_g']:.1f} g**) |
| Fiber | {per_serving['fiber_g']:.1f} g{fiber_note} |
| Sodium | {per_serving['sodium_mg']:.0f} mg |
{micros_line}

{summary}{saved_recipe_line}

---
^(I am a bot | Estimate based on USDA FoodData Central, actual values vary by brand/prep) | [^(Support this project — 100% to charity, tracked publicly)]({DONATE_URL}) | ^(Reply) ^`!cronometer N` ^(for a different serving count)
"""


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def handle_trigger_comment(comment, conn):
    try:
        parent = comment.parent()
        recipe_text = getattr(parent, "body", None)
        if not recipe_text:
            return  # parent isn't a comment with text (e.g. it's the post itself)

        requested_servings = parse_requested_servings(comment.body)

        recipe_data = extract_recipe(recipe_text)
        if "error" in recipe_data:
            comment.reply("I couldn't find a recipe in the comment you replied to.")
            mark_replied(conn, comment.id)
            return

        original_servings = recipe_data.get("original_servings", 1) or 1
        ingredients = recipe_data["ingredients"]

        per_ingredient_totals = []
        ingredient_names = []
        for ing in ingredients:
            per_100g = lookup_food(ing["name"])
            if per_100g is None:
                continue
            per_ingredient_totals.append(scale_nutrients(per_100g, ing["grams"]))
            ingredient_names.append(ing["name"])

        if not per_ingredient_totals:
            comment.reply("I couldn't match any ingredients to nutrition data for this recipe.")
            mark_replied(conn, comment.id)
            return

        batch_totals = sum_nutrient_dicts(per_ingredient_totals)
        # batch_totals is the nutrition for the whole original recipe. Scale it
        # to match the requested serving count, then divide by that count to
        # get the per-serving values shown in the reply.
        scale_factor = requested_servings / original_servings
        per_serving = {k: (v * scale_factor) / requested_servings for k, v in batch_totals.items()}

        summary = generate_summary(ingredient_names, per_serving, requested_servings)

        title = recipe_data.get("title", "Recipe")
        page_url = None
        try:
            page_url = publish_recipe_page(
                comment_id=comment.id,
                title=title,
                ingredient_names=ingredient_names,
                per_serving=per_serving,
                servings=requested_servings,
                summary=summary,
                reddit_comment_url=f"https://reddit.com{parent.permalink}",
            )
        except Exception as e:
            print(f"GitHub Pages publish failed (continuing without link): {e}")

        reply_text = format_reply(per_serving, requested_servings, summary, page_url)

        comment.reply(reply_text)
        mark_replied(conn, comment.id)
        print(f"Replied to comment {comment.id}")

    except Exception as e:
        print(f"Error handling comment {comment.id}: {e}")


def main():
    conn = init_seen_db()
    # praw natively supports watching multiple subreddits at once via a
    # "+"-joined name, e.g. reddit.subreddit("cooking+recipes+MealPrepSunday")
    subreddit = reddit.subreddit("+".join(SUBREDDIT_NAMES))
    print(f"Watching {', '.join('r/' + n for n in SUBREDDIT_NAMES)} for !cronometer triggers...")

    for comment in subreddit.stream.comments(skip_existing=True):
        if already_replied(conn, comment.id):
            continue
        if TRIGGER_RE.search(comment.body):
            handle_trigger_comment(comment, conn)
        # Be polite to the API even though PRAW handles most rate-limiting.
        time.sleep(0.5)


if __name__ == "__main__":
    main()
