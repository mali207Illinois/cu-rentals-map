"""Publish Bankier Apartments' public availability directory for the CU map.

Run: python3 outputs/scrape_bankier.py

The collector submits only the same empty/default search used by Bankier's
public directory, parses its returned property cards, and publishes the feed
atomically. It does not access resident or application information.
"""

import argparse
import datetime as dt
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "bankier-listings.json"
BASE = "https://bankierapartments.com"
DIRECTORY = BASE + "/properties/"
USER_AGENT = "CU-Apartment-Map/1.0 (public rental directory; low-rate daily snapshot)"
REGION = (39.9, 40.25, -88.45, -88.0)


class DirectoryForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.found_action = False
        self.found_search = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form" and values.get("id") == "filter":
            action = values.get("action", "")
            parsed = urllib.parse.urlsplit(action)
            if parsed.scheme != "https" or parsed.hostname not in ("bankierapartments.com", "www.bankierapartments.com"):
                raise ValueError("Bankier directory form points to an unexpected host")
            self.action = action
            self.found_action = True
        elif tag == "input" and values.get("name") == "action" and values.get("value") == "propertysearch":
            self.found_search = True


class PropertyCards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.properties = []
        self.card = None
        self.card_depth = 0
        self.active_field = None
        self.field_parts = []
        self.declared_count = None
        self.count_parts = []
        self.in_count = False

    @staticmethod
    def classes(value):
        return set(str(value or "").split())

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = self.classes(values.get("class"))

        if self.card is None and tag == "div" and "type-properties" in classes:
            self.card = {
                "id": values.get("data-id") or values.get("id", ""),
                "url": values.get("data-url", ""),
                "latitude": values.get("data-latitude", ""),
                "longitude": values.get("data-longitude", ""),
                "classes": classes,
                "name": "", "street": "", "city": "", "state": "", "postal": "",
                "price": "", "beds": "", "baths": "", "photo": ""
            }
            self.card_depth = 1
            return

        if self.card is not None:
            if tag == "div":
                self.card_depth += 1
            if "property-images-wrap" in classes:
                self.card["photo"] = values.get("data-image-url", "")
            if tag == "img" and not self.card["photo"]:
                self.card["photo"] = values.get("src", "")
            for class_name, field_name in (
                ("address", "street"), ("city", "city"), ("state", "state"),
                ("zipcode", "postal"), ("rentrange", "price"),
                ("bedsrange", "beds"), ("bathsrange", "baths")
            ):
                if class_name in classes:
                    self.active_field = field_name
                    self.field_parts = []
                    break
            if tag == "h3":
                self.active_field = "name"
                self.field_parts = []
            return

        if tag == "span" and "number" in classes:
            self.in_count = True
            self.count_parts = []

    def handle_data(self, data):
        if self.card is not None and self.active_field:
            self.field_parts.append(data)
        if self.card is None and self.in_count:
            self.count_parts.append(data)

    def handle_endtag(self, tag):
        if self.active_field and tag in ("h3", "span") and self.card is not None:
            self.card[self.active_field] = " ".join("".join(self.field_parts).split())
            self.active_field = None
            self.field_parts = []
        if tag == "span" and self.in_count:
            raw = "".join(self.count_parts).strip()
            if raw.isdigit():
                self.declared_count = int(raw)
            self.in_count = False
            self.count_parts = []
        if tag == "div" and self.card is not None:
            self.card_depth -= 1
            if self.card_depth == 0:
                self.properties.append(self.card)
                self.card = None
                self.active_field = None
                self.field_parts = []


def fetch(url, data=None):
    request = urllib.request.Request(url, data=data, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Content-Type": "application/x-www-form-urlencoded" if data else "text/html"
    })
    with urllib.request.urlopen(request, timeout=40) as response:
        if urllib.parse.urlsplit(response.geturl()).hostname not in (
                "bankierapartments.com", "www.bankierapartments.com"):
            raise ValueError("Unexpected Bankier directory redirect")
        return response.read().decode("utf-8")


def parse_money(value):
    match = re.fullmatch(r"\$\s*([\d,]+)(?:\s*[-–]\s*\$?\s*([\d,]+))?", str(value or "").strip())
    if not match:
        return None
    minimum = int(match.group(1).replace(",", ""))
    maximum = int((match.group(2) or match.group(1)).replace(",", ""))
    if not (0 < minimum <= maximum < 100000):
        return None
    display = f"${minimum:,}" if minimum == maximum else f"${minimum:,}–${maximum:,}"
    return {"min": minimum, "max": maximum, "display": display}


def parse_range(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)(?:\s*[-–]\s*(\d+(?:\.\d+)?))?\s*", str(value or ""))
    if not match:
        return None
    lower = float(match.group(1))
    upper = float(match.group(2) or match.group(1))
    if upper < lower:
        return None
    compact = lambda number: int(number) if number.is_integer() else number
    return compact(lower) if lower == upper else f"{compact(lower)}–{compact(upper)}"


def clean_url(value, allowed_hosts):
    parsed = urllib.parse.urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == item or host.endswith("." + item) for item in allowed_hosts):
        return ""
    return urllib.parse.urlunsplit(parsed)


def clean_property_url(value):
    parsed = urllib.parse.urlsplit(str(value or ""))
    host = (parsed.hostname or "").lower()
    official = host in ("bankierapartments.com", "www.bankierapartments.com") or bool(
        re.fullmatch(r"www\.[a-z0-9-]*bankierapartments\.com", host))
    if parsed.scheme != "https" or not official:
        return ""
    return urllib.parse.urlunsplit(parsed)


def normalize(record):
    identifier = str(record.get("id") or "")
    name = str(record.get("name") or "").strip()
    street = str(record.get("street") or "").strip()
    city = str(record.get("city") or "").strip()
    state = str(record.get("state") or "").strip()
    postal = str(record.get("postal") or "").strip()
    try:
        latitude, longitude = float(record["latitude"]), float(record["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Bankier property {identifier or name} has invalid coordinates") from exc
    if (not identifier.isdigit() or not name or not street or city not in ("Champaign", "Urbana") or
            state != "IL" or not (REGION[0] < latitude < REGION[1] and REGION[2] < longitude < REGION[3])):
        raise ValueError(f"Incomplete or out-of-region Bankier property: {identifier or name}")
    url = clean_property_url(record.get("url"))
    photo = clean_url(record.get("photo"), ["bankierapartments.com"])
    rent = parse_money(record.get("price"))
    bedrooms = parse_range(record.get("beds"))
    bathrooms = parse_range(record.get("baths"))
    if not url or not rent or bedrooms is None or bathrooms is None or not photo:
        raise ValueError(f"Bankier property {identifier} is missing its public price, layout range, URL, or photo")

    immediate = "propertytypes-immediate" in record["classes"]
    future = "propertytypes-future" in record["classes"]
    if "units-available" not in record["classes"]:
        raise ValueError(f"Bankier property {identifier} is not marked as currently listed")
    status = ("Listed for immediate availability" if immediate else
              "Future availability listed; exact date not stated" if future else
              "Listed as available; timing not stated")
    amenities = sorted({
        " ".join(part.capitalize() for part in token.removeprefix("amenities-").split("-"))
        for token in record["classes"] if token.startswith("amenities-")
    })
    offer = {
        "unitTypeNodeId": f"bankier:{identifier}:public-range",
        "title": "Published availability range",
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "rent": {"perMonthUsd": rent},
        "availableTerms": ["Available now"] if immediate else [],
        "statusText": status
    }
    return {
        "name": name,
        "propertyNodeId": f"bankier:{identifier}",
        "propertyUrl": url,
        "sourceName": "Bankier Apartments",
        "sourceUrl": BASE + "/",
        "sourceLogoUrl": BASE + "/wp-content/uploads/2022/05/Bankier_Logo_Med_Green_RGB-1.png",
        "source": {"assetHosts": [
            "bankierapartments.com",
            (urllib.parse.urlsplit(url).hostname or "").removeprefix("www.")
        ]},
        "address": {"street": street, "locality": city, "region": state, "postalCode": postal,
                    "source": "Bankier public property-search card"},
        "mapPoint": {"latitude": latitude, "longitude": longitude,
                     "source": "Bankier public property-search coordinates"},
        "availabilityStatus": "advertised",
        "offers": [offer],
        "representativeFloorplan": {
            "label": "Published availability range", "bedrooms": bedrooms, "bathrooms": bathrooms,
            "availabilityStatus": status, "availabilityRows": [], "utilitiesIncludedInFee": [],
            "amenities": amenities, "floorplanImage": None, "photoSamples": [photo]
        }
    }


def collect(html):
    parser = PropertyCards()
    parser.feed(html)
    if parser.card is not None or parser.declared_count is None or parser.declared_count != len(parser.properties):
        raise ValueError("Bankier property-search response is incomplete or its markup has changed")
    ids = set()
    properties = []
    for record in parser.properties:
        item = normalize(record)
        if item["propertyNodeId"] in ids:
            raise ValueError("Duplicate Bankier property identity")
        ids.add(item["propertyNodeId"])
        properties.append(item)
    if parser.declared_count and not properties:
        raise ValueError("Bankier reported results but no valid properties were parsed")
    return properties, parser.declared_count


def retrieve_results():
    index_html = fetch(DIRECTORY)
    form = DirectoryForm()
    form.feed(index_html)
    if not form.found_action or not form.found_search or not form.action:
        raise ValueError("Bankier public directory form was not found")
    endpoint = urllib.parse.urljoin(DIRECTORY, form.action)
    payload = urllib.parse.urlencode({"action": "propertysearch"}).encode("ascii")
    return fetch(endpoint, data=payload)


def publish(output, results_html, today):
    source = results_html.read_text(encoding="utf-8") if results_html else retrieve_results()
    properties, result_count = collect(source)
    result = {"schemaVersion": 1, "collection": {
        "source": "Bankier Apartments official public availability directory",
        "directoryUrl": DIRECTORY, "capturedOn": today.isoformat(),
        "method": "The default public property-search response linked from Bankier's directory form.",
        "buildingCount": len(properties), "advertisedBuildingCount": len(properties),
        "advertisedLayoutCount": len(properties), "directoryResultCount": result_count,
        "limitations": "The directory gives aggregate rent and bedroom/bathroom ranges per property, not unit-level floor plans or exact future availability dates. Its coordinates are realtor-published pins, not verified footprint centroids. Prices are shown as monthly directory rent ranges.",
        "futureRefreshRequirement": {"cadenceHours": 24, "implemented": False}
    }, "properties": properties}
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bankier-listings-", suffix=".json", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f"Published {len(properties)} currently listed Bankier properties to {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-html", type=Path, help="Use a saved public property-search response for testing")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    publish(args.output, args.results_html, dt.datetime.now(dt.timezone.utc).date())


if __name__ == "__main__":
    main()
