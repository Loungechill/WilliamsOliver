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
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_SOURCE = "https://williams-oliver.ru/api/feed/diginetica"

UNSUPPORTED_YANDEX_TAGS = {
    "rating",
    "badge",
    "reviews_count",
    "purchasable",
    "prices",
    "popularity",
}

MARKETING_CATEGORY_ROOT_NAMES = {"Подарки", "Лучшая цена"}


def normalize_vendor(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value.casefold()


def normalize_text(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", " ", value).strip()


def load_blacklist(path: Path) -> tuple[list[str], set[str]]:
    raw = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    normalized = {normalize_vendor(v) for v in raw}

    if len(raw) != len(normalized):
        raise RuntimeError(
            "Blacklist contains duplicates after normalization"
        )

    return raw, normalized


def download_source(source: str, destination: Path) -> None:
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(
            source,
            headers={
                "User-Agent": "WO-Feed-Filter/2.0 (+GitHub Actions)",
                "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8",
            },
        )

        with urllib.request.urlopen(
            request,
            timeout=180,
        ) as response, destination.open("wb") as out:

            if response.status != 200:
                raise RuntimeError(
                    f"Source returned HTTP {response.status}"
                )

            shutil.copyfileobj(response, out)

    else:
        shutil.copyfile(source, destination)

    size = destination.stat().st_size

    if size < 1_000_000:
        raise RuntimeError(
            f"Downloaded source is unexpectedly small: {size} bytes"
        )


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_child_by_local_name(
    parent: ET.Element,
    name: str,
) -> ET.Element | None:

    for child in parent:
        if local_name(child.tag) == name:
            return child

    return None


def build_category_map(
    shop: ET.Element,
) -> tuple[dict[str, dict[str, str | None]], set[str]]:

    categories = find_child_by_local_name(
        shop,
        "categories",
    )

    if categories is None:
        raise RuntimeError("<categories> not found")

    category_map: dict[
        str,
        dict[str, str | None],
    ] = {}

    for category in categories:
        if local_name(category.tag) != "category":
            continue

        category_id = normalize_text(
            category.attrib.get("id")
        )

        if not category_id:
            raise RuntimeError(
                "Category without id found"
            )

        if category_id in category_map:
            raise RuntimeError(
                f"Duplicate category id in <categories>: {category_id}"
            )

        category_map[category_id] = {
            "name": normalize_text(category.text),
            "parent": normalize_text(
                category.attrib.get("parentId")
            )
            or None,
        }

    if not category_map:
        raise RuntimeError("No categories found")

    root_ids_by_name: dict[
        str,
        list[str],
    ] = {
        name: []
        for name in MARKETING_CATEGORY_ROOT_NAMES
    }

    for category_id, data in category_map.items():

        if (
            data["name"] in root_ids_by_name
            and data["parent"] is None
        ):
            root_ids_by_name[
                data["name"]
            ].append(category_id)

    for name, ids in root_ids_by_name.items():

        if len(ids) != 1:
            raise RuntimeError(
                f"Expected exactly one root category named "
                f"{name!r}, found {len(ids)}: {ids}"
            )

    marketing_ids: set[str] = set()

    for ids in root_ids_by_name.values():

        root_id = ids[0]
        branch = {root_id}

        changed = True

        while changed:
            changed = False

            for category_id, data in category_map.items():

                if (
                    data["parent"] in branch
                    and category_id not in branch
                ):
                    branch.add(category_id)
                    changed = True

        marketing_ids.update(branch)

    return category_map, marketing_ids


def remove_unsupported_yandex_fields(
    root: ET.Element,
) -> tuple[Counter, int]:

    removed_tags: Counter = Counter()
    nofilter_removed = 0

    for parent in root.iter():

        for child in list(parent):

            tag_name = local_name(child.tag)

            if tag_name in UNSUPPORTED_YANDEX_TAGS:

                parent.remove(child)
                removed_tags[tag_name] += 1

    for element in root.iter():

        if (
            local_name(element.tag) == "param"
            and "noFilter" in element.attrib
        ):
            del element.attrib["noFilter"]
            nofilter_removed += 1

    return removed_tags, nofilter_removed


def normalize_offer_categories(
    offers: ET.Element,
    category_map: dict[
        str,
        dict[str, str | None],
    ],
    marketing_ids: set[str],
) -> dict[str, int]:

    duplicate_offers = 0
    duplicate_tags_removed = 0
    duplicate_offers_with_nonmarketing = 0
    duplicate_offers_only_marketing = 0
    kept_nonmarketing = 0
    kept_first_fallback = 0

    for offer in offers:

        if local_name(offer.tag) != "offer":
            continue

        category_elements = [
            child
            for child in offer
            if local_name(child.tag) == "categoryId"
        ]

        if not category_elements:
            raise RuntimeError(
                f"Offer {offer.attrib.get('id', '')} "
                f"has no <categoryId>"
            )

        category_ids = [
            normalize_text(element.text)
            for element in category_elements
        ]

        for category_id in category_ids:

            if category_id not in category_map:
                raise RuntimeError(
                    f"Offer "
                    f"{offer.attrib.get('id', '')} "
                    f"references unknown categoryId "
                    f"{category_id!r}"
                )

        if len(category_elements) == 1:
            continue

        duplicate_offers += 1
        duplicate_tags_removed += (
            len(category_elements) - 1
        )

        nonmarketing_elements = [
            element
            for element, category_id in zip(
                category_elements,
                category_ids,
            )
            if category_id not in marketing_ids
        ]

        if nonmarketing_elements:

            keep = nonmarketing_elements[0]

            duplicate_offers_with_nonmarketing += 1
            kept_nonmarketing += 1

        else:

            keep = category_elements[0]

            duplicate_offers_only_marketing += 1
            kept_first_fallback += 1

        for element in category_elements:

            if element is not keep:
                offer.remove(element)

    return {
        "category_duplicate_offers_fixed":
            duplicate_offers,

        "category_duplicate_tags_removed":
            duplicate_tags_removed,

        "category_duplicate_offers_with_nonmarketing_kept":
            duplicate_offers_with_nonmarketing,

        "category_duplicate_offers_only_marketing_fallback":
            duplicate_offers_only_marketing,

        "category_kept_nonmarketing":
            kept_nonmarketing,

        "category_kept_first_fallback":
            kept_first_fallback,
    }


def validate_yandex_cleanup(
    root: ET.Element,
    offers: ET.Element,
    category_map: dict[
        str,
        dict[str, str | None],
    ],
) -> None:

    bad_tags = Counter()
    nofilter_count = 0

    for element in root.iter():

        tag_name = local_name(element.tag)

        if tag_name in UNSUPPORTED_YANDEX_TAGS:
            bad_tags[tag_name] += 1

        if (
            tag_name == "param"
            and "noFilter" in element.attrib
        ):
            nofilter_count += 1

    if bad_tags:
        raise RuntimeError(
            f"Unsupported Yandex tags remain "
            f"after cleanup: {dict(bad_tags)}"
        )

    if nofilter_count:
        raise RuntimeError(
            f"noFilter attributes remain "
            f"after cleanup: {nofilter_count}"
        )

    for offer in offers:

        if local_name(offer.tag) != "offer":
            continue

        category_ids = [
            normalize_text(child.text)
            for child in offer
            if local_name(child.tag) == "categoryId"
        ]

        if len(category_ids) != 1:
            raise RuntimeError(
                f"Offer "
                f"{offer.attrib.get('id', '')} "
                f"must have exactly one categoryId "
                f"after cleanup; found "
                f"{len(category_ids)}"
            )

        if category_ids[0] not in category_map:
            raise RuntimeError(
                f"Offer "
                f"{offer.attrib.get('id', '')} "
                f"references unknown final categoryId "
                f"{category_ids[0]!r}"
            )


def filter_feed(
    source_file: Path,
    blacklist_file: Path,
    output_file: Path,
) -> dict[str, int]:

    raw_blacklist, blocked = load_blacklist(
        blacklist_file
    )

    try:
        tree = ET.parse(source_file)

    except ET.ParseError as exc:
        raise RuntimeError(
            f"Source XML is invalid: {exc}"
        ) from exc

    root = tree.getroot()

    if local_name(root.tag) != "yml_catalog":
        raise RuntimeError(
            f"Unexpected root tag: {root.tag}"
        )

    shop = find_child_by_local_name(
        root,
        "shop",
    )

    if shop is None:
        raise RuntimeError("<shop> not found")

    offers = find_child_by_local_name(
        shop,
        "offers",
    )

    if offers is None:
        raise RuntimeError("<offers> not found")

    category_map, marketing_ids = (
        build_category_map(shop)
    )

    original_count = len(offers)

    if original_count == 0:
        raise RuntimeError(
            "Source feed contains zero offers"
        )

    removed = 0
    matched_blocked: set[str] = set()

    for offer in list(offers):

        if local_name(offer.tag) != "offer":
            continue

        vendor_element = (
            find_child_by_local_name(
                offer,
                "vendor",
            )
        )

        vendor = (
            vendor_element.text
            if vendor_element is not None
            else None
        )

        normalized = normalize_vendor(vendor)

        if normalized in blocked:

            offers.remove(offer)

            removed += 1
            matched_blocked.add(normalized)

    remaining_count = len(offers)

    if remaining_count <= 0:
        raise RuntimeError(
            "Filtering removed every offer; "
            "refusing to publish"
        )

    category_stats = normalize_offer_categories(
        offers,
        category_map,
        marketing_ids,
    )

    removed_tags, nofilter_removed = (
        remove_unsupported_yandex_fields(root)
    )

    validate_yandex_cleanup(
        root,
        offers,
        category_map,
    )

    root.set(
        "date",
        datetime.now(
            ZoneInfo("Europe/Moscow")
        ).strftime("%Y-%m-%d %H:%M"),
    )

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with tempfile.NamedTemporaryFile(
        mode="wb",
        suffix=".xml",
        prefix="feed-",
        dir=output_file.parent,
        delete=False,
    ) as temp:

        temp_path = Path(temp.name)

    try:

        ET.indent(tree, space="  ")

        tree.write(
            temp_path,
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=True,
        )

        try:
            check_tree = ET.parse(temp_path)

        except ET.ParseError as exc:
            raise RuntimeError(
                f"Generated XML is invalid: {exc}"
            ) from exc

        check_root = check_tree.getroot()

        check_shop = find_child_by_local_name(
            check_root,
            "shop",
        )

        check_offers = (
            find_child_by_local_name(
                check_shop,
                "offers",
            )
            if check_shop is not None
            else None
        )

        if check_offers is None:
            raise RuntimeError(
                "Generated XML lost <offers>"
            )

        remaining_blocked = []

        for offer in check_offers:

            if local_name(offer.tag) != "offer":
                continue

            vendor_element = (
                find_child_by_local_name(
                    offer,
                    "vendor",
                )
            )

            vendor = (
                vendor_element.text
                if vendor_element is not None
                else None
            )

            if normalize_vendor(vendor) in blocked:
                remaining_blocked.append(
                    vendor or ""
                )

        if remaining_blocked:
            raise RuntimeError(
                f"Validation failed: "
                f"{len(remaining_blocked)} "
                f"blocked offers remain; examples: "
                f"{remaining_blocked[:5]}"
            )

        if len(check_offers) != remaining_count:
            raise RuntimeError(
                f"Offer count changed after write: "
                f"expected {remaining_count}, "
                f"got {len(check_offers)}"
            )

        check_category_map, _ = (
            build_category_map(check_shop)
        )

        validate_yandex_cleanup(
            check_root,
            check_offers,
            check_category_map,
        )

        if temp_path.stat().st_size < 1_000_000:
            raise RuntimeError(
                "Generated XML is unexpectedly small"
            )

        os.replace(
            temp_path,
            output_file,
        )

    finally:

        if temp_path.exists():
            temp_path.unlink()

    stats: dict[str, int] = {
        "blacklist":
            len(raw_blacklist),

        "matched_blacklist":
            len(matched_blocked),

        "unmatched_blacklist":
            len(blocked - matched_blocked),

        "offers_before":
            original_count,

        "offers_removed":
            removed,

        "offers_after":
            remaining_count,

        **category_stats,

        "noFilter_attributes_removed":
            nofilter_removed,

        "output_bytes":
            output_file.stat().st_size,
    }

    for tag_name in sorted(
        UNSUPPORTED_YANDEX_TAGS
    ):
        stats[
            f"removed_tag_{tag_name}"
        ] = removed_tags[tag_name]

    return stats


def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Filter Williams Oliver YML/XML feed "
            "by vendor blacklist and clean "
            "Yandex Direct warnings"
        )
    )

    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help="Source feed URL or local XML path",
    )

    parser.add_argument(
        "--blacklist",
        default="blocked_vendors.txt",
        help="Blacklist text file",
    )

    parser.add_argument(
        "--output",
        default="feed.xml",
        help="Output XML path",
    )

    args = parser.parse_args()

    blacklist_file = Path(
        args.blacklist
    )

    output_file = Path(
        args.output
    )

    if not blacklist_file.exists():

        print(
            f"ERROR: blacklist file not found: "
            f"{blacklist_file}",
            file=sys.stderr,
        )

        return 2

    with tempfile.TemporaryDirectory(
        prefix="wo-feed-"
    ) as tmpdir:

        source_file = (
            Path(tmpdir) / "source.xml"
        )

        try:

            download_source(
                args.source,
                source_file,
            )

            stats = filter_feed(
                source_file,
                blacklist_file,
                output_file,
            )

        except Exception as exc:

            print(
                f"ERROR: {exc}",
                file=sys.stderr,
            )

            return 1

    print("SUCCESS")

    for key, value in stats.items():
        print(f"{key}={value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
