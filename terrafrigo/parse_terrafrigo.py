#!/usr/bin/env python3
"""Build a B24U-compatible YML feed from three TerraFrigo catalog sections.

The script follows each category's pager, fetches the unique product pages,
and writes two reproducible artifacts next to this file:

* ``offers.jsonl`` – raw, normalized product records;
* ``feed.xml`` – Yandex Market YML ready for B24U import.

Public product pages do not expose a retail price. B24U's import filter
rejects offers with no <price> (FEED_ALL_FILTERED / «Нет цены»), so the feed
writes <price>0</price> as a placeholder and appends «Цена: по запросу» to
description — never invent a real amount. Cards also get available,
vendorCode, and quantity like prosps/smazki. Images come only from the main
gallery.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup, Tag


BASE_URL = "https://terrafrigo.ru"
SHOP_NAME = "ТерраФриго"
VENDOR = "TerraFrigo"
SOURCES = (
    "https://terrafrigo.ru/catalog/konditsionery-dlya-kommercheskogo-transporta/",
    "https://terrafrigo.ru/catalog/konditsionery-dlya-spetstekhniki/",
    "https://terrafrigo.ru/catalog/refrizheratory-khou/",
)
USER_AGENT = "Mozilla/5.0 (compatible; TerraFrigoCatalogFeed/1.0; +https://terrafrigo.ru/)"
TIMEOUT_SECONDS = 25
MAX_WORKERS = 6
NO_PHOTO_MARKERS = ("no_photo", "no-photo", "placeholder")
WHITESPACE_RE = re.compile(r"\s+")


def clean_text(value: str | None) -> str:
    return WHITESPACE_RE.sub(" ", value or "").strip()


def canonical_url(value: str, *, base_url: str = BASE_URL) -> str:
    """Return an absolute HTTPS URL without query string or fragment."""
    absolute = urljoin(base_url, value)
    parts = urlsplit(absolute)
    # Do not append a slash here: it would turn ``photo.jpg`` into
    # ``photo.jpg/``. Product links from the source already use a trailing
    # slash, while image URLs must retain their exact path.
    path = parts.path or "/"
    return urlunsplit(("https", parts.netloc.lower(), path, "", ""))


def is_product_url(url: str, category_url: str) -> bool:
    """Keep product cards below this category, excluding the category itself."""
    category_path = urlsplit(category_url).path.rstrip("/") + "/"
    path = urlsplit(url).path
    return path.startswith(category_path) and path != category_path


def fetch(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"})
    return session


def product_urls_from_category(html: str, category_url: str) -> list[str]:
    """Get only links belonging to ``.catalog-item`` cards on one catalog page."""
    soup = BeautifulSoup(html, "lxml")
    urls: set[str] = set()
    for card in soup.select(".catalog-item"):
        for link in card.select("a[href]"):
            url = canonical_url(link["href"])
            if is_product_url(url, category_url):
                urls.add(url)
    return sorted(urls)


def next_category_page(html: str, current_url: str) -> str | None:
    """Find the pager's explicit next page, if it exists."""
    soup = BeautifulSoup(html, "lxml")
    link = soup.select_one(".bx-pagination .bx-pag-next a[href]")
    # Pagination uses ``?PAGEN_1=…``; unlike product URLs that query string is
    # meaningful and must be retained.
    return urljoin(current_url, link["href"]) if link else None


def category_name_from_page(html: str, fallback_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    name = clean_text(soup.select_one("h1").get_text(" ", strip=True) if soup.select_one("h1") else "")
    return name or urlsplit(fallback_url).path.rstrip("/").split("/")[-1]


def product_id_from_url(url: str) -> str:
    slug = urlsplit(url).path.rstrip("/").split("/")[-1]
    return re.sub(r"[^a-z0-9_-]+", "-", slug.lower()).strip("-") or "product"


def image_urls_from_product(soup: BeautifulSoup, product_url: str) -> list[str]:
    """Extract gallery photos from the product card only, excluding placeholders."""
    gallery = soup.select_one(".card-photo #slider, .card-photo .card-slider, .card-photo")
    if not gallery:
        return []

    images: list[str] = []
    for image in gallery.select("img[src]"):
        url = canonical_url(image["src"], base_url=product_url)
        if any(marker in url.lower() for marker in NO_PHOTO_MARKERS):
            continue
        if url not in images:
            images.append(url)
    return images


def description_from_product(soup: BeautifulSoup) -> str:
    """Keep text from the product description and technical-specification tabs."""
    blocks = soup.select(".card-description, .coffee-spec-item")
    parts: list[str] = []
    for block in blocks:
        text = clean_text(block.get_text(" ", strip=True))
        if text and text not in parts:
            parts.append(text)
    return "\n\n".join(parts)


def breadcrumbs_from_product(soup: BeautifulSoup) -> list[str]:
    crumbs: list[str] = []
    for element in soup.select(".bread-crumbs [itemprop='name']"):
        value = clean_text(element.get_text(" ", strip=True))
        if value and value not in crumbs:
            crumbs.append(value)
    return crumbs


def parse_product(html: str, product_url: str, category: str) -> dict[str, object] | None:
    soup = BeautifulSoup(html, "lxml")
    heading = soup.select_one("h1")
    name = clean_text(heading.get_text(" ", strip=True) if heading else "")
    if not name or name == "----//----":
        return None

    breadcrumbs = breadcrumbs_from_product(soup)
    # The supplied catalog section is authoritative. Breadcrumbs are saved as
    # context but are not used to accidentally mix parent categories.
    return {
        "id": product_id_from_url(product_url),
        "name": name,
        "url": canonical_url(product_url),
        "pictures": image_urls_from_product(soup, product_url),
        "vendor": VENDOR,
        "category": category,
        "breadcrumbs": breadcrumbs,
        "description": description_from_product(soup),
    }


def collect_category_urls(session: requests.Session, category_url: str) -> tuple[str, list[str]]:
    """Walk every pager page of one supplied category and deduplicate products."""
    current_url = canonical_url(category_url)
    seen_pages: set[str] = set()
    products: set[str] = set()
    category_name = ""

    while current_url and current_url not in seen_pages:
        seen_pages.add(current_url)
        html = fetch(session, current_url)
        if not category_name:
            category_name = category_name_from_page(html, category_url)
        products.update(product_urls_from_category(html, category_url))
        current_url = next_category_page(html, current_url)

    return category_name or category_url, sorted(products)


def unique_ids(records: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    used: dict[str, int] = {}
    normalized: list[dict[str, object]] = []
    for record in records:
        base_id = str(record["id"])
        used[base_id] = used.get(base_id, 0) + 1
        record["id"] = base_id if used[base_id] == 1 else f"{base_id}-{used[base_id]}"
        normalized.append(record)
    return normalized


def append_text(parent: ET.Element, tag: str, value: object) -> ET.Element:
    child = ET.SubElement(parent, tag)
    child.text = str(value)
    return child


def build_yml(records: list[dict[str, object]], destination: Path) -> None:
    now = datetime.now().astimezone().replace(microsecond=0).isoformat()
    root = ET.Element("yml_catalog", {"date": now})
    shop = ET.SubElement(root, "shop")
    append_text(shop, "name", SHOP_NAME)
    append_text(shop, "company", SHOP_NAME)
    append_text(shop, "url", BASE_URL)
    append_text(shop, "platform", "BSM/Yandex/Market")

    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": "RUB", "rate": "1"})

    category_names = list(dict.fromkeys(str(item["category"]) for item in records))
    category_ids = {name: str(index) for index, name in enumerate(category_names, start=1)}
    categories = ET.SubElement(shop, "categories")
    for category_name in category_names:
        append_text(categories, "category", category_name).set("id", category_ids[category_name])

    offers = ET.SubElement(shop, "offers")
    for record in records:
        # Card order for B24U photos. No <param>.
        # Price 0 = placeholder: public site has no retail price; real amount is
        # «по запросу». Omitting <price> makes B24U drop every offer
        # (FEED_ALL_FILTERED / Нет цены).
        offer = ET.SubElement(
            offers, "offer", {"id": str(record["id"]), "available": "true"}
        )
        append_text(offer, "name", record["name"])
        append_text(offer, "url", record["url"])
        for picture in record["pictures"]:
            append_text(offer, "picture", picture)
        append_text(offer, "currencyId", "RUB")
        append_text(offer, "categoryId", category_ids[str(record["category"])])
        append_text(offer, "vendor", record["vendor"])
        append_text(offer, "vendorCode", record["id"])
        append_text(offer, "price", "0")
        append_text(offer, "quantity", "1")
        description = clean_text(str(record.get("description") or ""))
        if "цена:" not in description.lower():
            description = f"{description} Цена: по запросу".strip()
        append_text(offer, "description", description)

    ET.indent(root, space="  ")
    destination.write_bytes(b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="utf-8"))


def write_jsonl(records: Iterable[dict[str, object]], destination: Path) -> None:
    with destination.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--limit", type=int, help="Maximum number of valid offers, for a test run.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        raise SystemExit("--workers must be at least 1")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    session = make_session()

    url_categories: dict[str, str] = {}
    for source in SOURCES:
        category, urls = collect_category_urls(session, source)
        print(f"{category}: {len(urls)} product URLs", file=sys.stderr)
        for url in urls:
            url_categories.setdefault(url, category)

    def fetch_and_parse(url: str) -> dict[str, object] | None:
        # ``requests.Session`` is deliberately not shared between worker
        # threads. It is safe to parallelise six independent HTTP requests.
        with make_session() as worker_session:
            return parse_product(fetch(worker_session, url), url, url_categories[url])

    records: list[dict[str, object]] = []
    failures: list[tuple[str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, MAX_WORKERS)) as executor:
        futures = {executor.submit(fetch_and_parse, url): url for url in sorted(url_categories)}
        for future in concurrent.futures.as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
            except requests.RequestException as exc:
                failures.append((url, str(exc)))
                continue
            if record:
                records.append(record)

    records.sort(key=lambda item: str(item["url"]))
    records = unique_ids(records)
    if args.limit is not None:
        records = records[: args.limit]

    write_jsonl(records, args.output_dir / "offers.jsonl")
    build_yml(records, args.output_dir / "feed.xml")
    with_picture = sum(bool(record["pictures"]) for record in records)
    print(
        f"offers={len(records)} with_picture={with_picture} no_picture={len(records) - with_picture} "
        f"skipped={len(url_categories) - len(records)} fetch_failures={len(failures)}",
        file=sys.stderr,
    )
    if failures:
        for url, reason in failures:
            print(f"FAILED {url}: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
