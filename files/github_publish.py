"""
GitHub Pages publisher for recipe nutrition pages.
----------------------------------------------------
Pushes a standalone HTML page (recipe + nutrition breakdown) straight to a
GitHub repo via the Contents API, so it appears on GitHub Pages without any
local git setup. Used by cronometer_bot.py to give each reply a "save this
recipe" link, in addition to the inline table already posted to Reddit.

Requires a public repo with GitHub Pages enabled (Settings -> Pages ->
Deploy from branch -> main -> / (root)), and a fine-grained personal access
token scoped to that repo with "Contents: Read and write" permission.

Environment variables required:
    GITHUB_TOKEN    the personal access token
    GITHUB_OWNER    your GitHub username
    GITHUB_REPO     the repo name, e.g. "cronometer-bot-recipes"
"""

from __future__ import annotations

import os
import re
import base64
import time
import requests

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_OWNER = os.environ["GITHUB_OWNER"]
GITHUB_REPO = os.environ["GITHUB_REPO"]

API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents"
PAGES_BASE_URL = f"https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title} — Nutrition</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; }}
  h1 {{ font-size: 1.4em; }}
  table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
  td {{ padding: 8px 4px; border-bottom: 1px solid #eee; }}
  .ingredients {{ color: #555; font-size: 0.95em; }}
  .source-link {{ font-size: 0.85em; color: #888; margin-top: 30px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="ingredients"><strong>Ingredients:</strong> {ingredients}</p>
<table>
{rows}
</table>
<p>{summary}</p>
<p class="source-link">Generated from a Reddit comment. Nutrition data via USDA FoodData Central.
{reddit_link}</p>
</body>
</html>
"""


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "recipe"


def render_page(title: str, ingredient_names: list[str], per_serving: dict,
                 servings: int, summary: str, reddit_comment_url: str | None = None) -> str:
    rows = "\n".join(
        f"<tr><td>{label}</td><td>{value}</td></tr>"
        for label, value in [
            ("Servings", servings),
            ("Energy", f"{per_serving['energy_kcal']:.0f} kcal"),
            ("Protein", f"{per_serving['protein_g']:.1f} g"),
            ("Carbs", f"{per_serving['carbs_g']:.1f} g"),
            ("Fat", f"{per_serving['fat_g']:.1f} g"),
            ("Saturated fat", f"<strong>{per_serving['sat_fat_g']:.1f} g</strong>"),
            ("Fiber", f"{per_serving['fiber_g']:.1f} g"),
            ("Sodium", f"{per_serving['sodium_mg']:.0f} mg"),
        ]
    )
    reddit_link = f'<a href="{reddit_comment_url}">View original Reddit comment</a>' if reddit_comment_url else ""
    return PAGE_TEMPLATE.format(
        title=title,
        ingredients=", ".join(ingredient_names),
        rows=rows,
        summary=summary,
        reddit_link=reddit_link,
    )


def publish_recipe_page(comment_id: str, title: str, ingredient_names: list[str],
                         per_serving: dict, servings: int, summary: str,
                         reddit_comment_url: str | None = None) -> str:
    """Renders and pushes the recipe page to GitHub Pages. Returns the live URL."""
    slug = slugify(title) + "-" + comment_id
    path = f"recipes/{slug}.html"
    html = render_page(title, ingredient_names, per_serving, servings, summary, reddit_comment_url)
    content_b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")

    payload = {
        "message": f"Add recipe page: {slug}",
        "content": content_b64,
    }
    existing = requests.get(f"{API_BASE}/{path}", headers=HEADERS, timeout=15)
    if existing.status_code == 200:
        payload["sha"] = existing.json()["sha"]
        payload["message"] = f"Update recipe page: {slug}"

    resp = requests.put(f"{API_BASE}/{path}", headers=HEADERS, json=payload, timeout=15)

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"GitHub publish failed ({resp.status_code}): {resp.text}")

    return f"{PAGES_BASE_URL}/{path}"
