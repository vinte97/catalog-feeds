#!/usr/bin/env python3
"""Build a B24U YML feed for the Glonass24 catalog.

Source: public WooCommerce Store API of https://glonass24.com/katalog/
(198 published products). Retail prices are not published — the storefront
shows «Цена по запросу» and the API price is 0. B24U drops offers that have
no <price> (FEED_ALL_FILTERED), so the feed writes <price>0</price> and
appends «Цена: по запросу». A real numeric price, if the API ever returns
one, is written as rubles (the API amount is in minor units).

Photos come only from the product gallery, not from related cards or logos.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://glonass24.com"
SHOP_NAME = "Глонасс24"
# Уточнение 2026-10-14, пользователь: весь каталог https://glonass24.com/katalog/,
# YML на GitHub Pages для maia24. Бренды в API пустые — vendor = имя магазина.
VENDOR = "Глонасс24"
PRODUCTS_URL = "https://glonass24.com/wp-json/wc/store/v1/products"
CATEGORIES_URL = "https://glonass24.com/wp-json/wp/v2/product_cat"
USER_AGENT = "Mozilla/5.0 (compatible; Glonass24CatalogFeed/1.0; +https://glonass24.com/katalog/)"
TIMEOUT_SECONDS = 25
PER_PAGE = 100
WHITESPACE_RE = re.compile(r"\s+")
PICTURE_EXT = (".jpg", ".jpeg", ".png", ".webp")
PICTURE_SKIP = ("logo", "icon", "banner", "placeholder", "no_photo", "no-photo", "sprite")
ARTICLE_LABELS = {"артикул", "артикул товара", "код", "код товара", "sku"}


def clean_text(value: str | None) -> str:
    return WHITESPACE_RE.sub(" ", html.unescape(value or "")).strip()


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def fetch_pages(session: requests.Session, url: str) -> list[dict]:
    page = 1
    rows: list[dict] = []
    while True:
        response = session.get(
            url,
            params={"per_page": PER_PAGE, "page": page},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        batch = response.json()
        if not isinstance(batch, list):
            raise RuntimeError(f"Unexpected payload from {url}: {type(batch).__name__}")
        rows.extend(batch)
        total_pages = int(response.headers.get("X-WP-TotalPages") or 1)
        if page >= total_pages or not batch:
            break
        page += 1
    return rows


def html_to_text(raw: str | None) -> tuple[str, list[tuple[str, str]]]:
    """Plain text plus label/value pairs taken from specification tables."""
    if not raw:
        return "", []
    soup = BeautifulSoup(raw, "lxml")
    for tag in soup.select("script, style, img"):
        tag.decompose()
    pairs: list[tuple[str, str]] = []
    for table in soup.find_all("table"):
        lines: list[str] = []
        for row in table.find_all("tr"):
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["th", "td"])]
            cells = [cell for cell in cells if cell]
            if len(cells) >= 2:
                label, value = cells[0], cells[-1]
                if label.casefold() != value.casefold():
                    pairs.append((label, value))
                    lines.append(f"{label}: {value}")
            elif len(cells) == 1:
                lines.append(cells[0])
        table.replace_with(" ".join(lines))
    return clean_text(soup.get_text(" ", strip=True)), pairs


def format_price(prices: dict | None) -> tuple[str, bool]:
    """Return (yml price, is_on_request). API amounts are minor currency units."""
    prices = prices or {}
    raw = str(prices.get("price") or "0").strip()
    minor = int(prices.get("currency_minor_unit") or 0)
    try:
        amount = int(raw)
    except ValueError:
        return "0", True
    if amount <= 0:
        return "0", True
    if minor <= 0:
        return str(amount), False
    scale = 10 ** minor
    whole, frac = divmod(amount, scale)
    if frac == 0:
        return str(whole), False
    rendered = f"{whole}.{frac:0{minor}d}".rstrip("0").rstrip(".")
    return rendered, False


def picture_urls(images: list[dict] | None) -> list[str]:
    kept: list[str] = []
    seen: set[str] = set()
    for image in images or []:
        src = str(image.get("src") or "").strip()
        if not src.startswith("https://"):
            continue
        path = urlsplit(src).path.lower()
        if not path.endswith(PICTURE_EXT):
            continue
        name = path.rsplit("/", 1)[-1]
        if any(marker in name for marker in PICTURE_SKIP):
            continue
        if src in seen:
            continue
        seen.add(src)
        kept.append(src)
    return kept


def category_index(raw_categories: list[dict]) -> dict[int, dict]:
    index: dict[int, dict] = {}
    for row in raw_categories:
        index[int(row["id"])] = {
            "id": int(row["id"]),
            "name": clean_text(row.get("name")),
            "parent": int(row.get("parent") or 0),
        }
    return index


def category_path(cat_id: int, index: dict[int, dict]) -> list[dict]:
    chain: list[dict] = []
    seen: set[int] = set()
    current = cat_id
    while current and current not in seen and current in index:
        seen.add(current)
        node = index[current]
        chain.append(node)
        current = int(node["parent"])
    chain.reverse()
    return chain


def choose_category(product_cats: list[dict], index: dict[int, dict]) -> int | None:
    """Deepest assigned category. Ties keep the API order."""
    best_id: int | None = None
    best_depth = -1
    for row in product_cats or []:
        cat_id = int(row["id"])
        depth = len(category_path(cat_id, index))
        if depth > best_depth:
            best_id = cat_id
            best_depth = depth
    return best_id


def vendor_code(product: dict, pairs: list[tuple[str, str]]) -> str:
    sku = clean_text(str(product.get("sku") or ""))
    if sku:
        return sku
    for label, value in pairs:
        if label.casefold() in ARTICLE_LABELS and value:
            return value
    return str(product["id"])


def normalize(product: dict, index: dict[int, dict]) -> dict | None:
    name = clean_text(str((product.get("name") or "")))
    url = str(product.get("permalink") or "").strip()
    if not name or not url.startswith("https://"):
        return None
    short_text, _short_pairs = html_to_text(product.get("short_description"))
    long_text, pairs = html_to_text(product.get("description"))
    # Short text often repeats the first lines of the spec block.
    description = long_text or short_text
    if short_text and long_text and short_text not in long_text:
        description = f"{short_text} {long_text}".strip()
    price, on_request = format_price(product.get("prices"))
    cat_id = choose_category(product.get("categories") or [], index)
    path = category_path(cat_id, index) if cat_id else []
    section = " / ".join(node["name"] for node in path if node["name"])
    return {
        "id": str(product["id"]),
        "name": name,
        "url": url.split("?")[0],
        "pictures": picture_urls(product.get("images")),
        "price": price,
        "on_request": on_request,
        "available": bool(product.get("is_in_stock")),
        "category_id": str(cat_id) if cat_id else "",
        "category": section,
        "vendor": VENDOR,
        "vendor_code": vendor_code(product, pairs),
        "description": description,
    }


def used_categories(records: list[dict], index: dict[int, dict]) -> list[dict]:
    needed: dict[int, dict] = {}
    for record in records:
        if not record["category_id"]:
            continue
        for node in category_path(int(record["category_id"]), index):
            needed[int(node["id"])] = node
    return [needed[key] for key in sorted(needed)]


def append_text(parent: ET.Element, tag: str, value: object) -> ET.Element:
    child = ET.SubElement(parent, tag)
    child.text = str(value)
    return child


def offer_description(record: dict) -> str:
    description = clean_text(str(record.get("description") or ""))
    section = clean_text(str(record.get("category") or ""))
    if section and not description.startswith("Раздел:"):
        description = f"Раздел: {section}. {description}".strip()
    if record["on_request"] and "цена:" not in description.lower():
        description = f"{description} Цена: по запросу".strip()
    return description


def build_yml(records: list[dict], categories: list[dict], destination: Path) -> None:
    now = datetime.now().astimezone().replace(microsecond=0).isoformat()
    root = ET.Element("yml_catalog", {"date": now})
    shop = ET.SubElement(root, "shop")
    append_text(shop, "name", SHOP_NAME)
    append_text(shop, "company", SHOP_NAME)
    append_text(shop, "url", BASE_URL)
    append_text(shop, "platform", "BSM/Yandex/Market")

    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": "RUB", "rate": "1"})

    categories_el = ET.SubElement(shop, "categories")
    if not categories:
        append_text(categories_el, "category", "Каталог").set("id", "1")
    for node in categories:
        element = append_text(categories_el, "category", node["name"])
        element.set("id", str(node["id"]))
        parent = int(node["parent"])
        if parent and any(int(item["id"]) == parent for item in categories):
            element.set("parentId", str(parent))

    offers = ET.SubElement(shop, "offers")
    for record in records:
        # Card order for B24U photos: name, url, picture, currency, category,
        # vendor, vendorCode, price, quantity, description. No <param>.
        available = "true" if record["available"] else "false"
        offer = ET.SubElement(offers, "offer", {"id": str(record["id"]), "available": available})
        append_text(offer, "name", record["name"])
        append_text(offer, "url", record["url"])
        for picture in record["pictures"]:
            append_text(offer, "picture", picture)
        append_text(offer, "currencyId", "RUB")
        category_id = record["category_id"] or "1"
        append_text(offer, "categoryId", category_id)
        append_text(offer, "vendor", record["vendor"])
        append_text(offer, "vendorCode", record["vendor_code"])
        append_text(offer, "price", record["price"])
        # Numeric stock is not published. In-stock «по запросу» cards still
        # need <quantity>, same as the TerraFrigo / Смазка.ру feeds.
        if record["available"] and record["on_request"]:
            append_text(offer, "quantity", "1")
        append_text(offer, "description", offer_description(record))

    ET.indent(root, space="  ")
    destination.write_bytes(
        b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8")
    )


def write_jsonl(records: list[dict], destination: Path) -> None:
    with destination.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def unique_records(records: list[dict]) -> tuple[list[dict], int]:
    kept: list[dict] = []
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    skipped = 0
    for record in records:
        url = str(record["url"])
        offer_id = str(record["id"])
        if url in seen_urls:
            skipped += 1
            continue
        if offer_id in seen_ids:
            offer_id = f"{offer_id}-2"
            record = {**record, "id": offer_id}
        seen_urls.add(url)
        seen_ids.add(offer_id)
        kept.append(record)
    return kept, skipped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--limit", type=int, help="Maximum number of valid offers, for a test run.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()
    raw_categories = fetch_pages(session, CATEGORIES_URL)
    raw_products = fetch_pages(session, PRODUCTS_URL)
    index = category_index(raw_categories)
    print(f"api_products={len(raw_products)} api_categories={len(index)}", file=sys.stderr)

    records: list[dict] = []
    skipped_invalid = 0
    for product in raw_products:
        record = normalize(product, index)
        if record is None:
            skipped_invalid += 1
            continue
        records.append(record)
    records.sort(key=lambda item: str(item["url"]))
    records, skipped_dupes = unique_records(records)
    if args.limit is not None:
        records = records[: args.limit]

    categories = used_categories(records, index)
    write_jsonl(records, args.output_dir / "offers.jsonl")
    build_yml(records, categories, args.output_dir / "feed.xml")

    with_picture = sum(bool(record["pictures"]) for record in records)
    on_request = sum(bool(record["on_request"]) for record in records)
    print(
        f"offers={len(records)} with_picture={with_picture} "
        f"no_picture={len(records) - with_picture} on_request={on_request} "
        f"priced={len(records) - on_request} categories={len(categories)} "
        f"skipped_invalid={skipped_invalid} skipped_duplicate_url={skipped_dupes}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
