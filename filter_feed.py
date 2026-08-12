#!/usr/bin/env python3
import argparse
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_SOURCE = "https://williams-oliver.ru/api/feed/diginetica"


def normalize_vendor(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value.casefold()


def load_blacklist(path: Path) -> tuple[list[str], set[str]]:
    raw = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    normalized = {normalize_vendor(v) for v in raw}
    if len(raw) != len(normalized):
        raise RuntimeError("Blacklist contains duplicates after normalization")
    return raw, normalized


def download_source(source: str, destination: Path) -> None:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(
            source,
            headers={
                "User-Agent": "WO-Feed-Filter/1.0 (+GitHub Actions)",
                "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as out:
            if response.status != 200:
                raise RuntimeError(f"Source returned HTTP {response.status}")
            shutil.copyfileobj(response, out)
    else:
        shutil.copyfile(source, destination)

    size = destination.stat().st_size
    if size < 1_000_000:
        raise RuntimeError(f"Downloaded source is unexpectedly small: {size} bytes")


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_child_by_local_name(parent: ET.Element, name: str) -> ET.Element | None:
    for child in parent:
        if local_name(child.tag) == name:
            return child
    return None


def filter_feed(source_file: Path, blacklist_file: Path, output_file: Path) -> dict[str, int]:
    raw_blacklist, blocked = load_blacklist(blacklist_file)

    try:
        tree = ET.parse(source_file)
    except ET.ParseError as exc:
        raise RuntimeError(f"Source XML is invalid: {exc}") from exc

    root = tree.getroot()
    if local_name(root.tag) != "yml_catalog":
        raise RuntimeError(f"Unexpected root tag: {root.tag}")

    shop = find_child_by_local_name(root, "shop")
    if shop is None:
        raise RuntimeError("<shop> not found")

    offers = find_child_by_local_name(shop, "offers")
    if offers is None:
        raise RuntimeError("<offers> not found")

    original_count = len(offers)
    if original_count == 0:
        raise RuntimeError("Source feed contains zero offers")

    removed = 0
    matched_blocked: set[str] = set()

    for offer in list(offers):
        if local_name(offer.tag) != "offer":
            continue

        vendor_element = find_child_by_local_name(offer, "vendor")
        vendor = vendor_element.text if vendor_element is not None else None
        normalized = normalize_vendor(vendor)

        if normalized in blocked:
            offers.remove(offer)
            removed += 1
            matched_blocked.add(normalized)

    remaining_count = len(offers)
    if remaining_count <= 0:
        raise RuntimeError("Filtering removed every offer; refusing to publish")

    # Yandex requires this to represent generation time of the resulting YML.
    root.set("date", datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M"))

    output_file.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".xml", prefix="feed-", dir=output_file.parent, delete=False
    ) as temp:
        temp_path = Path(temp.name)

    try:
        ET.indent(tree, space="  ")
        tree.write(temp_path, encoding="utf-8", xml_declaration=True, short_empty_elements=True)

        # Re-parse the generated file so a truncated/malformed output is never published.
        try:
            check_tree = ET.parse(temp_path)
        except ET.ParseError as exc:
            raise RuntimeError(f"Generated XML is invalid: {exc}") from exc

        check_root = check_tree.getroot()
        check_shop = find_child_by_local_name(check_root, "shop")
        check_offers = find_child_by_local_name(check_shop, "offers") if check_shop is not None else None
        if check_offers is None:
            raise RuntimeError("Generated XML lost <offers>")

        remaining_blocked = []
        for offer in check_offers:
            if local_name(offer.tag) != "offer":
                continue
            vendor_element = find_child_by_local_name(offer, "vendor")
            vendor = vendor_element.text if vendor_element is not None else None
            if normalize_vendor(vendor) in blocked:
                remaining_blocked.append(vendor or "")

        if remaining_blocked:
            raise RuntimeError(
                f"Validation failed: {len(remaining_blocked)} blocked offers remain; "
                f"examples: {remaining_blocked[:5]}"
            )

        if len(check_offers) != remaining_count:
            raise RuntimeError(
                f"Offer count changed after write: expected {remaining_count}, got {len(check_offers)}"
            )

        if temp_path.stat().st_size < 1_000_000:
            raise RuntimeError("Generated XML is unexpectedly small")

        os.replace(temp_path, output_file)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return {
        "blacklist": len(raw_blacklist),
        "matched_blacklist": len(matched_blocked),
        "unmatched_blacklist": len(blocked - matched_blocked),
        "offers_before": original_count,
        "offers_removed": removed,
        "offers_after": remaining_count,
        "output_bytes": output_file.stat().st_size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter Williams Oliver YML/XML feed by <vendor> blacklist")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="Source feed URL or local XML path")
    parser.add_argument("--blacklist", default="blocked_vendors.txt", help="Blacklist text file")
    parser.add_argument("--output", default="feed.xml", help="Output XML path")
    args = parser.parse_args()

    blacklist_file = Path(args.blacklist)
    output_file = Path(args.output)

    if not blacklist_file.exists():
        print(f"ERROR: blacklist file not found: {blacklist_file}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="wo-feed-") as tmpdir:
        source_file = Path(tmpdir) / "source.xml"
        try:
            download_source(args.source, source_file)
            stats = filter_feed(source_file, blacklist_file, output_file)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    print("SUCCESS")
    for key, value in stats.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
