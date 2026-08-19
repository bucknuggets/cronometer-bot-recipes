"""
RSS-based trigger watcher — a stopgap while waiting on Reddit API approval.
------------------------------------------------------------------------
Reddit's per-subreddit comment stream is available unauthenticated as RSS
(https://www.reddit.com/r/<sub>/comments/.rss), so this polls that instead
of using praw/the official API to detect "!cronometer" trigger comments,
runs them through the same extraction -> USDA -> summary pipeline as
cronometer_bot.py, and writes the results to a local SQLite queue plus a
regenerated dashboard.html you can open privately in a browser.

Important limitation: RSS is a flat list with no parent/child threading
info. That means it can only resolve the "link-following" trigger style
(the trigger comment itself contains a URL to the recipe, either a specific
comment or a bare post link) — it can detect that a classic "reply directly
to the recipe comment" trigger fired, but can't tell which comment it
replied to without that link, so those are logged as "needs_manual_review"
instead of guessed at.

This does not post anything back to Reddit — it only reads. Actually
replying still requires real Reddit API credentials.

Environment variables required: USDA_API_KEY, SUBREDDIT_NAME (comma-
separated). Also needs the same dummy REDDIT_* placeholders as
cronometer_bot.py expects at import time (it constructs a praw.Reddit
client but never calls it here).

Run:
    python rss_watcher.py          # polls forever, every 5 minutes
    python rss_watcher.py --once   # single poll, then exit
"""

from __future__ import annotations

import re
import sys
import html
import time
import sqlite3
import requests
import xml.etree.ElementTree as ET

from cronometer_bot import (
    TRIGGER_RE,
    REDDIT_URL_RE,
    SUBREDDIT_NAMES,
    parse_requested_servings,
    extract_recipe,
    lookup_food,
    scale_nutrients,
    sum_nutrient_dicts,
    generate_summary,
    format_reply,
    build_author_list,
)

DB_PATH = "pending_requests.sqlite3"
DASHBOARD_PATH = "dashboard.html"
USER_AGENT = "cronombot-rss-watcher/0.1 (read-only RSS polling, by u/bucknuggets)"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
POLL_INTERVAL_SECONDS = 300

TAG_RE = re.compile(r"<[^>]+>")
# Reddit's RSS appends this chrome to submission (not comment) entries —
# "submitted by /u/x [link] [comments]" — which otherwise leaks into the
# extraction prompt and gets mistaken for an explicitly-credited author.
SUBMISSION_FOOTER_RE = re.compile(
    r"\s*submitted by\s*/u/\S+\s*(\[link\]\s*)?(\[comments?\]\s*)?$", re.IGNORECASE
)


def strip_html(raw: str) -> str:
    text = html.unescape(TAG_RE.sub(" ", raw)).strip()
    return SUBMISSION_FOOTER_RE.sub("", text).strip()


def fetch_rss(url: str) -> ET.Element:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    return ET.fromstring(resp.content)


def parse_entries(root: ET.Element) -> list[dict]:
    entries = []
    for e in root.findall("a:entry", ATOM_NS):
        author_el = e.find("a:author/a:name", ATOM_NS)
        author = author_el.text.lstrip("/u/") if author_el is not None and author_el.text else None
        link_el = e.find("a:link", ATOM_NS)
        permalink = link_el.get("href") if link_el is not None else None
        content_el = e.find("a:content", ATOM_NS)
        body = strip_html(content_el.text) if content_el is not None and content_el.text else ""
        comment_id = None
        if permalink:
            m = re.search(r"/comments/[^/]+/[^/]+/([a-z0-9]+)/?$", permalink)
            comment_id = m.group(1) if m else None
        entries.append({"id": comment_id, "author": author, "body": body, "permalink": permalink})
    return entries


def fetch_linked_recipe(url: str) -> tuple[str | None, str | None]:
    """Resolves a reddit.com URL found inside a trigger comment to
    (recipe_text, author_username) via that post's own comment-stream RSS."""
    url = url.rstrip(").,!?>'\"")
    post_match = re.search(r"/r/([^/]+)/comments/([a-z0-9]+)/([^/]+)", url)
    if not post_match:
        return None, None
    subreddit, post_id, slug = post_match.groups()
    comment_id_match = re.search(r"/comments/[^/]+/[^/]+/([a-z0-9]+)", url)

    rss_url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/{slug}/.rss"
    root = fetch_rss(rss_url)
    entries = parse_entries(root)

    if comment_id_match:
        target_id = comment_id_match.group(1)
        for e in entries:
            if e["id"] == target_id:
                return e["body"], e["author"]
        return None, None

    # Bare post link (no specific comment) — the submission's own selftext
    # shows up as the first entry in its own comment-stream RSS, distinguishable
    # by having no comment id in its permalink.
    for e in entries:
        if e["id"] is None:
            return e["body"], e["author"]
    return None, None


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending (
            comment_id TEXT PRIMARY KEY,
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            subreddit TEXT,
            trigger_author TEXT,
            trigger_permalink TEXT,
            title TEXT,
            authors TEXT,
            reply_text TEXT,
            status TEXT DEFAULT 'pending',
            note TEXT
        )
        """
    )
    conn.commit()
    return conn


def already_seen(conn: sqlite3.Connection, comment_id: str) -> bool:
    return conn.execute("SELECT 1 FROM pending WHERE comment_id=?", (comment_id,)).fetchone() is not None


def save(conn: sqlite3.Connection, **fields) -> None:
    fields.setdefault("title", None)
    fields.setdefault("authors", None)
    fields.setdefault("reply_text", None)
    fields.setdefault("note", None)
    conn.execute(
        """
        INSERT OR IGNORE INTO pending
            (comment_id, subreddit, trigger_author, trigger_permalink, title, authors, reply_text, status, note)
        VALUES
            (:comment_id, :subreddit, :trigger_author, :trigger_permalink, :title, :authors, :reply_text, :status, :note)
        """,
        fields,
    )
    conn.commit()


def process_trigger(entry: dict, subreddit: str, conn: sqlite3.Connection) -> None:
    if not entry["id"] or already_seen(conn, entry["id"]):
        return
    if not TRIGGER_RE.search(entry["body"]):
        return

    base = dict(
        comment_id=entry["id"],
        subreddit=subreddit,
        trigger_author=entry["author"],
        trigger_permalink=entry["permalink"],
    )

    url_match = REDDIT_URL_RE.search(entry["body"])
    if not url_match:
        save(conn, **base, status="needs_manual_review",
             note="Reply-to-parent trigger — RSS can't resolve the parent comment's "
                  "text without full API access. Open the permalink to check manually.")
        return

    try:
        recipe_text, source_author = fetch_linked_recipe(url_match.group(0))
    except Exception as e:
        save(conn, **base, status="error", note=f"Failed to fetch linked content: {e}")
        return

    if not recipe_text:
        save(conn, **base, status="error", note="Could not resolve the linked recipe content.")
        return

    requested_servings = parse_requested_servings(entry["body"])
    recipe_data = extract_recipe(recipe_text)
    if "error" in recipe_data:
        save(conn, **base, status="no_recipe", note="No recipe found in the linked content.")
        return

    original_servings = recipe_data.get("original_servings", 1) or 1
    per_ingredient_totals, ingredient_names = [], []
    for ing in recipe_data["ingredients"]:
        per_100g = lookup_food(ing["name"])
        if per_100g is None:
            continue
        per_ingredient_totals.append(scale_nutrients(per_100g, ing["grams"]))
        ingredient_names.append(ing["name"])

    if not per_ingredient_totals:
        save(conn, **base, status="no_match", note="No ingredients matched USDA data.")
        return

    batch_totals = sum_nutrient_dicts(per_ingredient_totals)
    scale_factor = requested_servings / original_servings
    per_serving = {k: (v * scale_factor) / requested_servings for k, v in batch_totals.items()}
    summary = generate_summary(ingredient_names, per_serving, requested_servings)
    authors = build_author_list(source_author, recipe_data.get("credited_authors", []))
    reply_text = format_reply(per_serving, requested_servings, summary, None)

    save(conn, **base, title=recipe_data.get("title", "Recipe"),
         authors=", ".join(authors), reply_text=reply_text, status="ready")
    print(f"Queued: {recipe_data.get('title', 'Recipe')} (r/{subreddit}, {entry['id']})")


def poll_once(conn: sqlite3.Connection) -> None:
    for i, subreddit in enumerate(SUBREDDIT_NAMES):
        if i > 0:
            time.sleep(2)  # be polite to the unauthenticated RSS endpoint
        try:
            root = fetch_rss(f"https://www.reddit.com/r/{subreddit}/comments/.rss?limit=25")
        except Exception as e:
            print(f"Failed to fetch r/{subreddit}: {e}")
            continue
        for entry in parse_entries(root):
            try:
                process_trigger(entry, subreddit, conn)
            except Exception as e:
                print(f"Error processing {entry.get('id')}: {e}")
    render_dashboard(conn)


STATUS_LABELS = {
    "ready": ("Ready to post", "#1a7f37"),
    "needs_manual_review": ("Needs manual review", "#9a6700"),
    "no_recipe": ("No recipe found", "#57606a"),
    "no_match": ("No nutrition match", "#57606a"),
    "error": ("Error", "#cf222e"),
}


def render_dashboard(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT * FROM pending ORDER BY detected_at DESC"
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM pending LIMIT 0").description]

    cards = []
    for row in rows:
        r = dict(zip(cols, row))
        label, color = STATUS_LABELS.get(r["status"], (r["status"], "#57606a"))
        title = html.escape(r["title"] or "(untitled)")
        authors = html.escape(r["authors"] or "")
        note = html.escape(r["note"] or "")
        reply = html.escape(r["reply_text"] or "")
        cards.append(f"""
        <div class="card">
          <div class="card-head">
            <span class="status" style="background:{color}">{html.escape(label)}</span>
            <span class="meta">r/{html.escape(r['subreddit'])} · {html.escape(r['detected_at'])}</span>
          </div>
          <h2>{title}</h2>
          {f'<p class="authors">by {authors}</p>' if authors else ''}
          <p class="meta">Trigger by <a href="{html.escape(r['trigger_permalink'] or '#')}">u/{html.escape(r['trigger_author'] or '?')}</a></p>
          {f'<p class="note">{note}</p>' if note else ''}
          {f'<pre class="reply">{reply}</pre>' if reply else ''}
        </div>""")

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<title>cronombot — pending requests</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 20px; color: #1a1a1a; background: #fafafa; }}
  h1 {{ font-size: 1.6em; }}
  .subtitle {{ color: #666; margin-top: -10px; }}
  .card {{ background: white; border: 1px solid #e1e4e8; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; }}
  .card-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
  .status {{ color: white; font-size: 0.75em; padding: 3px 8px; border-radius: 12px; }}
  .meta {{ color: #888; font-size: 0.85em; }}
  .authors {{ color: #666; margin: 4px 0; }}
  .note {{ color: #9a6700; font-size: 0.9em; }}
  .reply {{ white-space: pre-wrap; background: #f6f8fa; padding: 10px; border-radius: 6px; font-size: 0.85em; }}
  h2 {{ margin-bottom: 4px; }}
</style>
</head>
<body>
<h1>cronombot — pending requests</h1>
<p class="subtitle">Detected via RSS (no Reddit API access yet). This page is only on your machine — nothing here is hosted anywhere. Auto-refreshes every 60s.</p>
{''.join(cards) if cards else '<p>No triggers detected yet.</p>'}
</body>
</html>
"""
    with open(DASHBOARD_PATH, "w") as f:
        f.write(page)


def main() -> None:
    conn = init_db()
    once = "--once" in sys.argv
    print(f"Watching {', '.join('r/' + s for s in SUBREDDIT_NAMES)} via RSS (read-only, no Reddit API needed)...")
    while True:
        poll_once(conn)
        if once:
            break
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
