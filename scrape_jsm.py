"""Publish JSM's public building directory and currently advertised offers.

Run from the project directory: python3 outputs/scrape_jsm.py
Only official public pages are requested. A failed/incomplete run leaves the
previous JSON untouched; a successful run atomically replaces it.
"""

import argparse
import datetime as dt
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "jsm-listing-samples.json"
BASE = "https://www.jsmliving.com"
AVAILABILITY = BASE + "/search-available-units"
BUILDINGS = BASE + "/buildings"
USER_AGENT = "CU-Apartment-Map/1.0 (public JSM building directory; low-rate read-only fetch)"


def fetch(url):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=25) as response:
                if response.geturl().split("/")[2] not in ("www.jsmliving.com", "jsmliving.com"):
                    raise ValueError("Unexpected redirect from JSM")
                return response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Could not fetch {url}") from exc
            time.sleep(2 * (attempt + 1))


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")


def text(fragment):
    parser = PlainText()
    parser.feed(fragment)
    return html.unescape(" ".join(" ".join(parser.parts).split()))


def lines(fragment):
    parser = PlainText()
    parser.feed(fragment)
    return [html.unescape(" ".join(part.split())).strip() for part in
            "".join(parser.parts).split("\n") if part.strip()]


def extract(pattern, source, default="", flags=re.S):
    match = re.search(pattern, source, flags)
    return match.group(1) if match else default


def field(source, field_name):
    return extract(r'<div class="field field--name-field-' + re.escape(field_name) +
                   r'\b[^>]*>(.*?)</div>', source)


def official_photo_urls(source, limit=6):
    photos = []
    for path in re.findall(r'(?:href)="(/sites/default/files/[^"?]+\.(?:jpg|jpeg|png|webp))"', source, re.I):
        url = BASE + html.unescape(path)
        if url not in photos:
            photos.append(url)
        if len(photos) >= limit:
            break
    return photos


def price(label):
    if not label:
        return None
    amount_text = extract(r'\$\s*([\d,]+(?:\.\d+)?(?:\s*-\s*\$?[\d,]+(?:\.\d+)?)?)', label)
    amounts = [float(value.replace(",", "")) for value in
               re.findall(r'[\d,]+(?:\.\d+)?', amount_text)]
    if not amounts:
        return None
    def compact(value):
        return int(value) if value.is_integer() else value
    return {"min": compact(amounts[0]), "max": compact(amounts[-1]), "display": label}


def directory_and_offers(source):
    options = extract(r'<select name="building">(.*?)</select>', source)
    directory = {node: text(name) for node, name in
                 re.findall(r'<option value="(\d+)">(.*?)</option>', options)}
    settings = extract(r'<script type="application/json" data-drupal-selector="drupal-settings-json">(.*?)</script>', source)
    map_points = json.loads(settings).get("unitsSearch", {}).get("mapPoints", [])
    cards = re.findall(r'<article\b[^>]*class="unit-card\b.*?</article>', source, re.S)
    offers = {node: [] for node in directory}
    card_ids = set()
    for card in cards:
        unit_id = extract(r'data-history-node-id="(\d+)"', card)
        node = extract(r'field--name-field-building\b.*?<a href="/node/(\d+)"', card)
        if not node or node not in directory or not unit_id:
            raise ValueError("Availability card without a known building/unit")
        card_ids.add(unit_id)
        beds = extract(r'unit__card-bedrooms.*?<p>(\d+)\s+Beds?</p>', card)
        baths = extract(r'unit__card-bathrooms.*?<p>(\d+)\s+Baths?</p>', card)
        rent = {}
        for css_class, key in (("rent_unit", "perUnitUsd"), ("rent_pp", "perPersonUsd")):
            value = price(text(extract(r'class="' + css_class + r'"[^>]*>(.*?)</div>', card)))
            if value:
                rent[key] = value
        if not beds or not baths or not rent:
            raise ValueError(f"JSM offer {unit_id} is missing its published layout or rent")
        terms = text(extract(r'unit__card-availability"[^>]*>(.*?)</div>', card))
        terms = [term.strip() for term in terms.removeprefix("Available:").split(",") if term.strip()]
        offer = {
            "unitTypeNodeId": unit_id,
            "bedrooms": int(beds) if beds else None,
            "bathrooms": int(baths) if baths else None,
            "rent": rent,
            "availableTerms": terms,
            "statusText": text(extract(r'class=[\'\"]unit__card-summary[\'\"][^>]*>(.*?)</div>', card)),
        }
        offers[node].append(offer)
    if len(directory) < 20 or len(cards) < 20 or len(card_ids) != len(cards):
        raise ValueError("Availability page is incomplete or unexpectedly changed")
    if len(map_points) != len(cards) or {str(p["id"]) for p in map_points} != card_ids:
        raise ValueError("JSM map-point IDs do not match the advertised units")
    return directory, offers, map_points


def official_directory(source):
    cards = re.findall(r'<article data-history-node-id="(\d+)" class="building-card\b(.*?)</article>', source, re.S)
    directory = {}
    for node, card in cards:
        # Drupal now serves some links as /index.php/node/{id} while older
        # pages use /node/{id}; both are the same official building URL.
        name = text(extract(r'<h2>\s*<a href="/(?:index\.php/)?node/' +
                            re.escape(node) + r'"[^>]*>(.*?)</a>', card))
        if not name or node in directory:
            raise ValueError("Building directory contains an incomplete or duplicate card")
        directory[node] = name
    if len(directory) < 20:
        raise ValueError("Official building directory is incomplete or unexpectedly changed")
    return directory


def detail(source, node):
    if not re.search(r'<article data-history-node-id="' + re.escape(node) + r'" class="building\b', source):
        raise ValueError(f"JSM detail page for {node} is not a building page")
    latitude = extract(r'<meta property="latitude" content="([\d.-]+)"', source)
    longitude = extract(r'<meta property="longitude" content="([\d.-]+)"', source)
    if not latitude or not longitude:
        raise ValueError(f"JSM building {node} has no published location")
    building_gallery = extract(r'<div class="building__photos">(.*?)<div class="building__description">', source)
    building_photos = official_photo_urls(building_gallery, limit=5)
    units = re.findall(r'<article\b[^>]*class="unit\b[^>]*\bteaser\b.*?</article>', source, re.S)
    return float(latitude), float(longitude), building_photos, units


def representative(units, advertised_unit_ids, building_photos):
    matching = next((unit for unit in units if extract(r'data-history-node-id="(\d+)"', unit)
                     in advertised_unit_ids), units[0] if units else "")
    label = text(field(matching, "apartment-class"))
    heading = text(extract(r'<h2>(.*?)</h2>', matching))
    beds = extract(r'(\d+)\s+Beds?', heading)
    if not beds and re.search(r'\bStudio\b', heading, re.I):
        beds = '0'
    baths = extract(r'(\d+)\s+Baths?', heading)
    utilities = text(field(matching, "utility-and-service"))
    amenities = lines(field(matching, "amenities"))
    gallery = extract(r'<div class="unit__photos">(.*?)<div class="unit__description">', matching)
    photos = official_photo_urls(gallery, limit=6)
    floorplan_image = photos[0] if photos and re.search(r'(?:floor|plan|br-|studio)', photos[0], re.I) else None
    if floorplan_image:
        photos = photos[1:]
    displayed_photos = list(dict.fromkeys(building_photos + photos))[:6]
    result = {"label": label or None, "bedrooms": int(beds) if beds else None,
              "bathrooms": int(baths) if baths else None, "availabilityRows": [],
              "utilitiesIncludedInFee": [part.strip() for part in utilities.split(",") if part.strip()],
              "amenities": amenities, "floorplanImage": floorplan_image,
              "photoSamples": displayed_photos}
    table = extract(r'<table class=[\'\"]responsive-table[\'\"]>(.*?)</table>', matching)
    rows = re.findall(r'<tr>(.*?)</tr>', table, re.S)
    for row in rows[1:]:
        cells = [text(value) for value in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        if len(cells) >= 4:
            rent = price(cells[2])
            fee = price(cells[3])
            result["availabilityRows"].append({"term": cells[0],
                 "rentUsd": rent["min"] if rent else None,
                 "unitUtilityFeeUsd": fee["min"] if fee else None})
    return result


def build_properties(directory, offers, map_points, cached_pages=None, delay=.5):
    by_title = {}
    for point in map_points:
        by_title.setdefault(point["title"], []).append(point)
    properties = []
    for index, (node, name) in enumerate(directory.items(), 1):
        if cached_pages is None:
            source = fetch(f"{BASE}/node/{node}")
            time.sleep(delay)
        else:
            source = (cached_pages / f"{node}.html").read_text(encoding="utf-8")
        lat, lng, photos, units = detail(source, node)
        if not 40.0 < lat < 40.25 or not -88.4 < lng < -88.05:
            raise ValueError(f"JSM building {node} has coordinates outside Champaign-Urbana")
        points = by_title.get(name, [])
        if points and any(abs(lat - p["lat"]) > .00001 or abs(lng - p["lng"]) > .00001 for p in points):
            raise ValueError(f"Published map point disagrees with detail page for {node}")
        active_offers = offers.get(node, [])
        property_record = {
            "name": name,
            "propertyNodeId": node,
            "propertyUrl": f"{BASE}/node/{node}",
            "sourceName": "JSM Living",
            "sourceUrl": BASE + "/",
            "sourceLogoUrl": BASE + "/themes/custom/custom/images/JSM-logo.png",
            "address": {"street": name.split(" - ")[0], "locality": None,
                        "region": "IL", "postalCode": None, "source": "JSM listing title"},
            "mapPoint": {"latitude": lat, "longitude": lng,
                         "source": "JSM property detail page latitude/longitude metadata",
                         "mapPointUnitId": points[0]["id"] if points else None},
            "availabilityStatus": "advertised" if active_offers else "not_advertised",
            "offers": active_offers,
            "representativeFloorplan": representative(units, {o["unitTypeNodeId"] for o in active_offers}, photos),
        }
        properties.append(property_record)
        print(f"{index}/{len(directory)} {name}: {len(active_offers)} advertised layouts", flush=True)
    return properties


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--availability-html", type=Path, help="Use a saved public page for testing")
    parser.add_argument("--buildings-html", type=Path, help="Use a saved public building directory for testing")
    parser.add_argument("--detail-directory", type=Path, help="Use saved detail pages for testing")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    page = args.availability_html.read_text(encoding="utf-8") if args.availability_html else fetch(AVAILABILITY)
    advertised_directory, offers, points = directory_and_offers(page)
    buildings_page = args.buildings_html.read_text(encoding="utf-8") if args.buildings_html else fetch(BUILDINGS)
    directory = official_directory(buildings_page)
    if not set(advertised_directory).issubset(directory):
        raise ValueError("Available JSM buildings are absent from the official directory")
    if any(directory[node] != name for node, name in advertised_directory.items()):
        raise ValueError("JSM building names disagree between public directories")
    print(f"Found {len(directory)} JSM buildings, {sum(map(len, offers.values()))} advertised layouts", flush=True)
    properties = build_properties(directory, offers, points, args.detail_directory)
    old = None
    if args.output.exists():
        old = json.loads(args.output.read_text(encoding="utf-8"))
    if old and len(properties) < len(old.get("properties", [])) // 2:
        raise ValueError("Unexpected directory contraction; retaining old data")
    collection = {
        "source": "JSM Living official public website",
        "availabilityUrl": AVAILABILITY,
        "directoryUrl": BUILDINGS,
        "capturedOn": dt.datetime.now(dt.timezone.utc).date().isoformat(),
        "method": "Public building directory and availability cards; coordinates, photos and details from official building pages.",
        "buildingCount": len(properties),
        "advertisedBuildingCount": sum(bool(p["offers"]) for p in properties),
        "advertisedLayoutCount": sum(len(p["offers"]) for p in properties),
        "limitations": "Offers are advertised floor-plan types, not unit inventory. No-availability properties are present but not marked for rent. Coordinates are JSM map pins, not verified footprint centroids.",
        "futureRefreshRequirement": {"cadenceHours": 24, "implemented": False}
    }
    result = {"schemaVersion": 1, "collection": collection, "properties": properties}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".jsm-listings-", suffix=".json", dir=args.output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Published {len(properties)} properties to {args.output}")


if __name__ == "__main__":
    main()
