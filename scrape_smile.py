"""Publish Smile Student Living's public availability feed for the map.

Run: python3 outputs/scrape_smile.py

The official availability page uses Duda's public ``appfolio-listings``
collection. This collector reads its paginated JSON, selects only the
property group configured on Smile's availability widget, groups advertised
unit/listable records by AppFolio property ID, and publishes atomically.
Hosting must schedule this command; merely serving index.html does not run it.
"""

import argparse
import datetime as dt
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import re
import tempfile
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "smile-listings.json"
BASE = "https://www.smilestudentliving.com"
DIRECTORY = BASE + "/availability"
GROUP = "smile uiuc scattered site portfolio"
ENDPOINT = BASE + "/rts/collections/public/0af4bf1b/runtime/collection/appfolio-listings/query-data"
USER_AGENT = "CU-Apartment-Map/1.0 (public Smile availability collection; daily snapshot)"
UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z", re.I)


def fetch_page(number):
    query = urllib.parse.urlencode({"pageSize": 100, "pageNumber": number, "query": "()"})
    request = urllib.request.Request(f"{ENDPOINT}?{query}", headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        if urllib.parse.urlsplit(response.geturl()).hostname != "www.smilestudentliving.com":
            raise ValueError("Unexpected Smile feed redirect")
        return json.load(response)


def read_pages(saved_pages=None):
    entries = []
    seen = set()
    expected_count = None
    expected_pages = None
    reported_pages = None
    for number in range(30):  # Safety cap if the API changes its pagination.
        page = (json.loads(saved_pages[number].read_text(encoding="utf-8"))
                if saved_pages is not None and number < len(saved_pages) else fetch_page(number))
        values = page.get("values")
        if page.get("name") != "appfolio-listings" or not isinstance(values, list) or len(values) > 100:
            raise ValueError(f"Invalid Smile collection page {number}")
        meta = page.get("page") or {}
        if (meta.get("pageNumber") != number or meta.get("pageSize") != 100 or
                not isinstance(meta.get("totalItems"), int) or
                not isinstance(meta.get("totalPages"), int)):
            raise ValueError(f"Invalid Smile pagination metadata on page {number}")
        if number == 0:
            expected_count = meta["totalItems"]
            reported_pages = meta["totalPages"]
            expected_pages = max(1, reported_pages)
            if expected_count < 0 or reported_pages < 0 or expected_pages > 30 or \
                    expected_pages != max(1, math.ceil(expected_count / 100)):
                raise ValueError("Unexpected Smile feed pagination totals")
        if meta["totalItems"] != expected_count or meta["totalPages"] != reported_pages:
            raise ValueError("Smile feed changed during pagination; retaining old snapshot")
        for wrapper in values:
            data = wrapper.get("data") if isinstance(wrapper, dict) else None
            uid = data.get("listable_uid") if isinstance(data, dict) else None
            if not isinstance(uid, str) or not UUID.fullmatch(uid) or uid in seen:
                raise ValueError(f"Missing or duplicate Smile listing ID on page {number}")
            seen.add(uid)
            entries.append(data)
        if number + 1 == expected_pages:
            if len(entries) != expected_count:
                raise ValueError("Smile availability feed is incomplete")
            return entries
        if len(values) != 100:
            raise ValueError("Smile feed ended before the reported final page")
        if saved_pages is not None and number + 1 >= len(saved_pages):
            raise ValueError("Saved Smile pages stop before pagination ends")
    raise ValueError("Smile feed exceeded pagination safety limit")


def group_member(row):
    groups = row.get("property_lists") or []
    return any(isinstance(group, dict) and group.get("name", "").lower() == GROUP
               for group in groups)


def amount(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(number) or not 0 < number < 100000:
        return None
    return int(number) if number.is_integer() else number


def money(value):
    return {"min": value, "max": value, "display": f"${value:,.2f}".rstrip("0").rstrip(".")}


def photos(row):
    return [item["url"] for item in row.get("photos") or []
            if isinstance(item, dict) and isinstance(item.get("url"), str)
            and item["url"].startswith("https://images.cdn.appfolio.com/")]


def text_list(value):
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def availability(row, today):
    try:
        day = dt.date.fromisoformat(row["available_date"])
    except (KeyError, TypeError, ValueError):
        return None
    return "Available now" if day <= today else f"Available {day.strftime('%b')} {day.day}, {day.year}"


def property_record(rows, today):
    first = rows[0]
    identifier = str(first.get("property_uid") or "")
    street = str(first.get("address_address1") or "").strip()
    city = str(first.get("address_city") or "").strip()
    state = str(first.get("address_state") or "").strip()
    postal = str(first.get("address_postal_code") or "").strip()
    lat, lon = float(first["address_latitude"]), float(first["address_longitude"])
    if not identifier.isdigit() or not street or city not in ("Champaign", "Urbana") or state != "IL":
        raise ValueError(f"Unexpected Smile property identity/address: {identifier}")
    if not (39.9 < lat < 40.25 and -88.45 < lon < -88.0):
        raise ValueError(f"Smile property {identifier} has an out-of-region map pin")
    if any(row.get("address_address1") != street or row.get("address_city") != city or
           abs(float(row["address_latitude"]) - lat) > .00001 or
           abs(float(row["address_longitude"]) - lon) > .00001 for row in rows):
        raise ValueError(f"Smile property {identifier} has conflicting addresses or coordinates")

    offers = []
    for row in rows:
        price = amount(row.get("market_rent"))
        term = availability(row, today)
        # The feed also contains a $0 generic sublease *application*, not a
        # priced rental. Only a currently posted, priced, available listable
        # with a valid date is advertised as an offer.
        if row.get("available") is not True or not price or not term or not row.get("posted_to_website"):
            continue
        title = str(row.get("marketing_title") or "").strip()
        description = str(row.get("marketing_description") or "").lower()
        per_person = "priced per person" in title.lower() or "prices displayed are for individual leases" in description
        basis = "perPersonUsd" if per_person else "perMonthUsd"
        offers.append({
            "unitTypeNodeId": f"smile:{row['listable_uid']}",
            "title": str(row.get("unit_template_name") or "").strip() or title,
            "bedrooms": row.get("bedrooms"), "bathrooms": row.get("bathrooms"),
            "rent": {basis: money(price)}, "availableTerms": [term.removeprefix("Available ")],
            "statusText": title[:180]
        })
    offers.sort(key=lambda row: (row["bedrooms"] if row["bedrooms"] is not None else 99,
                                 row["rent"].get("perPersonUsd", row["rent"].get("perMonthUsd"))["min"]))
    if not offers:
        return None  # Smile's public directory contains currently available listables only.

    representative = next(row for row in rows if f"smile:{row['listable_uid']}" == offers[0]["unitTypeNodeId"])
    photo_urls = list(dict.fromkeys(
        url for row in (representative, *rows) for url in photos(row)[:3]))[:8]
    amenities = text_list(representative.get("amenities"))[:24]
    utilities = text_list(representative.get("utilities"))[:12]
    return {
        "name": street, "propertyNodeId": f"smile:{identifier}",
        "propertyUrl": f"{BASE}/listings/detail/{representative['listable_uid']}",
        "sourceName": "Smile Student Living", "sourceUrl": BASE + "/",
        "sourceLogoUrl": str(representative.get("logo_url") or ""),
        "source": {"assetHosts": ["cdn.appfolio.com"]},
        "address": {"street": street, "locality": city, "region": state, "postalCode": postal,
                    "source": "Smile public AppFolio availability collection"},
        "mapPoint": {"latitude": lat, "longitude": lon,
                     "source": "Smile published property latitude/longitude"},
        "availabilityStatus": "advertised", "offers": offers,
        "representativeFloorplan": {
            "label": str(representative.get("unit_template_name") or "").strip() or None,
            "bedrooms": representative.get("bedrooms"), "bathrooms": representative.get("bathrooms"),
            "availabilityStatus": availability(representative, today), "availabilityRows": [],
            "utilitiesIncludedInFee": utilities, "amenities": amenities,
            "floorplanImage": None, "photoSamples": photo_urls
        }
    }


def collect(entries, today):
    grouped = defaultdict(list)
    # Mirror the official widget: an exact portfolio-name match takes
    # precedence over the property-list fallback (rather than unioning both).
    direct = [row for row in entries if row.get("portfolio_name") == "Smile UIUC Scattered Site Portfolio"]
    selected = direct if direct else [row for row in entries if group_member(row)]
    if entries and not selected:
        raise ValueError("Smile availability widget's property group is missing from the feed")
    for row in selected:
        grouped[str(row.get("property_uid") or "")].append(row)
    properties = [item for rows in grouped.values() if (item := property_record(rows, today))]
    properties.sort(key=lambda item: item["name"])
    if selected and not properties and any(row.get("available") is True and
                                           amount(row.get("market_rent")) and
                                           row.get("posted_to_website") and
                                           availability(row, today) is None for row in selected):
        raise ValueError("Smile has priced available listings with invalid dates")
    if len({p["propertyNodeId"] for p in properties}) != len(properties):
        raise ValueError("Smile grouped property catalog is duplicated")
    return properties, len(selected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-json", type=Path, nargs="+", help="Use saved public API pages for testing")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    today = dt.datetime.now(dt.timezone.utc).date()
    properties, listable_count = collect(read_pages(args.page_json), today)
    result = {"schemaVersion": 1, "collection": {
        "source": "Smile Student Living official public availability page",
        "directoryUrl": DIRECTORY, "capturedOn": today.isoformat(),
        "method": "Paginated public Duda appfolio-listings collection, filtered by the availability widget's Smile UIUC property group; unit listables grouped by published property ID.",
        "buildingCount": len(properties), "advertisedBuildingCount": len(properties),
        "advertisedLayoutCount": sum(len(item["offers"]) for item in properties),
        "sourceListableCount": listable_count,
        "limitations": "The public collection is an availability-only snapshot, not a full inventory of Smile's unlisted buildings. A zero-rent sublease application is not a rental offer. Published prices may be per person when explicitly stated; otherwise they are labeled monthly without inferring a per-unit basis. Photos are sampled from official URLs. Pins are Smile-provided, not verified footprint centroids.",
        "futureRefreshRequirement": {"cadenceHours": 24, "implemented": False}
    }, "properties": properties}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".smile-listings-", suffix=".json", dir=args.output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Published {len(properties)} Smile properties with {result['collection']['advertisedLayoutCount']} "
          f"advertised listables to {args.output}")


if __name__ == "__main__":
    main()
