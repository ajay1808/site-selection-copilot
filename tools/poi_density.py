import math
import time

import httpx
from shapely.geometry import Point, shape

from schemas import CandidateSite, CompetitorResult
from tracing_setup import traced

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "site-selection-copilot/0.1 (research prototype)"

# Broad business_type -> OSM tag filters. category should be a general type
# ("coffee shop"), not a chain name — extend this map as new business types
# come up rather than guessing a tag at query time.
_CATEGORY_OSM_FILTERS: dict[str, list[str]] = {
    "coffee shop": ['["amenity"="cafe"]', '["shop"="coffee"]'],
    "cafe": ['["amenity"="cafe"]'],
    "restaurant": ['["amenity"="restaurant"]'],
    "fast-casual restaurant": ['["amenity"="restaurant"]', '["amenity"="fast_food"]'],
    "fast food": ['["amenity"="fast_food"]'],
    "bar": ['["amenity"="bar"]', '["amenity"="pub"]'],
    "gym": ['["leisure"="fitness_centre"]'],
    "grocery": ['["shop"="supermarket"]', '["shop"="grocery"]'],
    "pharmacy": ['["amenity"="pharmacy"]'],
    "convenience store": ['["shop"="convenience"]'],
}

_cache: dict[tuple, list[tuple[float, float]]] = {}


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r_miles = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r_miles * math.asin(math.sqrt(a))


def _catchment_area_sqmi(polygon_geojson: dict, center_lat: float) -> float:
    poly = shape(polygon_geojson)
    miles_per_deg_lat = 69.0
    miles_per_deg_lon = 69.0 * math.cos(math.radians(center_lat))
    return poly.area * miles_per_deg_lat * miles_per_deg_lon


def _search_radius_miles(candidate: CandidateSite, catchment_geometry: dict) -> float:
    poly = shape(catchment_geometry)
    max_dist = max(
        _haversine_miles(candidate.lat, candidate.lon, y, x) for x, y in poly.exterior.coords
    )
    return max_dist + 1.0  # +1mi buffer to also catch competitors just outside the catchment


def _query_overpass(candidate: CandidateSite, radius_miles: float, filters: list[str]) -> list[tuple[float, float]]:
    cache_key = (round(candidate.lat, 4), round(candidate.lon, 4), round(radius_miles, 1), tuple(filters))
    if cache_key in _cache:
        return _cache[cache_key]

    radius_m = int(radius_miles * 1609.34)
    clauses = "\n".join(
        f'  node(around:{radius_m},{candidate.lat},{candidate.lon}){f};\n'
        f'  way(around:{radius_m},{candidate.lat},{candidate.lon}){f};'
        for f in filters
    )
    query = f"[out:json][timeout:25];\n(\n{clauses}\n);\nout center;"

    backoffs = [5, 15, 30]
    resp = None
    for attempt, wait_s in enumerate([0] + backoffs):
        if wait_s:
            time.sleep(wait_s)
        resp = httpx.post(OVERPASS_URL, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=30.0)
        if resp.status_code != 429:
            break
    resp.raise_for_status()
    elements = resp.json()["elements"]

    points = []
    for el in elements:
        if el["type"] == "node":
            points.append((el["lat"], el["lon"]))
        elif "center" in el:
            points.append((el["center"]["lat"], el["center"]["lon"]))

    _cache[cache_key] = points
    return points


# Used when no drive-time catchment is available. A 3-mile circle is a
# reasonable stand-in for a 10-minute urban drive -- less precise than a real
# isochrone, but far better than reporting "competitors unknown" just because
# the routing service happened to be down.
_FALLBACK_RADIUS_MILES = 3.0


@traced("get_poi_density")
def get_poi_density(
    candidate: CandidateSite,
    catchment_geojson: dict,
    category: str,
    population_density_per_sqmi: float = None,
) -> CompetitorResult:
    filters = _CATEGORY_OSM_FILTERS.get(category.lower())
    if filters is None:
        return CompetitorResult(
            candidate=candidate,
            same_category_count_in_catchment=None,
            saturation_score=None,
            nearest_three_distances_miles=[],
            status="failed",
            failure_reason=(
                f"'{category}' isn't in the category→OpenStreetMap tag map, so competitors can't be "
                "identified without guessing which tag to search."
            ),
        )

    # Prefer the real drive-time catchment; fall back to a radius so a routing
    # failure costs one signal instead of two.
    catchment_poly = None
    geometry = (catchment_geojson or {}).get("geometry")
    if geometry:
        try:
            catchment_poly = shape(geometry)
            radius_miles = _search_radius_miles(candidate, geometry)
            basis = "drive-time catchment"
        except (KeyError, ValueError, AttributeError):
            catchment_poly = None
    if catchment_poly is None:
        radius_miles = _FALLBACK_RADIUS_MILES
        basis = f"{_FALLBACK_RADIUS_MILES:g}-mile radius (drive-time catchment unavailable)"

    try:
        points = _query_overpass(candidate, radius_miles, filters)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return CompetitorResult(
            candidate=candidate,
            same_category_count_in_catchment=None,
            saturation_score=None,
            nearest_three_distances_miles=[],
            status="failed",
            failure_reason=f"OpenStreetMap competitor lookup failed ({type(exc).__name__}).",
        )

    if catchment_poly is not None:
        count = sum(1 for lat, lon in points if catchment_poly.contains(Point(lon, lat)))
        area_sqmi = _catchment_area_sqmi(geometry, candidate.lat)
    else:
        count = sum(
            1 for lat, lon in points
            if _haversine_miles(candidate.lat, candidate.lon, lat, lon) <= radius_miles
        )
        area_sqmi = math.pi * radius_miles ** 2

    distances = sorted(_haversine_miles(candidate.lat, candidate.lon, lat, lon) for lat, lon in points)
    nearest_three = [round(d, 2) for d in distances[:3]]

    # Competitors per 10k residents. Needs a population estimate for the search
    # area, which the caller supplies from census tract density -- the two
    # halves live in different sub-agents, so the orchestrator joins them.
    saturation = None
    if population_density_per_sqmi and area_sqmi > 0:
        population = population_density_per_sqmi * area_sqmi
        if population > 0:
            saturation = round(count / (population / 10_000), 2)

    return CompetitorResult(
        candidate=candidate,
        same_category_count_in_catchment=count,
        saturation_score=saturation,
        nearest_three_distances_miles=nearest_three,
        catchment_basis=basis,
        # "ok" only when both the count and the saturation rate are real; a
        # count without a denominator is still useful, just not the full picture.
        status="ok" if saturation is not None else "degraded",
    )
