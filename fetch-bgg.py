import os
import json
import requests
import xml.etree.ElementTree as ET

BGG_TOKEN = os.getenv("BGG_TOKEN")

# 先放測試用的 BGG ID
GAME_IDS = [
    68448
]

def fetch_bgg_games(ids):
    url = "https://boardgamegeek.com/xmlapi2/thing"

    headers = {
        "Authorization": f"Bearer {BGG_TOKEN}"
    }

    params = {
        "id": ",".join(str(i) for i in ids),
        "stats": 1
    }

    response = requests.get(
        url,
        headers=headers,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    root = ET.fromstring(response.text)

    games = []

    for item in root.findall("item"):

        bgg_id = item.attrib.get("id")

        name_node = item.find(
            "./name[@type='primary']"
        )

        year_node = item.find("yearpublished")
        image_node = item.find("image")
        thumbnail_node = item.find("thumbnail")

        game = {
            "bggId": bgg_id,
            "title": (
                name_node.attrib.get("value")
                if name_node is not None
                else ""
            ),
            "year": (
                year_node.attrib.get("value")
                if year_node is not None
                else ""
            ),
            "image": (
                image_node.text
                if image_node is not None
                else ""
            ),
            "thumbnail": (
                thumbnail_node.text
                if thumbnail_node is not None
                else ""
            ),
            "bggUrl": (
                f"https://boardgamegeek.com/"
                f"boardgame/{bgg_id}"
            )
        }

        games.append(game)

    return games


if __name__ == "__main__":

    if not BGG_TOKEN:
        raise RuntimeError(
            "BGG_TOKEN is not configured"
        )

    games = fetch_bgg_games(GAME_IDS)

    with open(
        "games.json",
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            games,
            f,
            ensure_ascii=False,
            indent=2
        )

    print(
        f"Saved {len(games)} games to games.json"
    )
