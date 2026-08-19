#!/usr/bin/env python3
"""
Daily updater for the Essen 2026 preorder site.

Fetches the BoardGameGeek geeklist "Essen 2026 Preorder Pickups"
(https://boardgamegeek.com/geeklist/380039/essen-2026-preorder-pickups),
groups items by publisher/booth (parsed from each item's free-text body,
which is how the original list author annotates publisher + booth),
enriches each game with year/image/categories from the BGG thing API,
and rewrites the `const DATA = [...]` line plus the "資料整理時間" date
inside index.html.

Designed to run on GitHub Actions (unrestricted outbound network). If a
step fails partway (e.g. BGG rate limiting), the script exits non-zero
without touching index.html, so a bad run never overwrites good data.
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

GEEKLIST_ID = 380039
GEEKLIST_URL = f"https://boardgamegeek.com/xmlapi/geeklist/{GEEKLIST_ID}?comments=0"
THING_URL = "https://boardgamegeek.com/xmlapi2/thing"
INDEX_HTML = "index.html"
USER_AGENT = "essen-2026-preorder-site-bot/1.0 (+https://github.com/)"

TAIPEI = timezone(timedelta(hours=8))


def http_get(url, params=None, retries=4, timeout=30):
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                url, params=params, headers={"User-Agent": USER_AGENT}, timeout=timeout
            )
            # BGG returns 202 while it prepares an export; poll a bit.
            if resp.status_code == 202:
                time.sleep(3)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed to GET {url}: {last_err}")


def parse_publisher_booth(body_text):
    """Best-effort extraction of 'Publisher: X' / 'Booth: Y' lines from a
    geeklist item's free-text body. Falls back to (None, None) when the
    annotation isn't present, so callers can bucket into 'Unknown'."""
    publisher = None
    booth = None
    if not body_text:
        return publisher, booth
    for line in body_text.splitlines():
        line = line.strip()
        m = re.match(r"(?i)^publisher\s*[:\-]\s*(.+)$", line)
        if m:
            publisher = m.group(1).strip() or None
            continue
        m = re.match(r"(?i)^booth\s*[:\-]\s*(.+)$", line)
        if m:
            val = m.group(1).strip()
            booth = None if val.lower() in ("", "n/a", "none", "tbd") else val
    return publisher, booth


def fetch_geeklist_items():
    resp = http_get(GEEKLIST_URL)
    root = ET.fromstring(resp.text)
    items = []
    for item in root.findall("item"):
        object_id = item.attrib.get("objectid")
        object_name = item.attrib.get("objectname")
        subtype = item.attrib.get("subtype", "")
        if not object_id or subtype != "boardgame":
            continue
        body_node = item.find("body")
        body_text = body_node.text if body_node is not None else ""
        publisher, booth = parse_publisher_booth(body_text)
        items.append(
            {
                "thing": int(object_id),
                "name": object_name,
                "publisher": publisher or "Unknown / Unsorted",
                "booth": booth,
            }
        )
    return items


def fetch_thing_details(thing_ids):
    """Batch-fetch year/image/categories for a list of BGG thing ids."""
    details = {}
    ids = list(dict.fromkeys(thing_ids))  # de-dupe, keep order
    for i in range(0, len(ids), 20):
        batch = ids[i : i + 20]
        resp = http_get(THING_URL, params={"id": ",".join(str(x) for x in batch)})
        root = ET.fromstring(resp.text)
        for item in root.findall("item"):
            thing_id = int(item.attrib.get("id"))
            name_node = item.find("./name[@type='primary']")
            year_node = item.find("yearpublished")
            image_node = item.find("image")
            categories = [
                link.attrib.get("value")
                for link in item.findall("link[@type='boardgamecategory']")
            ]
            details[thing_id] = {
                "name": (name_node.attrib.get("value") if name_node is not None else None),
                "year": (int(year_node.attrib.get("value")) if year_node is not None else None),
                "image": (image_node.text if image_node is not None else None),
                "categories": categories,
            }
        time.sleep(2)  # be polite to BGG between batches
    return details


def build_data(items, details):
    grouped = {}
    order = []
    for it in items:
        pub = it["publisher"]
        if pub not in grouped:
            grouped[pub] = {"publisher": pub, "booth": it["booth"], "games": []}
            order.append(pub)
        elif grouped[pub]["booth"] is None and it["booth"]:
            grouped[pub]["booth"] = it["booth"]

        d = details.get(it["thing"], {})
        grouped[pub]["games"].append(
            {
                "name": d.get("name") or it["name"],
                "year": d.get("year"),
                "image": d.get("image"),
                "categories": d.get("categories") or [],
                "thing": it["thing"],
                "bgg_url": f"https://boardgamegeek.com/boardgame/{it['thing']}",
            }
        )
    return [grouped[pub] for pub in order]


def replace_data_line(html_text, data):
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    new_line = f"const DATA = {data_json};"
    pattern = re.compile(r"^const DATA = .*;$", re.MULTILINE)
    if not pattern.search(html_text):
        raise RuntimeError("Could not find `const DATA = ...;` line in index.html")
    return pattern.sub(lambda _m: new_line, html_text, count=1)


def replace_update_date(html_text):
    today = datetime.now(TAIPEI).strftime("%Y/%m/%d")
    pattern = re.compile(r"資料整理時間：\d{4}/\d{2}/\d{2}")
    if not pattern.search(html_text):
        return html_text
    return pattern.sub(f"資料整理時間：{today}", html_text, count=1)


def main():
    print("Fetching geeklist items...")
    items = fetch_geeklist_items()
    if not items:
        raise RuntimeError("Geeklist returned zero boardgame items — aborting without writing.")
    print(f"Found {len(items)} geeklist items across publishers.")

    print("Fetching BGG thing details...")
    details = fetch_thing_details([it["thing"] for it in items])

    data = build_data(items, details)
    total_games = sum(len(p["games"]) for p in data)
    print(f"Built DATA for {len(data)} publishers / {total_games} games.")

    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        html = f.read()

    html = replace_data_line(html, data)
    html = replace_update_date(html)

    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print("index.html updated.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
