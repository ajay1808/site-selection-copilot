"""Suggests real candidate addresses spread across a city, for users who want
to explore a market before they have specific sites in mind.

Every suggestion is a real, existing commercial address -- nothing here
invents a plausible-looking street number. The pipeline is:

  1. Resolve the city to a real bounding box (Nominatim).
  2. Ask OpenStreetMap (Overpass) for commercial POIs inside that box --
     shops, restaurants, cafes and the like. These mark places where retail
     actually exists today, which is the honest proxy for "a storefront could
     plausibly go here."
  3. Bin those POIs into a spatial grid over the city and take one per cell,
     so the results are genuinely spread out rather than all clustered
     downtown (which is what naive sampling gives you, since POI density is
     heavily centre-weighted).
  4. Reverse-geocode each pick to a real street address and neighborhood name.

If any step comes back empty, this returns fewer suggestions (or none) rather
than padding the list with invented locations.
"""

import math
import time

import httpx
from shapely.geometry import Point, shape

from schemas import CandidateSite

NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "site-selection-copilot/0.1 (candidate suggestion; https://github.com/ajay1808/site-selection-copilot)"

# Nominatim's usage policy caps clients at 1 request/second. We make one
# search plus one reverse call per suggestion, so this is the pacing floor.
_NOMINATIM_MIN_INTERVAL_S = 1.1
_last_nominatim_call = 0.0

# A city bbox can be enormous (NYC's spans ~40km). Overpass queries over a
# huge box with a broad tag filter are slow and unkind to a shared public
# instance, so the search area is clamped to a sensible metro core.
_MAX_BBOX_SPAN_KM = 25.0

_COMMERCIAL_FILTERS = [
    '["shop"]',
    '["amenity"~"^(restaurant|cafe|fast_food|bar|pharmacy|bank)$"]',
]
_MAX_POIS = 400


def _throttle_nominatim():
    global _last_nominatim_call
    elapsed = time.monotonic() - _last_nominatim_call
    if elapsed < _NOMINATIM_MIN_INTERVAL_S:
        time.sleep(_NOMINATIM_MIN_INTERVAL_S - elapsed)
    _last_nominatim_call = time.monotonic()


def _clamp_bbox(south: float, north: float, west: float, east: float) -> tuple[float, float, float, float]:
    """Shrinks an oversized city bbox toward its centre, so Overpass isn't
    asked to scan an entire metro region."""
    lat_c, lon_c = (south + north) / 2, (west + east) / 2
    max_lat_span = _MAX_BBOX_SPAN_KM / 111.0
    max_lon_span = _MAX_BBOX_SPAN_KM / (111.0 * max(math.cos(math.radians(lat_c)), 0.01))

    if north - south > max_lat_span:
        south, north = lat_c - max_lat_span / 2, lat_c + max_lat_span / 2
    if east - west > max_lon_span:
        west, east = lon_c - max_lon_span / 2, lon_c + max_lon_span / 2
    return south, north, west, east


def _city_geometry(city: str):
    """Resolves a city name to (bbox, boundary polygon).

    The polygon matters: a bounding box around Manhattan also covers Hoboken,
    Brooklyn and part of New Jersey, so box-only filtering suggests addresses
    in cities the user didn't ask about. Nominatim can return the real
    administrative boundary, which we test points against instead.
    """
    _throttle_nominatim()
    resp = httpx.get(
        NOMINATIM_SEARCH_URL,
        params={"q": city, "format": "json", "limit": 1, "polygon_geojson": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=20.0,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None, None

    result = results[0]
    # Nominatim returns boundingbox as [south, north, west, east], stringified.
    south, north, west, east = (float(v) for v in result["boundingbox"])
    bbox = _clamp_bbox(south, north, west, east)

    boundary = None
    geojson = result.get("geojson")
    if geojson and geojson.get("type") in ("Polygon", "MultiPolygon"):
        try:
            boundary = shape(geojson)
        except Exception:
            boundary = None  # unusable geometry -- fall back to bbox-only filtering
    return bbox, boundary


def _commercial_pois(bbox: tuple[float, float, float, float]) -> list[dict]:
    south, north, west, east = bbox
    bbox_str = f"{south},{west},{north},{east}"
    clauses = "\n".join(f"  node{f}({bbox_str});" for f in _COMMERCIAL_FILTERS)
    query = f"[out:json][timeout:40];\n(\n{clauses}\n);\nout center {_MAX_POIS};"

    resp = httpx.post(
        OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=60.0
    )
    resp.raise_for_status()
    pois = []
    for el in resp.json().get("elements", []):
        if el.get("type") == "node" and "lat" in el:
            lat, lon = el["lat"], el["lon"]
        elif "center" in el:
            lat, lon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        pois.append({"lat": lat, "lon": lon, "tags": el.get("tags", {})})
    return pois


def _tag_address(tags: dict) -> dict | None:
    """Street address components straight from the POI's own OSM addr:* tags.

    Preferred over reverse-geocoding when present: these are the storefront's
    actual mapped address rather than whatever building the coordinate lands
    nearest to. Often partial (a house number and street but no city), so the
    caller fills the gaps from the reverse geocode.
    """
    house, street = tags.get("addr:housenumber"), tags.get("addr:street")
    if not (house and street):
        return None
    return {
        "street": f"{house} {street}",
        "city": tags.get("addr:city"),
        "state": tags.get("addr:state"),
        "postcode": tags.get("addr:postcode"),
    }


def _format_address(components: dict) -> str | None:
    if not components.get("street"):
        return None
    tail = " ".join(p for p in (components.get("state"), components.get("postcode")) if p)
    parts = [components["street"], components.get("city"), tail or None]
    return ", ".join(p for p in parts if p)


def _spread_pick(pois: list[dict], bbox: tuple[float, float, float, float], n: int) -> list[dict]:
    """Grid-bins the POIs and returns the best one per cell.

    Sampling POIs at random would over-represent the downtown core, where
    commercial density is highest. Binning first guarantees the suggestions
    actually span the city. Within a cell, a POI carrying a complete mapped
    address beats one closer to the cell's centre -- a precise address is
    worth more than a few hundred metres of positioning.
    """
    if not pois:
        return []

    south, north, west, east = bbox
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)

    picks = []
    for r in range(rows):
        for c in range(cols):
            if len(picks) >= n:
                break
            lat_lo = south + (north - south) * r / rows
            lat_hi = south + (north - south) * (r + 1) / rows
            lon_lo = west + (east - west) * c / cols
            lon_hi = west + (east - west) * (c + 1) / cols
            lat_c, lon_c = (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2

            in_cell = [p for p in pois if lat_lo <= p["lat"] < lat_hi and lon_lo <= p["lon"] < lon_hi]
            if not in_cell:
                continue  # genuinely nothing commercial in this part of the city
            best = min(
                in_cell,
                key=lambda p: (
                    0 if _tag_address(p["tags"]) else 1,
                    (p["lat"] - lat_c) ** 2 + (p["lon"] - lon_c) ** 2,
                ),
            )
            picks.append(best)
    return picks


def _reverse_geocode(lat: float, lon: float) -> tuple[dict, str | None] | None:
    """Returns (address components, neighborhood) for a coordinate, or None."""
    _throttle_nominatim()
    resp = httpx.get(
        NOMINATIM_REVERSE_URL,
        params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1, "zoom": 18},
        headers={"User-Agent": USER_AGENT},
        timeout=20.0,
    )
    resp.raise_for_status()
    addr = resp.json().get("address")
    if not addr:
        return None

    road = addr.get("road")
    house = addr.get("house_number")
    components = {
        "street": f"{house} {road}" if (house and road) else road,
        "city": addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality"),
        "state": addr.get("ISO3166-2-lvl4", "").split("-")[-1] or addr.get("state"),
        "postcode": addr.get("postcode"),
    }
    neighborhood = addr.get("neighbourhood") or addr.get("suburb") or addr.get("quarter") or addr.get("city_district")
    return components, neighborhood


def generate_candidates(city: str, n: int = 5) -> list[CandidateSite]:
    """Returns up to `n` real commercial addresses spread across `city`.

    Returns fewer (or an empty list) if the city can't be resolved, has no
    mapped commercial POIs, or reverse-geocoding fails -- never invents one.
    """
    bbox, boundary = _city_geometry(city)
    if bbox is None:
        return []

    pois = _commercial_pois(bbox)
    if boundary is not None:
        pois = [p for p in pois if boundary.contains(Point(p["lon"], p["lat"]))]
    picks = _spread_pick(pois, bbox, n)

    candidates: list[CandidateSite] = []
    seen_addresses = set()
    for poi in picks:
        lat, lon = poi["lat"], poi["lon"]
        reverse = _reverse_geocode(lat, lon)
        reverse_components, neighborhood = reverse if reverse else ({}, None)

        # Merge per field: the POI's own tags are the more precise source for
        # the street line, but they're frequently missing city/state/postcode,
        # which the reverse geocode reliably supplies. An address missing its
        # city geocodes badly downstream, so completeness matters here.
        components = dict(reverse_components)
        for field, value in (_tag_address(poi["tags"]) or {}).items():
            if value:
                components[field] = value

        address = _format_address(components)
        if not address or address in seen_addresses:
            continue

        seen_addresses.add(address)
        candidates.append(CandidateSite(address=address, lat=lat, lon=lon, neighborhood=neighborhood))
    return candidates
