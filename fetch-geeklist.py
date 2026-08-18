import requests
from bs4 import BeautifulSoup
import json
import time

GEEKLIST_ID = 380039

def fetch_geeklist():
    all_items = []
    page = 1

    while True:
        url = f"https://boardgamegeek.com/geeklist/{GEEKLIST_ID}?page={page}"

        print(f"Fetching page {page}...")

        response = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
            timeout=30
        )

        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # 先找頁面中所有可能的 BGG boardgame links
        links = soup.find_all("a", href=True)

        page_items = []
        seen_ids = set()

        for link in links:
            href = link.get("href", "")

            if "/boardgame/" not in href:
                continue

            parts = href.split("/boardgame/")

            if len(parts) < 2:
                continue

            rest = parts[1].split("/")
            bgg_id = rest[0]

            if not bgg_id.isdigit():
                continue

            if bgg_id in seen_ids:
                continue

            title = link.get_text(strip=True)

            if not title:
                continue

            seen_ids.add(bgg_id)

            page_items.append({
                "bggId": int(bgg_id),
                "title": title,
                "sourcePage": page,
                "bggUrl": f"https://boardgamegeek.com/boardgame/{bgg_id}"
            })

        print(f"Found {len(page_items)} candidate games on page {page}")

        # 如果完全抓不到東西，就停止
        if not page_items:
            break

        all_items.extend(page_items)

        page += 1

        # 避免無限抓
        if page > 20:
            break

        time.sleep(3)

    return all_items


if __name__ == "__main__":
    items = fetch_geeklist()

    with open(
        "geeklist-games.json",
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            items,
            f,
            ensure_ascii=False,
            indent=2
        )

    print(f"Saved {len(items)} games to geeklist-games.json")
