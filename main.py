import argparse
import requests
import time
import os
import logging
from features.review import get_steam_review_info
from features.steamstore import get_steam_store_info

# ─────────────────────────────────────────────
# CONFIG — set these as GitHub Actions secrets
# ─────────────────────────────────────────────
STEAM_API_KEY      = os.environ.get("STEAM_API_KEY")
STEAM_USER_ID      = os.environ.get("STEAM_USER_ID")
NOTION_API_KEY     = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

# OPTIONAL
include_played_free_games = os.environ.get("include_played_free_games") or 'true'
enable_item_update        = os.environ.get("enable_item_update") or 'true'
enable_filter             = os.environ.get("enable_filter") or 'false'

# MISC
MAX_RETRIES = 20
RETRY_DELAY = 2


def send_request_with_retry(url, headers=None, json_data=None, retries=MAX_RETRIES, method="patch"):
    while retries > 0:
        try:
            if method == "patch":
                response = requests.patch(url, headers=headers, json=json_data)
            elif method == "post":
                response = requests.post(url, headers=headers, json=json_data)
            elif method == "get":
                response = requests.get(url)
            if not response.ok:
                logger.error(f"Request failed with {response.status_code}: {response.text}")
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            logger.error(f"Request exception: {e}")
            retries -= 1
            if retries > 0:
                time.sleep(RETRY_DELAY)
            else:
                logger.error("Max retries exceeded, giving up.")
                return {}

# ─────────────────────────────────────────────
# STEAM API
# ─────────────────────────────────────────────
def get_owned_game_data_from_steam():
    url = "http://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?"
    url += f"key={STEAM_API_KEY}&steamid={STEAM_USER_ID}&include_appinfo=True"
    if include_played_free_games == "true":
        url += "&include_played_free_games=True"

    logger.info("Fetching data from Steam...")
    try:
        response = send_request_with_retry(url, method="get")
        logger.info("Steam data fetched successfully.")
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch Steam data: {e}")


def query_achievements_info_from_steam(game):
    url = (
        f"http://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/?"
        f"key={STEAM_API_KEY}&steamid={STEAM_USER_ID}&appid={game['appid']}"
    )
    logger.info(f"Fetching achievements for {game['name']}...")
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Achievements request failed for {game['name']}: {e}")
    except ValueError as e:
        logger.error(f"JSON parse failed for {game['name']}: {e}")
    return None


# ─────────────────────────────────────────────
# NOTION API
# ─────────────────────────────────────────────
def add_item_to_notion_database(game, achievements_info, review_text, steam_store_data):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    logger.info(f"Adding {game['name']} to Notion...")

    playtime        = round(float(game["playtime_forever"]) / 60, 1)
    last_played     = time.strftime("%Y-%m-%d", time.localtime(game["rtime_last_played"]))
    store_url       = f"https://store.steampowered.com/app/{game['appid']}"
    icon_url        = f"https://media.steampowered.com/steamcommunity/public/images/apps/{game['appid']}/{game['img_icon_url']}.jpg"
    cover_url       = f"https://steamcdn-a.akamaihd.net/steam/apps/{game['appid']}/header.jpg"
    total_ach       = achievements_info["total"]
    achieved_ach    = achievements_info["achieved"]
    completion      = round(float(achieved_ach) / float(total_ach) * 100, 1) if total_ach > 0 else -1

    data = {
        "parent": {"type": "database_id", "database_id": NOTION_DATABASE_ID},
        "properties": {
            "name":                  {"title": [{"type": "text", "text": {"content": game['name']}}]},
            "playtime":              {"number": playtime},
            "last play":             {"date": {"start": last_played}},
            "store url":             {"url": store_url},
            "completion":            {"number": completion},
            "total achievements":    {"number": total_ach},
            "achieved achievements": {"number": achieved_ach},
            "review":                {"rich_text": [{"type": "text", "text": {"content": review_text}}]},
            "info":                  {"rich_text": [{"type": "text", "text": {"content": steam_store_data["info"]}}]},
            "tags":                  {"multi_select": steam_store_data['tag']},
            "platform":              {"multi_select": [{"name": "Steam"}]},
        },
        "cover": {"type": "external", "external": {"url": cover_url}},
        "icon":  {"type": "external", "external": {"url": icon_url}},
    }

    try:
        response = send_request_with_retry(url, headers=headers, json_data=data, method="post")
        logger.info(f"{game['name']} added.")
        return response.json()
    except Exception as e:
        logger.error(f"Failed to add {game['name']}: {e}")


def query_item_from_notion_database(game):
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    data = {"filter": {"property": "name", "rich_text": {"equals": game['name']}}}

    logger.info(f"Querying Notion for {game['name']}...")
    try:
        response = send_request_with_retry(url, headers=headers, json_data=data, method="post")
        logger.info("Query complete.")
    except Exception as e:
        logger.error(f"Query failed for {game['name']}: {e}")
    finally:
        return response.json()


def update_item_to_notion_database(page_id, game, achievements_info, review_text, steam_store_data):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    playtime        = round(float(game["playtime_forever"]) / 60, 1)
    last_played     = time.strftime("%Y-%m-%d", time.localtime(game["rtime_last_played"]))
    store_url       = f"https://store.steampowered.com/app/{game['appid']}"
    icon_url        = f"https://media.steampowered.com/steamcommunity/public/images/apps/{game['appid']}/{game['img_icon_url']}.jpg"
    cover_url       = f"https://steamcdn-a.akamaihd.net/steam/apps/{game['appid']}/header.jpg"
    total_ach       = achievements_info["total"]
    achieved_ach    = achievements_info["achieved"]
    completion      = round(float(achieved_ach) / float(total_ach) * 100, 1) if total_ach > 0 else -1

    logger.info(f"Updating {game['name']} in Notion...")

    data = {
        "properties": {
            "name":                  {"title": [{"type": "text", "text": {"content": game['name']}}]},
            "playtime":              {"number": playtime},
            "last play":             {"date": {"start": last_played}},
            "store url":             {"url": store_url},
            "completion":            {"number": completion},
            "total achievements":    {"number": total_ach},
            "achieved achievements": {"number": achieved_ach},
            "review":                {"rich_text": [{"type": "text", "text": {"content": review_text}}]},
            "info":                  {"rich_text": [{"type": "text", "text": {"content": steam_store_data["info"]}}]},
            "tags":                  {"multi_select": steam_store_data['tag']},
            "platform":              {"multi_select": [{"name": "Steam"}]},
        },
        "cover": {"type": "external", "external": {"url": cover_url}},
        "icon":  {"type": "external", "external": {"url": icon_url}},
    }

    try:
        response = send_request_with_retry(url, headers=headers, json_data=data, method="patch")
        logger.info(f"{game['name']} updated.")
        return response.json()
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}, response: {response.text}, retrying...")


# ─────────────────────────────────────────────
# MISC
# ─────────────────────────────────────────────
def is_record(game, achievements):
    not_record_time = "2020-01-01 00:00:00"
    timestamp       = time.mktime(time.strptime(not_record_time, "%Y-%m-%d %H:%M:%S"))
    playtime        = round(float(game["playtime_forever"]) / 60, 1)

    if (playtime < 0.1 and achievements["total"] < 1) or (
        game["rtime_last_played"] < timestamp
        and achievements["total"] < 1
        and playtime < 6
    ):
        logger.info(f"{game['name']} does not meet filter rule, skipping.")
        return False
    return True


def get_achievements_count(game):
    achievements_info = {"total": 0, "achieved": 0}
    game_achievements = query_achievements_info_from_steam(game)

    if game_achievements is None or not game_achievements["playerstats"]["success"]:
        achievements_info = {"total": -1, "achieved": -1}
        logger.info(f"No achievement info for {game['name']}")
    elif "achievements" not in game_achievements["playerstats"]:
        achievements_info = {"total": -1, "achieved": -1}
        logger.info(f"No achievements for {game['name']}")
    else:
        for a in game_achievements["playerstats"]["achievements"]:
            achievements_info["total"] += 1
            if a["achieved"]:
                achievements_info["achieved"] += 1
        logger.info(f"{game['name']} achievements counted.")

    return achievements_info


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    logger = logging.getLogger("")
    logger.setLevel(logging.INFO)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    if args.debug:
        logger.addHandler(logging.FileHandler("app.log", encoding="utf-8"))
    logger.addHandler(logging.StreamHandler())

    owned_game_data = get_owned_game_data_from_steam()

    for game in owned_game_data["response"]["games"]:
        achievements_info = get_achievements_count(game)
        review_text       = get_steam_review_info(game["appid"], STEAM_USER_ID)
        steam_store_data  = get_steam_store_info(game["appid"])

        if "rtime_last_played" not in game:
            logger.info(f"{game['name']} has no last play time, setting to 0.")
            game["rtime_last_played"] = 0

        if enable_filter == "true" and not is_record(game, achievements_info):
            continue

        queryed_item = query_item_from_notion_database(game)
        if "results" not in queryed_item:
            logger.error(f"{game['name']} query failed, skipping.")
            continue

        if queryed_item["results"]:
            if enable_item_update == "true":
                logger.info(f"{game['name']} already exists, updating.")
                update_item_to_notion_database(
                    queryed_item["results"][0]["id"], game, achievements_info, review_text, steam_store_data
                )
            else:
                logger.info(f"{game['name']} already exists, skipping.")
        else:
            logger.info(f"{game['name']} not found, creating.")
            add_item_to_notion_database(game, achievements_info, review_text, steam_store_data)
