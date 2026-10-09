#!/usr/bin/env python3
"""Build an additional feed without changing the existing brand-filter pipeline."""
import argparse
import csv
import io
import json
import os
import re
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# Reuse only the existing vendor normalization/blacklist reader, not its XML edits.
from filter_feed import load_blacklist, normalize_vendor

SOURCE = "https://williamsoliverfeed.rta.workers.dev/feed.xml"
SPREADSHEET_ID = "1Lz9YwI-yClqqwvqtGFWTy0W8FCWuuhJGvwgznnPE1NA"


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "WO-Segmented-Feed/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=180) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}: {url}")
        return response.read()


def exclusions(csv_text: str, threshold: int) -> tuple[set[str], int, int]:
    if threshold < 0:
        raise ValueError("Impressions threshold must be nonnegative")
    rows = csv.DictReader(io.StringIO(csv_text.lstrip('\ufeff')), strict=True)
    if not rows.fieldnames or not {"ID товара", "Показы"} <= set(rows.fieldnames):
        raise ValueError("CSV must contain «ID товара» and «Показы»; check tab/public access")
    excluded = set()
    product_rows = ignored_rows = 0
    for row in rows:
        if None in row:
            raise ValueError("Malformed CSV row: extra columns")
        if all(not (value or '').strip() for value in row.values()):
            continue
        offer_id = (row.get("ID товара") or "").strip()
        # Catalog/brand rows in search have non-product IDs; don't match these.
        if not re.fullmatch(r"[0-9]{11}", offer_id):
            ignored_rows += 1
            continue
        raw = re.sub(r"\s", "", row.get("Показы") or "")
        if not re.fullmatch(r"[0-9]+", raw):
            raise ValueError(f"Invalid impressions for ID {offer_id!r}")
        product_rows += 1
        # Strict >: a row with exactly 1000 stays. A later duplicate cannot undo an exclusion.
        if int(raw) > threshold:
            excluded.add(offer_id)
    if not product_rows:
        raise ValueError("No valid product rows in statistics; refusing to overwrite the previous feed")
    return excluded, product_rows, ignored_rows


def build_feed(source: bytes, csv_text: str, blacklist: Path, output: Path, threshold: int) -> dict:
    excluded, product_rows, ignored_rows = exclusions(csv_text, threshold)
    _, blocked = load_blacklist(blacklist)
    if not blocked:
        raise ValueError("Vendor blacklist is empty")
    root = ET.fromstring(source)
    if root.tag != "yml_catalog":
        raise ValueError("Source is not a yml_catalog")
    shop = root.find("shop")
    offers = shop.find("offers") if shop is not None else None
    categories = shop.find("categories") if shop is not None else None
    if offers is None or categories is None:
        raise ValueError("Source must contain shop/offers and shop/categories")
    original = list(offers)
    if not original or any(offer.tag != "offer" for offer in original):
        raise ValueError("Source offers are missing or invalid")
    category_bytes = ET.tostring(categories)
    seen = set()
    removed_brands = removed_impressions = 0
    expected = []
    for offer in original:
        offer_id = offer.get("id")
        if not offer_id or offer_id in seen:
            raise ValueError(f"Missing or duplicate offer id: {offer_id!r}")
        seen.add(offer_id)
        if normalize_vendor(offer.findtext("vendor")) in blocked:
            offers.remove(offer)
            removed_brands += 1
        elif offer_id in excluded:
            offers.remove(offer)
            removed_impressions += 1
        else:
            # Includes offers absent from the statistics. Preserve all fields/categoryId.
            expected.append(ET.tostring(offer))
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".xml", delete=False) as temp:
            temp_path = Path(temp.name)
            ET.ElementTree(root).write(temp, encoding="utf-8", xml_declaration=True)
        check = ET.parse(temp_path)
        if [ET.tostring(o) for o in check.findall("./shop/offers/offer")] != expected:
            raise ValueError("Generated feed changed retained product data")
        if ET.tostring(check.find("./shop/categories")) != category_bytes:
            raise ValueError("Generated feed changed categories")
        os.replace(temp_path, output)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return {
        "source_offers": len(original), "removed_brands": removed_brands,
        "removed_impressions": removed_impressions, "remaining_offers": len(expected),
        "threshold": threshold, "excluded_ids_in_sheet": len(excluded),
        "statistics_product_rows": product_rows, "ignored_nonproduct_rows": ignored_rows,
    }


def run(channel: str, gid: str, threshold: int = 1000) -> int:
    parser = argparse.ArgumentParser(description=f"WO {channel}: exclude brands and offers with impressions strictly above the threshold")
    parser.add_argument("--source", default=SOURCE, help="Already filtered feed URL or local XML")
    parser.add_argument("--csv", help="Optional local statistics CSV for offline verification")
    parser.add_argument("--spreadsheet-id", default=SPREADSHEET_ID)
    parser.add_argument("--gid", default=gid)
    parser.add_argument("--threshold", type=int, default=threshold)
    parser.add_argument("--blacklist", type=Path, default=Path(__file__).with_name("blocked_vendors.txt"))
    parser.add_argument("--output", type=Path, default=Path(f"feed_{channel}.xml"))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source = download(args.source) if args.source.startswith(("https://", "http://")) else Path(args.source).read_bytes()
    csv_url = f"https://docs.google.com/spreadsheets/d/{args.spreadsheet_id}/export?format=csv&gid={args.gid}"
    csv_text = args.csv and Path(args.csv).read_text(encoding="utf-8-sig")
    if csv_text is None:
        csv_text = download(csv_url).decode("utf-8-sig")
    stats = build_feed(source, csv_text, args.blacklist, args.output, args.threshold)
    if args.report:
        args.report.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"channel": channel, **stats}, ensure_ascii=False, indent=2))
    return 0
