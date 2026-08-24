#!/usr/bin/env python3
"""Refresh the Essen 2026 preorder site from BGG GeekList #380039.

The legacy GeekList XML endpoint now requires authorization and caused every
scheduled run to fail. This updater uses the JSON endpoints that power the
public GeekList, preserves manually listed games that do not yet have a BGG
thing ID, enriches linked games, and only writes index.html after a complete
successful refresh.
"""

import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GEEKLIST_ID = 380039
API_ROOT = "https://api.geekdo.com"
INDEX_HTML = "index.html"
USER_AGENT = (
    "essen-2026-preorder-site-bot/2.0 "
    "(+https://github.com/bu105330-glitch/essen-2026-preorder)"
)
TAIPEI = timezone(timedelta(hours=8))


def get_json(url, retries=4, timeout=30):
    """Fetch JSON with bounded retries for transient BGG/CDN failures."""
    last_error = None
    for attempt in range(retries):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if isinstance(error, HTTPError) and error.code not in (408, 429, 500, 502, 503, 504):
                break
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def normalize_key(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def extract_booth(text):
    patterns = (
        r"(?:stand|booth|hall(?:e)?)\D{0,35}([1-8]\s*[-/]?\s*[A-Z]\s*\d{2,4})",
        r"\b([1-8]\s*[-/]?\s*[A-Z]\s*\d{2,4})\b",
        r"\b(H\s*[1-8]\s*[A-Z]\s*\d{2,4})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", "", match.group(1)).replace("/", "-")
    return None


def extract_urls(text):
    urls = []
    for match in re.finditer(r"\[url=(https?://[^\]\s]+)[^\]]*\]", text or "", re.IGNORECASE):
        urls.append(match.group(1).replace("&amp;", "&"))
    for match in re.finditer(r"(?<!\[url=)(https?://[^\s\[]+)", text or "", re.IGNORECASE):
        urls.append(match.group(1).rstrip(".,;)").replace("&amp;", "&"))
    return list(dict.fromkeys(urls))


def extract_price(text):
    match = re.search(
        r"(?:€|EUR|Euro)\s*(\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s*(?:€|EUR|Euro)",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return float((match.group(1) or match.group(2)).replace(",", "."))


def extract_preorder_url(line, body, detail):
    urls = extract_urls(line) or extract_urls(body)
    version_url = (detail.get("versioninfo") or {}).get("orderurl")
    if version_url:
        urls.append(version_url)
    return next((url for url in urls if url), None)


def load_existing_state(html_text):
    match = re.search(r"^const DATA = (.*);$", html_text, flags=re.MULTILINE)
    if not match:
        raise RuntimeError("Could not find the DATA block in index.html")
    data = json.loads(match.group(1))
    by_thing = {}
    unlinked_by_publisher = {}
    for publisher in data:
        publisher_key = normalize_key(publisher.get("publisher"))
        for game in publisher.get("games", []):
            thing = game.get("thing")
            if thing:
                by_thing[int(thing)] = game
            else:
                unlinked_by_publisher.setdefault(publisher_key, []).append(game)
    return by_thing, unlinked_by_publisher


def fetch_list_entries():
    first = get_json(f"{API_ROOT}/api/listitems?listid={GEEKLIST_ID}&page=1")
    pagination = first.get("pagination") or {}
    per_page = int(pagination.get("perPage") or 25)
    total = int(pagination.get("total") or len(first.get("data") or []))
    page_count = max(1, (total + per_page - 1) // per_page)
    payloads = [first]
    for page in range(2, page_count + 1):
        payloads.append(get_json(f"{API_ROOT}/api/listitems?listid={GEEKLIST_ID}&page={page}"))
    entries = [entry for payload in payloads for entry in (payload.get("data") or [])]
    if len(entries) < total:
        raise RuntimeError(f"GeekList pagination incomplete: expected {total}, received {len(entries)}")
    return entries, page_count


def parse_game_references(entries):
    references = []
    for entry in entries:
        publisher = ((entry.get("item") or {}).get("name") or "Unknown / Unsorted").strip()
        body = entry.get("body") or ""
        booth = extract_booth(body)
        item_url = f"https://boardgamegeek.com{entry.get('href', '')}"
        for line in body.splitlines():
            for match in re.finditer(r"\[thing=(\d+)\]([^[]*)\[/thing\]", line, re.IGNORECASE):
                references.append(
                    {
                        "thing": int(match.group(1)),
                        "listed_name": match.group(2).strip(),
                        "publisher": publisher,
                        "booth": booth,
                        "line": line,
                        "body": body,
                        "geeklist_url": item_url,
                    }
                )
    if not references:
        raise RuntimeError("GeekList returned no linked board games")
    return references


def fetch_details(thing_ids):
    def fetch_one(thing_id):
        payload = get_json(f"{API_ROOT}/api/geekitems?objecttype=thing&objectid={thing_id}")
        return thing_id, payload.get("item") or {}

    details = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_one, thing_id) for thing_id in thing_ids]
        for future in as_completed(futures):
            thing_id, detail = future.result()
            details[thing_id] = detail
    return details


def link_names(detail, link_type):
    links = (detail.get("links") or {}).get(link_type) or []
    return [link.get("name") for link in links if link.get("name")]


def build_data(entries, references, details, existing_by_thing, unlinked_by_publisher):
    refs_by_publisher = {}
    for reference in references:
        refs_by_publisher.setdefault(reference["publisher"], []).append(reference)

    output = []
    seen_things = set()
    for entry in entries:
        publisher = ((entry.get("item") or {}).get("name") or "Unknown / Unsorted").strip()
        body = entry.get("body") or ""
        publisher_games = []
        for reference in refs_by_publisher.get(publisher, []):
            thing_id = reference["thing"]
            if thing_id in seen_things:
                continue
            seen_things.add(thing_id)
            detail = details.get(thing_id) or {}
            fallback = existing_by_thing.get(thing_id) or {}
            image_set = detail.get("images") or {}
            year_value = detail.get("yearpublished")
            try:
                year_value = int(year_value) if year_value else fallback.get("year")
            except (TypeError, ValueError):
                year_value = fallback.get("year")
            publisher_games.append(
                {
                    "name": detail.get("name") or reference["listed_name"] or fallback.get("name") or f"BGG {thing_id}",
                    "year": year_value,
                    "image": image_set.get("original") or detail.get("imageurl") or fallback.get("image"),
                    "categories": link_names(detail, "boardgamecategory") or fallback.get("categories") or [],
                    "thing": thing_id,
                    "bgg_url": detail.get("canonical_link") or fallback.get("bgg_url") or f"https://boardgamegeek.com/boardgame/{thing_id}",
                    "designers": link_names(detail, "boardgamedesigner") or fallback.get("designers") or [],
                    "preorder_url": extract_preorder_url(reference["line"], reference["body"], detail),
                    "price_eur": extract_price(reference["line"]),
                    "geeklist_url": reference["geeklist_url"],
                }
            )

        publisher_key = normalize_key(publisher)
        publisher_games.extend(unlinked_by_publisher.get(publisher_key, []))
        output.append(
            {
                "publisher": publisher,
                "booth": extract_booth(body),
                "games": publisher_games,
            }
        )
    return output


def replace_site_data(html_text, data):
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html_text, data_count = re.subn(
        r"^const DATA = .*;$",
        lambda _match: f"const DATA = {data_json};",
        html_text,
        count=1,
        flags=re.MULTILINE,
    )
    if data_count != 1:
        raise RuntimeError("Could not replace the DATA block in index.html")
    today = datetime.now(TAIPEI).strftime("%Y/%m/%d")
    html_text, date_count = re.subn(
        r"資料整理時間：\d{4}/\d{2}/\d{2}",
        f"資料整理時間：{today}",
        html_text,
        count=1,
    )
    if date_count != 1:
        raise RuntimeError("Could not replace the update date in index.html")
    return html_text


def main():
    with open(INDEX_HTML, "r", encoding="utf-8") as file:
        original_html = file.read()
    existing_by_thing, unlinked_by_publisher = load_existing_state(original_html)

    print("Fetching GeekList entries from Geekdo...")
    entries, page_count = fetch_list_entries()
    references = parse_game_references(entries)
    unique_ids = list(dict.fromkeys(reference["thing"] for reference in references))
    print(f"Found {len(entries)} publisher entries across {page_count} pages and {len(unique_ids)} linked games.")

    print("Fetching current BGG game details...")
    details = fetch_details(unique_ids)
    if len(details) != len(unique_ids):
        raise RuntimeError(f"Incomplete BGG details: expected {len(unique_ids)}, received {len(details)}")

    data = build_data(entries, references, details, existing_by_thing, unlinked_by_publisher)
    total_games = sum(len(publisher["games"]) for publisher in data)
    if total_games < len(unique_ids):
        raise RuntimeError("Built site contains fewer games than the GeekList source")

    updated_html = replace_site_data(original_html, data)
    with open(INDEX_HTML, "w", encoding="utf-8", newline="\n") as file:
        file.write(updated_html)
    print(f"Updated index.html with {len(data)} publishers / {total_games} games.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
