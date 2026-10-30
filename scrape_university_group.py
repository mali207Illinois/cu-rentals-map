"""Collect University Group's public rental directory into the CU map feed.

Normal hosted run: python3 outputs/scrape_university_group.py
Requires ``pip install playwright`` and ``python -m playwright install chromium``.
The browser visits the same public directory/search pages as a visitor. It
does not call the site's signed API, copy its credentials, or bypass a 403.

Initial verified snapshot: python3 outputs/scrape_university_group.py --snapshot
This uses directory cards observed on 2026-10-16, then reads each property's
public JSON-LD page for its official address, pin, and lead photograph.
The snapshot is *not* a daily refresh; use normal mode after hosting setup.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
BASE = "https://ugroupcu.com"
OUTPUT = ROOT / "university-group-listings.json"
SNAPSHOT = ROOT.parent / "work" / "ugroup-directory-snapshot.tsv"
USER_AGENT = "CU-Apartment-Map/1.0 (public property pages; daily rental snapshot)"
FILTERS = {
    "A": "Available August 2027",
    "N": "Immediate Move-In : Move-In Today!",
    "L": "August – Individual Leases",
    "H": "August 2027 – Houses",
    "J": "Available January 2027",
}
TERMS = {"A": "August 2027 search", "N": "Immediate move-in search",
         "L": "Individual leases search", "H": "August 2027 houses search",
         "J": "January 2027 search"}
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
TOTAL = re.compile(r"Showing\s+(\d+)\s*-\s*(\d+)\s+of\s+(\d+)\s+Properties", re.I)
PRICE = re.compile(r"\$\s*([\d,]+)\s*/\s*Per Person", re.I)


class StructuredProperty(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_script = False
        self.parts = []
        self.records = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self.in_script = True
            self.parts = []

    def handle_data(self, data):
        if self.in_script:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            self.in_script = False
            try:
                value = json.loads("".join(self.parts))
            except json.JSONDecodeError:
                return
            self.records.extend(value if isinstance(value, list) else [value])


def valid_slug(value):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ValueError(f"Unexpected University Group property slug: {value!r}")
    return value


def variant(code, beds, price, badge=""):
    if code not in FILTERS or not isinstance(beds, int) or not 0 <= beds <= 12 or \
            not isinstance(price, int) or not 100 <= price <= 10000:
        raise ValueError("Invalid public University Group directory card")
    return {"category": code, "bedrooms": beds, "price": price, "badge": badge}


def snapshot_cards(path=SNAPSHOT):
    cards = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        slug, raw = line.split("|", 1)
        valid_slug(slug)
        if slug in cards:
            raise ValueError(f"Duplicate snapshot slug: {slug}")
        cards[slug] = []
        for item in raw.split(";"):
            code, beds, price = item.split(":")
            cards[slug].append(variant(code, int(beds), int(price)))
    if len(cards) < 100:
        raise ValueError("University Group snapshot is incomplete")
    return cards


def live_cards():
    """Read only ordinary, publicly rendered cards; never replay the signed API."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Install Playwright and Chromium for live directory refresh") from exc

    cards = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.goto(BASE + "/", wait_until="domcontentloaded", timeout=45000)
            page.locator(".property-card").first.wait_for(timeout=30000)
            previous_filter = None
            for code, label in FILTERS.items():
                if previous_filter:
                    previous = page.locator(".stats").inner_text()
                    previous_href = page.locator(".property-card a.btn-more-info").first.get_attribute("href")
                    page.get_by_label(previous_filter, exact=True).uncheck()
                    page.get_by_label(label, exact=True).check()
                    page.wait_for_function("""([stats, href]) => {
                      const current = document.querySelector('.stats')?.textContent || '';
                      const first = document.querySelector('.property-card a.btn-more-info')?.getAttribute('href');
                      return current && (current !== stats || first !== href);
                    }""", arg=[previous, previous_href], timeout=30000)
                    # Both checkbox changes can start searches. Wait for the
                    # final browser-rendered result, not the intermediate one.
                    page.wait_for_load_state("networkidle", timeout=30000)
                previous_filter = label
                seen = 0
                total = None
                for page_number in range(1, 51):
                    page.locator(".property-card").first.wait_for(timeout=30000)
                    stats = page.locator(".stats").inner_text()
                    matched = TOTAL.search(stats)
                    if not matched:
                        raise ValueError(f"University Group search did not return a count: {stats}")
                    start, end, count = map(int, matched.groups())
                    if start != seen + 1 or end != min(start + 11, count) or (total is not None and count != total):
                        raise ValueError(f"University Group directory changed while paging: {stats}")
                    total = count
                    rows = page.locator(".property-card").evaluate_all("""nodes => nodes.map(card => ({
                      slug: card.querySelector('a.btn-more-info')?.getAttribute('href') || '',
                      badge: card.querySelector('.badge-available')?.textContent?.trim() || '',
                      price: card.querySelector('.price-amount')?.textContent?.trim() || '',
                      bedrooms: card.querySelector('.info-value')?.textContent?.trim() || ''
                    }))""")
                    if len(rows) != end - start + 1:
                        raise ValueError("University Group directory returned incomplete cards")
                    for row in rows:
                        if re.search(r"fully leased|not available", row["badge"], re.I):
                            continue
                        slug = row["slug"].removeprefix("/property-details/")
                        valid_slug(slug)
                        amount = PRICE.fullmatch(row["price"])
                        beds = 0 if row["bedrooms"].lower() == "studio" else re.fullmatch(
                            r"(\d+) bedroom", row["bedrooms"], re.I)
                        if not amount or beds is None:
                            raise ValueError(f"Missing published starting price/bedrooms for {slug}")
                        record = variant(code, 0 if beds == 0 else int(beds.group(1)),
                                         int(amount.group(1).replace(",", "")), row["badge"])
                        if record not in cards.setdefault(slug, []):
                            cards[slug].append(record)
                    seen += len(rows)
                    if seen == total:
                        break
                    next_page = page_number + 1
                    page.locator(".pagination").get_by_role("button", name=str(next_page), exact=True).click()
                    page.get_by_text(f"Showing {seen + 1}-{min(seen + 12, total)} of {total} Properties").wait_for(
                        timeout=30000)
                if seen != total:
                    raise ValueError("University Group directory exceeded pagination safety cap")
            if len(cards) < 50:
                raise ValueError("University Group directory is unexpectedly small")
        finally:
            browser.close()
    return cards


def official_url(value, host="ugroupcu.com"):
    url = urllib.parse.urlsplit(str(value or ""))
    if url.scheme != "https" or url.hostname != host or url.username or url.password:
        return ""
    return urllib.parse.urlunsplit(url)


def images(record):
    raw = record.get("image")
    values = raw if isinstance(raw, list) else [raw]
    result = []
    for value in values:
        url = urllib.parse.urlsplit(str(value or ""))
        if url.scheme == "https" and url.hostname in ("admin.ugroupcu.com", "ugroupcu.com"):
            result.append(urllib.parse.urlunsplit(url))
    return list(dict.fromkeys(result))[:8]


def property_page(slug, variants):
    url = BASE + "/property-details/" + valid_slug(slug)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=35) as response:
        if official_url(response.geturl()) != url:
            raise ValueError("Property page redirected away from its published URL")
        body = response.read(500_000).decode("utf-8", errors="replace")
    parser = StructuredProperty()
    parser.feed(body)
    matches = [item for item in parser.records if isinstance(item, dict) and
               item.get("@type") in ("ApartmentComplex", "Apartment") and item.get("url") == url]
    if len(matches) != 1:
        raise ValueError("Property page lacks matching public structured data")
    item = matches[0]
    address = item.get("address") or {}
    geo = item.get("geo") or {}
    street = str(address.get("streetAddress") or "").strip()
    city = str(address.get("addressLocality") or "").strip()
    name = str(item.get("name") or "").strip()
    try:
        lat, lon = float(geo["latitude"]), float(geo["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Missing published coordinates") from exc
    if not name or not street or city not in ("Champaign", "Urbana") or \
            str(address.get("addressRegion") or "").strip() != "IL" or not (39.9 < lat < 40.25) or \
            not (-88.45 < lon < -88.0) or not (math.isfinite(lat) and math.isfinite(lon)):
        raise ValueError("Incomplete or out-of-region published address/pin")

    offers = []
    for record in variants:
        code, beds, price = record["category"], record["bedrooms"], record["price"]
        signature = (beds, price, code)
        if any(offer["_signature"] == signature for offer in offers):
            continue
        offers.append({
            "_signature": signature,
            "unitTypeNodeId": f"ugroup:{slug}:{code}:{beds}:{price}",
            "title": "Directory starting price (not a floor plan)",
            "bedrooms": beds, "bathrooms": None,
            "rent": {"perPersonUsd": {"min": price, "max": price, "display": f"From ${price:,}"}},
            "availableTerms": [],
            "searchCategory": TERMS[code],
            "statusText": f"Listed in {TERMS[code]}; confirm current unit availability with University Group"
        })
    for offer in offers:
        del offer["_signature"]
    photos = images(item)
    first = offers[0]
    return {
        "name": name, "propertyNodeId": f"ugroup:{slug}", "propertyUrl": url,
        "sourceName": "University Group", "sourceUrl": BASE + "/",
        "sourceLogoUrl": BASE + "/images/logo.png",
        "source": {"assetHosts": ["admin.ugroupcu.com"]},
        "address": {"street": street, "locality": city, "region": "IL",
                    "postalCode": str(address.get("postalCode") or "").strip(),
                    "source": "University Group property-page JSON-LD"},
        "mapPoint": {"latitude": lat, "longitude": lon,
                     "source": "University Group published property coordinates"},
        "availabilityStatus": "advertised", "offers": offers,
        "representativeFloorplan": {
            "label": "Directory starting price", "bedrooms": first["bedrooms"], "bathrooms": None,
            "availabilityStatus": first["statusText"], "availabilityRows": [],
            "utilitiesIncludedInFee": [], "amenities": [],
            "floorplanImage": None, "photoSamples": photos
        }
    }


def build(cards):
    def validated_page(slug, variants):
        for attempt in range(2):
            try:
                return property_page(slug, variants)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise  # Do not retry or work around access restrictions.
                if attempt:
                    raise
            except (ValueError, OSError):
                if attempt:
                    raise
            time.sleep(.75)

    properties, skipped = [], []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {}
        for slug, variants in cards.items():
            futures[pool.submit(validated_page, slug, variants)] = slug
            time.sleep(0.35)  # Keep discovery and detail reads low-rate.
        for future in as_completed(futures):
            slug = futures[future]
            try:
                properties.append(future.result())
            except (ValueError, OSError, KeyError) as exc:
                skipped.append({"slug": slug, "reason": str(exc)[:160]})
    if len(properties) < max(50, math.ceil(len(cards) * .8)):
        raise ValueError(f"Only {len(properties)}/{len(cards)} property pages validated; old feed retained. "
                         f"First failures: {skipped[:5]}")
    properties.sort(key=lambda x: x["name"].casefold())
    return properties, skipped


def publish(properties, skipped, origin):
    feed = {
        "schemaVersion": 1,
        "collection": {
            "source": "University Group public rental directory and property-page JSON-LD",
            "directoryUrl": BASE + "/", "capturedOn": dt.date.today().isoformat(),
            "method": origin, "buildingCount": len(properties),
            "advertisedBuildingCount": len(properties),
            "directoryStartingPriceCount": sum(len(item["offers"]) for item in properties),
            "skippedPropertyCount": len(skipped),
            "limitations": "Directory-card prices are published starting prices per person, not guaranteed unit quotes. "
                           "The public JSON-LD supplies a lead photo and a property pin, not a verified building "
                           "footprint or complete unit-level availability. Check the realtor page for current terms.",
            "futureRefreshRequirement": {"cadenceHours": 24, "implemented": False}
        }, "properties": properties
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=ROOT, prefix=".ugroup-",
                                     suffix=".json", delete=False) as output:
        temp_path = Path(output.name)
        json.dump(feed, output, ensure_ascii=False, indent=2)
        output.write("\n")
    os.replace(temp_path, OUTPUT)
    print(f"Published {len(properties)} University Group property pages; skipped {len(skipped)}.")
    for problem in skipped[:10]:
        print(f"  skipped {problem['slug']}: {problem['reason']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", action="store_true", help="Revalidate the observed directory-card snapshot")
    args = parser.parse_args()
    cards = snapshot_cards() if args.snapshot else live_cards()
    properties, skipped = build(cards)
    publish(properties, skipped, "Public directory snapshot + live property pages" if args.snapshot else
            "Public browser-rendered directory + live property pages")


if __name__ == "__main__":
    main()
