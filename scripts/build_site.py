#!/usr/bin/env python3
"""Build the SPIEL Essen 2026 site from BGG GeekPreview and GeekList data.

GeekPreview #93 is the complete show catalogue. GeekList #380039 is overlaid
as the pickup/pre-order source. The builder validates every API page before it
writes index.html, so a partial BGG response cannot replace a complete site.
"""

import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PREVIEW_ID = 93
GEEKLIST_ID = 380039
API_ROOT = "https://api.geekdo.com"
ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "index.html"
TEMPLATE_HTML = ROOT / "site_template.html"
USER_AGENT = (
    "essen-2026-show-list-bot/3.0 "
    "(+https://github.com/bu105330-glitch/essen-2026-preorder)"
)
TAIPEI = timezone(timedelta(hours=8))


def get_json(url, retries=5, timeout=45):
    """Fetch JSON with bounded retries for transient BGG/CDN failures."""
    last_error = None
    for attempt in range(retries):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
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


def integer(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def number(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def nested_item(value):
    return (value or {}).get("item") or {}


def link_names(detail, link_type):
    links = (detail.get("links") or {}).get(link_type) or []
    return [link.get("name") for link in links if link.get("name")]


def game_weight(detail):
    dynamic = nested_item(detail.get("dynamicinfo"))
    polls = dynamic.get("polls") or {}
    weight_poll = polls.get("boardgameweight") or {}
    value = number(weight_poll.get("averageweight"))
    if value is None:
        value = number((dynamic.get("stats") or {}).get("avgweight"))
    return round(value, 2) if value is not None and value > 0 else None


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
    return float((match.group(1) or match.group(2)).replace(",", ".")) if match else None


def extract_preorder_url(line, body, detail=None):
    urls = extract_urls(line) or extract_urls(body)
    version_url = ((detail or {}).get("versioninfo") or {}).get("orderurl")
    if version_url:
        urls.append(version_url)
    return next((url for url in urls if url), None)


def parallel_pages(url_pattern, page_count, workers=10):
    """Return API page payloads in page-number order."""
    pages = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(get_json, url_pattern.format(page=page)): page for page in range(1, page_count + 1)}
        for future in as_completed(futures):
            page = futures[future]
            pages[page] = future.result()
    if len(pages) != page_count:
        raise RuntimeError(f"Pagination incomplete: expected {page_count} pages, received {len(pages)}")
    return [pages[page] for page in range(1, page_count + 1)]


def fetch_preview():
    metadata = get_json(f"{API_ROOT}/api/geekpreviews?previewid={PREVIEW_ID}&nosession=1")
    config = metadata.get("config") or {}
    expected_items = int(config.get("numitems") or 0)
    item_pages = int(config.get("numpages") or 0)
    expected_parents = int(config.get("numparentitems") or 0)
    parent_pages = int(config.get("numparentitempages") or 0)
    if not all((expected_items, item_pages, expected_parents, parent_pages)):
        raise RuntimeError("GeekPreview metadata is missing pagination totals")

    item_payloads = parallel_pages(
        f"{API_ROOT}/api/geekpreviewitems?previewid={PREVIEW_ID}&pageid={{page}}&nosession=1",
        item_pages,
    )
    items = [item for payload in item_payloads for item in (payload or [])]
    if len(items) != expected_items:
        raise RuntimeError(f"GeekPreview incomplete: expected {expected_items} items, received {len(items)}")
    item_ids = [integer(item.get("itemid")) for item in items]
    if None in item_ids or len(set(item_ids)) != expected_items:
        raise RuntimeError("GeekPreview returned missing or duplicate preview item IDs")

    parent_payloads = parallel_pages(
        f"{API_ROOT}/api/geekpreviewparentitems?previewid={PREVIEW_ID}&pageid={{page}}&nosession=1",
        parent_pages,
        workers=6,
    )
    parents = [item for payload in parent_payloads for item in (payload or [])]
    parent_ids = {integer(item.get("parentitemid")) for item in parents}
    parent_ids.discard(None)
    if len(parent_ids) != expected_parents:
        raise RuntimeError(f"GeekPreview parent list incomplete: expected {expected_parents}, received {len(parent_ids)}")
    return metadata, items, parents


def parent_maps(parents):
    by_preview_item = {}
    for parent in parents:
        detail = nested_item(parent.get("geekitem"))
        parent_info = {
            "publisher": ((detail.get("primaryname") or {}).get("name") or "Unknown / Unsorted").strip(),
            "booth": parent.get("location") or None,
        }
        for preview_item_id in parent.get("previewitemids") or []:
            by_preview_item.setdefault(integer(preview_item_id), parent_info)
    return by_preview_item


def preview_game(item, preview_parent):
    detail = nested_item(item.get("geekitem"))
    version = nested_item(item.get("version"))
    primary_name = (detail.get("primaryname") or {}).get("name")
    version_name = version.get("linkedname") or version.get("versionname") or version.get("name")
    publisher_data = item.get("publishers") or []
    publisher = None
    if publisher_data:
        publisher = ((nested_item(publisher_data[0]).get("primaryname") or {}).get("name"))
    publisher = (publisher or (preview_parent or {}).get("publisher") or "Unknown / Unsorted").strip()
    images = detail.get("images") or {}
    version_images = version.get("images") or {}
    thumbnail = (item.get("thumbnail") or {}).get("src")
    thing_id = integer(item.get("objectid"))
    href = detail.get("href") or ""
    canonical = detail.get("canonical_link") or (f"https://boardgamegeek.com{href}" if href else None)
    return {
        "name": primary_name or version_name or f"BGG {thing_id}",
        "year": integer(detail.get("yearpublished")) or integer(version.get("yearpublished")),
        "image": thumbnail or images.get("previewthumb") or images.get("thumb") or images.get("original") or version_images.get("thumb"),
        "categories": link_names(detail, "boardgamecategory"),
        "thing": thing_id,
        "bgg_url": canonical or (f"https://boardgamegeek.com/boardgame/{thing_id}" if thing_id else None),
        "designers": link_names(detail, "boardgamedesigner"),
        "min_players": integer(detail.get("minplayers")),
        "max_players": integer(detail.get("maxplayers")),
        "min_age": integer(detail.get("minage")),
        "bgg_weight": game_weight(detail),
        "hot_score": integer((item.get("reactions") or {}).get("thumbs")) or 0,
        "publisher": publisher,
        "booth": item.get("location") or (preview_parent or {}).get("booth") or None,
        "in_preview": True,
        "preview_url": f"https://boardgamegeek.com{item.get('href')}" if item.get("href") else None,
        "availability": item.get("pretty_availability_status") or item.get("availability_status"),
        "show_price": number(item.get("showprice")),
        "show_price_currency": item.get("showprice_currency") or None,
        "msrp": number(item.get("msrp")),
        "msrp_currency": item.get("msrp_currency") or None,
        "pickup": False,
        "preorder_url": None,
        "price_eur": None,
        "geeklist_url": None,
    }


def fetch_list_entries():
    first = get_json(f"{API_ROOT}/api/listitems?listid={GEEKLIST_ID}&page=1")
    pagination = first.get("pagination") or {}
    per_page = int(pagination.get("perPage") or 25)
    total = int(pagination.get("total") or len(first.get("data") or []))
    page_count = max(1, (total + per_page - 1) // per_page)
    payloads = [first]
    if page_count > 1:
        payloads.extend(parallel_pages(
            f"{API_ROOT}/api/listitems?listid={GEEKLIST_ID}&page={{page}}",
            page_count,
            workers=6,
        )[1:])
    entries = [entry for payload in payloads for entry in (payload.get("data") or [])]
    if len(entries) < total:
        raise RuntimeError(f"GeekList incomplete: expected {total} entries, received {len(entries)}")
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
                references.append({
                    "thing": int(match.group(1)),
                    "listed_name": match.group(2).strip(),
                    "publisher": publisher,
                    "booth": booth,
                    "line": line,
                    "body": body,
                    "geeklist_url": item_url,
                })
    if not references:
        raise RuntimeError("GeekList returned no linked board games")
    return references


def load_existing_unlinked():
    if not INDEX_HTML.exists():
        return []
    html_text = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r"^const DATA = (.*);$", html_text, flags=re.MULTILINE)
    if not match:
        return []
    output = []
    for group in json.loads(match.group(1)):
        for game in group.get("games") or []:
            if not game.get("thing") and game.get("pickup", True):
                preserved = dict(game)
                preserved["publisher"] = game.get("publisher") or group.get("publisher")
                preserved["booth"] = game.get("booth") or group.get("booth")
                preserved["pickup"] = True
                preserved["in_preview"] = False
                output.append(preserved)
    return output


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


def preorder_only_game(reference, detail):
    images = detail.get("images") or {}
    thing_id = reference["thing"]
    return {
        "name": detail.get("name") or reference["listed_name"] or f"BGG {thing_id}",
        "year": integer(detail.get("yearpublished")),
        "image": images.get("previewthumb") or images.get("thumb") or images.get("original") or detail.get("imageurl"),
        "categories": link_names(detail, "boardgamecategory"),
        "thing": thing_id,
        "bgg_url": detail.get("canonical_link") or f"https://boardgamegeek.com/boardgame/{thing_id}",
        "designers": link_names(detail, "boardgamedesigner"),
        "min_players": integer(detail.get("minplayers")),
        "max_players": integer(detail.get("maxplayers")),
        "min_age": integer(detail.get("minage")),
        "bgg_weight": game_weight(detail),
        "hot_score": 0,
        "publisher": reference["publisher"],
        "booth": reference["booth"],
        "in_preview": False,
        "preview_url": None,
        "availability": None,
        "show_price": None,
        "show_price_currency": None,
        "msrp": None,
        "msrp_currency": None,
        "pickup": True,
        "preorder_url": extract_preorder_url(reference["line"], reference["body"], detail),
        "price_eur": extract_price(reference["line"]),
        "geeklist_url": reference["geeklist_url"],
    }


def merge_sources(preview_items, parents, references, existing_unlinked):
    by_preview_item = parent_maps(parents)
    games = [preview_game(item, by_preview_item.get(integer(item.get("itemid")))) for item in preview_items]
    by_thing = {}
    for game in games:
        if game.get("thing"):
            by_thing.setdefault(game["thing"], []).append(game)

    references_by_thing = {}
    for reference in references:
        references_by_thing.setdefault(reference["thing"], []).append(reference)
    missing_ids = sorted(set(references_by_thing) - set(by_thing))
    missing_details = fetch_details(missing_ids) if missing_ids else {}

    for thing_id, thing_references in references_by_thing.items():
        if thing_id in by_thing:
            candidates = by_thing[thing_id]
            for reference in thing_references:
                publisher_key = normalize_key(reference.get("publisher"))
                game = next(
                    (
                        candidate for candidate in candidates
                        if normalize_key(candidate.get("publisher")) == publisher_key and not candidate.get("pickup")
                    ),
                    None,
                ) or next((candidate for candidate in candidates if not candidate.get("pickup")), candidates[0])
                game["pickup"] = True
                game["preorder_url"] = extract_preorder_url(reference["line"], reference["body"])
                game["price_eur"] = extract_price(reference["line"])
                game["geeklist_url"] = reference["geeklist_url"]
                if not game.get("booth"):
                    game["booth"] = reference.get("booth")
        else:
            reference = thing_references[0]
            game = preorder_only_game(reference, missing_details.get(thing_id) or {})
            games.append(game)
            by_thing[thing_id] = [game]

    known_unlinked = {(normalize_key(game.get("publisher")), normalize_key(game.get("name"))) for game in games}
    for game in existing_unlinked:
        key = (normalize_key(game.get("publisher")), normalize_key(game.get("name")))
        if key not in known_unlinked:
            games.append(game)
            known_unlinked.add(key)
    return games, len(missing_ids)


def grouped_data(games):
    groups = {}
    order = []
    for game in games:
        publisher = game.get("publisher") or "Unknown / Unsorted"
        if publisher not in groups:
            groups[publisher] = {"publisher": publisher, "booth": game.get("booth"), "games": []}
            order.append(publisher)
        groups[publisher]["games"].append(game)
        if not groups[publisher].get("booth") and game.get("booth"):
            groups[publisher]["booth"] = game["booth"]
    return [groups[publisher] for publisher in order]


def render_site(data, metadata, list_entry_count):
    template = TEMPLATE_HTML.read_text(encoding="utf-8")
    site_meta = {
        "updated": datetime.now(TAIPEI).strftime("%Y/%m/%d"),
        "preview_count": int((metadata.get("config") or {}).get("numitems") or 0),
        "geeklist_entries": list_entry_count,
    }
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    meta_json = json.dumps(site_meta, ensure_ascii=False, separators=(",", ":"))
    if template.count("const DATA = [];") != 1 or template.count("const SITE_META = {};") != 1:
        raise RuntimeError("site_template.html is missing a unique data placeholder")
    return template.replace("const DATA = [];", f"const DATA = {data_json};").replace(
        "const SITE_META = {};", f"const SITE_META = {meta_json};"
    )


def main():
    existing_unlinked = load_existing_unlinked()
    print("Fetching all GeekPreview #93 pages...")
    metadata, preview_items, parents = fetch_preview()
    print(f"Validated {len(preview_items)} preview items and {len(parents)} publisher records.")

    print("Fetching GeekList #380039 pickup entries...")
    entries, list_pages = fetch_list_entries()
    references = parse_game_references(entries)
    unique_pickup_ids = {reference["thing"] for reference in references}
    print(f"Found {len(entries)} GeekList entries across {list_pages} pages and {len(unique_pickup_ids)} linked pickups.")

    games, preorder_only_count = merge_sources(preview_items, parents, references, existing_unlinked)
    data = grouped_data(games)
    preview_count = sum(1 for game in games if game.get("in_preview"))
    pickup_count = sum(1 for game in games if game.get("pickup"))
    expected_preview = int((metadata.get("config") or {}).get("numitems") or 0)
    if preview_count != expected_preview:
        raise RuntimeError(f"Merged preview count mismatch: expected {expected_preview}, received {preview_count}")
    if pickup_count < len(unique_pickup_ids):
        raise RuntimeError("Merged site contains fewer pickup games than the GeekList source")

    updated_html = render_site(data, metadata, len(entries))
    INDEX_HTML.write_text(updated_html, encoding="utf-8", newline="\n")
    print(
        f"Updated index.html: {preview_count} preview items + {preorder_only_count} linked preorder-only items "
        f"= {len(games)} total; {pickup_count} pickup items; {len(data)} publishers."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
