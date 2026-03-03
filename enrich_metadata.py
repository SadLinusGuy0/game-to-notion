import requests
import time
import os
import logging
from urllib.parse import quote
from bs4 import BeautifulSoup
from http import cookiejar
from urllib import request as urllib_request

# ─────────────────────────────────────────────────────────────
# CONFIG — set these as GitHub Actions secrets
# ─────────────────────────────────────────────────────────────
NOTION_API_KEY     = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
STEAMGRIDDB_API_KEY = os.environ.get("STEAMGRIDDB_API_KEY")

# ─────────────────────────────────────────────────────────────
# MISC
# ─────────────────────────────────────────────────────────────
MAX_RETRIES = 5
RETRY_DELAY = 3
SGDB_BASE   = "https://www.steamgriddb.com/api/v2"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# NOTION HELPERS
# ─────────────────────────────────────────────────────────────
def notion_request(method, path, json_data=None):
    """Notion API request with retry and error logging."""
    url = f"https://api.notion.com/v1{path}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    for attempt in range(MAX_RETRIES):
        try:
            if method == "post":
                response = requests.post(url, headers=headers, json=json_data, timeout=15)
            elif method == "patch":
                response = requests.patch(url, headers=headers, json=json_data, timeout=15)
            elif method == "get":
                response = requests.get(url, headers=headers, timeout=15)
            if not response.ok:
                logger.error(f"Notion {response.status_code}: {response.text}")
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Notion request failed ({attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    return None


def get_all_notion_pages():
    """Fetch all visible pages from the Notion database, handling pagination."""
    logger.info("Fetching all pages from Notion database...")
    pages = []
    cursor = None

    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor

        data = notion_request("post", f"/databases/{NOTION_DATABASE_ID}/query", body)
        if not data:
            logger.error("Failed to fetch pages from Notion.")
            break

        results = data.get("results", [])
        pages.extend(results)

        if data.get("has_more"):
            cursor = data.get("next_cursor")
        else:
            break

    logger.info(f"Found {len(pages)} pages in Notion database.")
    return pages


def update_notion_page(page_id, cover_url=None, info=None, tags=None):
    """Update a Notion page with cover art, info, and/or tags."""
    payload = {"properties": {}}

    if info:
        payload["properties"]["info"] = {
            "rich_text": [{"type": "text", "text": {"content": info[:2000]}}]
        }

    if tags:
        payload["properties"]["tags"] = {
            "multi_select": [{"name": t} for t in tags[:20]]
        }

    if cover_url:
        payload["cover"] = {"type": "external", "external": {"url": cover_url}}

    result = notion_request("patch", f"/pages/{page_id}", payload)
    return result is not None


# ─────────────────────────────────────────────────────────────
# STEAMGRIDDB
# ─────────────────────────────────────────────────────────────
def sgdb_get(path):
    """GET request to SteamGridDB API."""
    url = f"{SGDB_BASE}{path}"
    headers = {"Authorization": f"Bearer {STEAMGRIDDB_API_KEY}"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.ok:
            return response.json()
        logger.warning(f"SGDB {response.status_code} for {path}")
        return None
    except requests.exceptions.RequestException as e:
        logger.warning(f"SGDB request failed: {e}")
        return None


def get_sgdb_game_id(name):
    """Search SteamGridDB for a game by name and return its ID."""
    data = sgdb_get(f"/search/autocomplete/{quote(name)}")
    if data and data.get("success") and data.get("data"):
        return data["data"][0]["id"]
    return None


def get_sgdb_horizontal_cover(name):
    """
    Fetch a 920x430 horizontal grid image URL from SteamGridDB.
    Falls back to 460x215 if no 920x430 results exist.
    """
    game_id = get_sgdb_game_id(name)
    if not game_id:
        logger.warning(f"  SGDB: no game found for '{name}'")
        return None

    # Try 920x430 first (high-res horizontal), then fall back to 460x215
    for dims in ["920x430", "460x215"]:
        data = sgdb_get(f"/grids/game/{game_id}?dimensions={dims}&limit=1")
        if data and data.get("success") and data.get("data"):
            url = data["data"][0].get("url")
            if url:
                logger.info(f"  SGDB: found {dims} cover for '{name}'")
                return url

    logger.warning(f"  SGDB: no horizontal cover found for '{name}'")
    return None


# ─────────────────────────────────────────────────────────────
# STEAM STORE SCRAPER
# ─────────────────────────────────────────────────────────────
def get_steam_store_info(name):
    """
    Search the Steam store for a game by name and scrape its
    description snippet and tags. Returns (info_text, tags_list).
    """
    # First: find the appid via Steam search API
    search_url = f"https://store.steampowered.com/api/storesearch/?term={quote(name)}&l=english&cc=gb"
    try:
        response = requests.get(search_url, timeout=10)
        data = response.json()
        items = data.get("items", [])
        if not items:
            return None, []
        appid = items[0]["id"]
    except Exception as e:
        logger.warning(f"  Steam search failed for '{name}': {e}")
        return None, []

    # Second: scrape the store page for description and tags
    store_url = f"https://store.steampowered.com/app/{appid}/?l=english"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
        "Cookie": "birthtime=568022401; lastagecheckage=1-January-1990; wants_mature_content=1"
    }

    try:
        resp = requests.get(store_url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")

        info = ""
        snippet = soup.find("div", {"class": "game_description_snippet"})
        if snippet:
            info = snippet.get_text(strip=True)

        tags = []
        tag_elements = soup.find_all("a", {"class": "app_tag"})
        for tag in tag_elements:
            text = tag.get_text(strip=True)
            if text:
                tags.append(text)

        return info, tags[:20]

    except Exception as e:
        logger.warning(f"  Steam store scrape failed for '{name}' (appid {appid}): {e}")
        return None, []


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def get_page_name(page):
    """Extract the game name from a Notion page object."""
    try:
        return page["properties"]["name"]["title"][0]["plain_text"]
    except (KeyError, IndexError):
        return None


def page_needs_cover(page):
    """Return True if the page has no cover image set."""
    return page.get("cover") is None


def page_needs_info(page):
    """Return True if the page has an empty info field."""
    try:
        text = page["properties"]["info"]["rich_text"]
        return len(text) == 0 or text[0]["plain_text"].strip() == ""
    except (KeyError, IndexError):
        return True


def page_needs_tags(page):
    """Return True if the page has no tags set."""
    try:
        return len(page["properties"]["tags"]["multi_select"]) == 0
    except (KeyError):
        return True


if __name__ == "__main__":
    logger.info("═══ Metadata Enrichment ═══")
    logger.info("Fetches horizontal covers from SteamGridDB + info/tags from Steam")
    logger.info("Only updates entries that are missing these fields.")

    pages = get_all_notion_pages()
    if not pages:
        logger.error("No pages found. Check NOTION_API_KEY and NOTION_DATABASE_ID.")
        exit(1)

    updated   = 0
    skipped   = 0
    no_cover  = 0
    no_steam  = 0

    for i, page in enumerate(pages, 1):
        name = get_page_name(page)
        if not name:
            skipped += 1
            continue

        page_id      = page["id"]
        needs_cover  = page_needs_cover(page)
        needs_info   = page_needs_info(page)
        needs_tags   = page_needs_tags(page)

        if not needs_cover and not needs_info and not needs_tags:
            logger.info(f"[{i}/{len(pages)}] {name} — already complete, skipping")
            skipped += 1
            continue

        logger.info(f"[{i}/{len(pages)}] {name} — enriching (cover={needs_cover}, info={needs_info}, tags={needs_tags})")

        cover_url = None
        info      = None
        tags      = None

        # Fetch horizontal cover from SteamGridDB
        if needs_cover:
            cover_url = get_sgdb_horizontal_cover(name)
            if not cover_url:
                no_cover += 1
            time.sleep(0.3)  # SGDB rate limit courtesy

        # Fetch info + tags from Steam store (only if needed)
        if needs_info or needs_tags:
            steam_info, steam_tags = get_steam_store_info(name)
            if needs_info and steam_info:
                info = steam_info
            if needs_tags and steam_tags:
                tags = steam_tags
            if not steam_info and not steam_tags:
                no_steam += 1
            time.sleep(0.5)

        # Push update to Notion
        if cover_url or info or tags:
            success = update_notion_page(page_id, cover_url=cover_url, info=info, tags=tags)
            if success:
                logger.info(f"  ✓ Updated: {name}")
                updated += 1
            else:
                logger.error(f"  ✗ Failed to update: {name}")
        else:
            logger.info(f"  — Nothing to update for: {name}")
            skipped += 1

        # Notion rate limit: 3 requests/sec
        time.sleep(0.4)

    logger.info("═══ Enrichment complete ═══")
    logger.info(f"  Updated:              {updated}")
    logger.info(f"  Skipped (complete):   {skipped}")
    logger.info(f"  No SGDB cover found:  {no_cover}")
    logger.info(f"  Not on Steam:         {no_steam}")
