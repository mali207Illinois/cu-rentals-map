"""Collect Green Street Realty's public property directory in one request.

Run: python3 outputs/scrape_greenstreet.py

The directory embeds one structured JSON record per property, including
published coordinates, floor plans, amenities, and image IDs. A failed or
incomplete run leaves the previous JSON untouched. Respect the site's
10-second robots.txt crawl delay if adding more requests in the future.
"""

import argparse
import datetime as dt
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.request


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "greenstreet-listings.json"
DIRECTORY = "https://www.greenstrealty.com/properties"
BASE = "https://www.greenstrealty.com"
USER_AGENT = "CU-Apartment-Map/1.0 (public property directory; one request per run)"
MONTHS = {month: index for index, month in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}


class PropertyScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.records = []
        self.record_parts = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and "property-info-json" in attributes.get("class", "").split():
            if attributes.get("type") != "application/json" or self.record_parts is not None:
                raise ValueError("Unexpected property data markup")
            self.record_parts = []

    def handle_data(self, data):
        if self.record_parts is not None:
            self.record_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.record_parts is not None:
            self.records.append(json.loads("".join(self.record_parts)))
            self.record_parts = None


def fetch_directory():
    request = urllib.request.Request(DIRECTORY, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        if response.geturl().split("/")[2] not in ("www.greenstrealty.com", "greenstrealty.com"):
            raise ValueError("Unexpected directory redirect")
        return response.read().decode("utf-8")


def is_current(term, today):
    term = str(term or "").strip()
    if term.lower() == "available now":
        return True
    match = re.fullmatch(r"(?:Available\s+)?([A-Za-z]+)\s+(20\d\d)", term, re.I)
    if not match:
        return False  # Unknown, leased, or waitlisted is never advertised as available.
    month = MONTHS.get(match.group(1).capitalize())
    return month is not None and (int(match.group(2)), month) >= (today.year, today.month)


def price(value):
    raw = str(value or "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:-\d+(?:\.\d+)?)?", raw):
        return None
    numbers = [float(part) for part in raw.split("-")]
    if not all(0 < number < 100000 for number in numbers) or numbers[-1] < numbers[0]:
        return None
    def compact(number):
        return int(number) if number.is_integer() else number
    def money(number):
        return f"${number:,.2f}".rstrip("0").rstrip(".")
    return {"min": compact(numbers[0]), "max": compact(numbers[-1]),
            "display": "–".join(money(number) for number in numbers)}


def room_count(value):
    raw = str(value or "").strip().lower()
    if raw == "studio":
        return 0
    if raw.endswith("+"):
        return raw
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        number = float(raw)
        return int(number) if number.is_integer() else number
    return None


def image_url(identifier):
    return f"{BASE}/image/{identifier}/900" if str(identifier or "").isdigit() else None


def as_items(value):
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else []


def normalize(record, today):
    node = str(record.get("id") or "")
    slug = str(record.get("slug") or "")
    name = str(record.get("address_1") or "").strip()
    city = str(record.get("city") or "").strip()
    if not node.isdigit() or not re.fullmatch(r"[a-z0-9-]+", slug) or not name or not city:
        raise ValueError(f"Incomplete Green Street property: {node or slug}")
    lat, lon = float(record["latitude"]), float(record["longitude"])
    if not (39.9 < lat < 40.25 and -88.45 < lon < -88.0):
        raise ValueError(f"Property {node} has coordinates outside Champaign-Urbana")
    url = f"{BASE}/properties/profile/{slug}"
    if record.get("url") != url:
        raise ValueError(f"Property {node} has an unexpected official URL")

    plans = record.get("fplans")
    if not isinstance(plans, list):
        raise ValueError(f"Property {node} has no floor-plan list")
    offers = []
    for index, plan in enumerate(plans, 1):
        term = str(plan.get("availability") or "").strip()
        if not is_current(term, today):
            continue
        rent = {}
        for source, target in (("total_price", "perUnitUsd"), ("price_per_bed", "perPersonUsd")):
            amount = price(plan.get(source))
            if amount:
                rent[target] = amount
        offers.append({"unitTypeNodeId": f"gsr:{node}:{index}",
                       "title": str(plan.get("title") or "").strip(),
                       "bedrooms": room_count(plan.get("beds")),
                       "bathrooms": room_count(plan.get("baths")),
                       "rent": rent, "availableTerms": [term], "statusText": ""})

    photos = [image_url(item.get("img")) for item in as_items(record.get("photos"))]
    photos = list(dict.fromkeys(url for url in photos if url))
    if not photos:
        hero = image_url(record.get("img"))
        if hero:
            photos = [hero]
    representative = next((plan for plan in plans if is_current(plan.get("availability"), today)),
                          plans[0] if plans else {})
    album = as_items(representative.get("album"))
    floorplan = next((image_url(item.get("img")) for item in album
                      if re.search(r"\b(?:plan|floorplan)\b", str(item.get("caption") or ""), re.I)
                      and image_url(item.get("img"))), None)
    amenities = [part.strip() for part in str(record.get("amenities") or "").split(",") if part.strip()]
    street = str(record.get("address_2") or "").strip() or name
    return {
        "name": name, "propertyNodeId": f"gsr:{node}", "propertyUrl": url,
        "sourceName": "Green Street Realty", "sourceUrl": BASE + "/",
        "sourceLogoUrl": BASE + "/image/18830/500",
        "address": {"street": street, "locality": city, "region": str(record.get("state") or "IL"),
                    "postalCode": str(record.get("zip") or "") or None,
                    "source": "Green Street Realty public property directory"},
        "mapPoint": {"latitude": lat, "longitude": lon,
                     "source": "Green Street Realty published latitude/longitude"},
        "availabilityStatus": "advertised" if offers else "not_advertised",
        "offers": offers,
        "representativeFloorplan": {
            "label": str(representative.get("title") or "").strip() or None,
            "bedrooms": room_count(representative.get("beds")),
            "bathrooms": room_count(representative.get("baths")),
            "availabilityStatus": str(representative.get("availability") or "No availability stated"),
            "availabilityRows": [], "utilitiesIncludedInFee": [],
            "amenities": amenities, "floorplanImage": floorplan,
            "photoSamples": photos[:6]
        }
    }


def collect(source, today):
    parser = PropertyScripts()
    parser.feed(source)
    if parser.record_parts is not None or len(parser.records) < 200:
        raise ValueError("Green Street directory is incomplete or its markup has changed")
    ids, urls = set(), set()
    properties = []
    for record in parser.records:
        listing = normalize(record, today)
        if listing["propertyNodeId"] in ids or listing["propertyUrl"] in urls:
            raise ValueError("Duplicate property identity in the directory")
        ids.add(listing["propertyNodeId"])
        urls.add(listing["propertyUrl"])
        properties.append(listing)
    if not any(property_["offers"] for property_ in properties):
        raise ValueError("No currently advertised properties; refusing an incomplete publish")
    return properties


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory-html", type=Path, help="Use saved public HTML for testing")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    source = args.directory_html.read_text(encoding="utf-8") if args.directory_html else fetch_directory()
    today = dt.datetime.now(dt.timezone.utc).date()
    properties = collect(source, today)
    if args.output.exists():
        old = json.loads(args.output.read_text(encoding="utf-8"))
        if len(properties) < len(old.get("properties", [])) * .8:
            raise ValueError("Unexpected directory contraction; retaining the previous feed")
    result = {"schemaVersion": 1, "collection": {
        "source": "Green Street Realty official public website", "directoryUrl": DIRECTORY,
        "capturedOn": today.isoformat(), "method": "Embedded public property-info-json records on the property directory page.",
        "buildingCount": len(properties),
        "advertisedBuildingCount": sum(bool(item["offers"]) for item in properties),
        "advertisedLayoutCount": sum(len(item["offers"]) for item in properties),
        "limitations": "Directory plans are published floor-plan offers, not live unit inventory. Leased and past-dated plans are not marked available. Coordinates are realtor-provided pins, not verified footprint centroids.",
        "futureRefreshRequirement": {"cadenceHours": 24, "implemented": False}
    }, "properties": properties}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".greenstreet-listings-", suffix=".json", dir=args.output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Published {len(properties)} Green Street properties "
          f"({result['collection']['advertisedBuildingCount']} advertising) to {args.output}")


if __name__ == "__main__":
    main()
