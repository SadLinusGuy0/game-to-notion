import requests
import time
import os
import logging

# ─────────────────────────────────────────────
# CONFIG — set these as GitHub Actions secrets
# ─────────────────────────────────────────────
OPENXBL_API_KEY     = os.environ.get("OPENXBL_API_KEY")
NOTION_API_KEY      = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID  = os.environ.get("NOTION_DATABASE_ID_XBOX")
STEAMGRIDDB_API_KEY = os.environ.get("STEAMGRIDDB_API_KEY")
SGDB_BASE           = "https://www.steamgriddb.com/api/v2"

MAX_RETRIES  = 5
RETRY_DELAY  = 3
OPENXBL_BASE = "https://xbl.io/api/v2"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def openxbl_get(path):
    url = f"{OPENXBL_BASE}{path}"
    headers = {
        "X-Authorization": OPENXBL_API_KEY,
        "Accept-Language": "en-US",
        "Accept": "application/json",
    }
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 429:
                logger.warning("Rate limited — waiting 60s")
                time.sleep(60)
                continue
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"OpenXBL request failed ({attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    return None


def notion_request(method, path, json_data=None):
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
            response.raise_for_status()
            return response.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"Notion request failed ({attempt+1}/{MAX_RETRIES}): {e}")
            logger.error(f"Response body: {response.text}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
        except requests.exceptions.RequestException as e:
            logger.error(f"Notion request failed ({attempt+1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    return None


# ─────────────────────────────────────────────
# XBOX DATA
# ─────────────────────────────────────────────
def get_my_xuid():
    """Fetch the XUID of the authenticated account."""
    data = openxbl_get("/account")
    if not data:
        return None
    try:
        return data["profileUsers"][0]["id"]
    except (KeyError, IndexError) as e:
        logger.error(f"Could not parse XUID from account response: {e}")
        return None


def get_played_games():
    logger.info("Fetching played games from Xbox Live...")
    xuid = get_my_xuid()
    if not xuid:
        logger.error("Could not retrieve XUID — check OPENXBL_API_KEY.")
        return []
    logger.info(f"Authenticated as XUID: {xuid}")
    data = openxbl_get(f"/achievements/player/{xuid}")
    if not data:
        return []
    titles = data.get("titles", [])
    logger.info(f"Found {len(titles)} played titles.")
    return titles


def get_gamepass_title_ids():
    logger.info("Fetching Game Pass catalogue...")
    data = openxbl_get("/gamepass/all")
    if not data:
        logger.warning("Could not fetch Game Pass list — filter disabled.")
        return set()

    titles = data if isinstance(data, list) else data.get("titles", [])
    gp_ids = {str(g.get("titleId", "")) for g in titles if g.get("titleId")}
    logger.info(f"Game Pass catalogue: {len(gp_ids)} titles.")
    return gp_ids


def get_achievement_stats(title_id):
    data = openxbl_get(f"/achievements/title/{title_id}")
    if not data:
        return {"total": -1, "achieved": -1}
    achievements = data.get("achievements", [])
    if not achievements:
        return {"total": -1, "achieved": -1}
    total    = len(achievements)
    achieved = sum(1 for a in achievements if a.get("progressState") == "Achieved")
    return {"total": total, "achieved": achieved}


# ─────────────────────────────────────────────
# STEAMGRIDDB
# ─────────────────────────────────────────────
def get_sgdb_game_id(name):
    """Search SteamGridDB for a game by name, return the first match's ID."""
    try:
        response = requests.get(
            f"{SGDB_BASE}/search/autocomplete/{requests.utils.quote(name)}",
            headers={"Authorization": f"Bearer {STEAMGRIDDB_API_KEY}"},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success") and data.get("data"):
            return data["data"][0]["id"]
    except Exception as e:
        logger.warning(f"SteamGridDB search failed for '{name}': {e}")
    return None


def get_sgdb_cover(name):
    """Fetch a portrait grid (cover art) URL from SteamGridDB by game name."""
    game_id = get_sgdb_game_id(name)
    if not game_id:
        return None
    try:
        response = requests.get(
            f"{SGDB_BASE}/grids/game/{game_id}",
            headers={"Authorization": f"Bearer {STEAMGRIDDB_API_KEY}"},
            params={"dimensions": "600x900"},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        if data.get("success") and data.get("data"):
            return data["data"][0]["url"]
    except Exception as e:
        logger.warning(f"SteamGridDB grids fetch failed for '{name}': {e}")
    return None


# ─────────────────────────────────────────────
# NOTION
# ─────────────────────────────────────────────
def query_notion_for_game(name):
    data = notion_request("post", f"/databases/{NOTION_DATABASE_ID}/query", {
        "filter": {"property": "name", "rich_text": {"equals": name}}
    })
    return data.get("results", []) if data else []


def build_notion_payload(game, achievements, is_update=False):
    title_id  = str(game.get("titleId", ""))
    name      = game.get("name", "Unknown")
    store_url = f"https://www.xbox.com/en-GB/games/store/-/{title_id}"
    cover_url = get_sgdb_cover(name)
    icon_url  = game.get("displayImage", "")

    last_played = ""
    raw = game.get("titleHistory", {}).get("lastTimePlayed", "")
    if raw:
        last_played = raw[:10]

    total      = achievements.get("total", -1)
    achieved   = achievements.get("achieved", -1)
    completion = round(achieved / total * 100, 1) if total > 0 else -1

    properties = {
        "name":                  {"title": [{"type": "text", "text": {"content": name}}]},
        "store url":             {"url": store_url},
        "completion":            {"number": completion},
        "achieved achievements": {"number": achieved},
        "total achievements":    {"number": total},
        "platform":              {"multi_select": [{"name": "Xbox"}]},
    }
    if last_played:
        properties["last play"] = {"date": {"start": last_played}}

    payload = {"properties": properties}
    if not is_update:
        payload["parent"] = {"type": "database_id", "database_id": NOTION_DATABASE_ID}
    if cover_url:
        payload["cover"] = {"type": "external", "external": {"url": cover_url}}
    if icon_url:
        payload["icon"] = {"type": "external", "external": {"url": icon_url}}

    return payload


def add_game_to_notion(game, achievements):
    result = notion_request("post", "/pages", build_notion_payload(game, achievements))
    if result:
        logger.info(f"  + Added: {game.get('name')}")
    else:
        logger.error(f"  x Failed: {game.get('name')}")


def update_game_in_notion(page_id, game, achievements):
    result = notion_request("patch", f"/pages/{page_id}", build_notion_payload(game, achievements, is_update=True))
    if result:
        logger.info(f"  ~ Updated: {game.get('name')}")
    else:
        logger.error(f"  x Failed: {game.get('name')}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("=== Xbox to Notion Sync ===")

    played_games = get_played_games()
    if not played_games:
        logger.error("No games found. Check API key and Xbox profile privacy settings.")
        exit(1)

    gamepass_ids   = get_gamepass_title_ids()
    filtered_games = []
    skipped        = 0

    for game in played_games:
        title_id    = str(game.get("titleId", ""))
        is_gp       = title_id in gamepass_ids
        last_played = game.get("titleHistory", {}).get("lastTimePlayed", "")

        if is_gp and not last_played:
            logger.info(f"  Skipping unplayed Game Pass title: {game.get('name')}")
            skipped += 1
            continue

        filtered_games.append(game)

    logger.info(f"Processing {len(filtered_games)} games ({skipped} unplayed Game Pass titles skipped)")

    for i, game in enumerate(filtered_games, 1):
        logger.info(f"[{i}/{len(filtered_games)}] {game.get('name')}")
        achievements = get_achievement_stats(str(game.get("titleId", "")))
        existing     = query_notion_for_game(game.get("name", ""))

        if existing:
            update_game_in_notion(existing[0]["id"], game, achievements)
        else:
            add_game_to_notion(game, achievements)

        time.sleep(0.5)

    logger.info(f"=== Done: {len(filtered_games)} games processed ===")
