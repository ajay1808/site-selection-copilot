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


@traced("get_poi_density")
def get_poi_density(candidate: CandidateSite, catchment_geojson: dict, category: str) -> CompetitorResult:
    filters = _CATEGORY_OSM_FILTERS.get(category.lower())
    if filters is None:
        return CompetitorResult(
            candidate=candidate,
            same_category_count_in_catchment=None,
            saturation_score=None,
            nearest_three_distances_miles=[],
            status="failed",
        )

    try:
        radius_miles = _search_radius_miles(candidate, catchment_geojson["geometry"])
        points = _query_overpass(candidate, radius_miles, filters)
    except (httpx.HTTPError, KeyError, ValueError):
        return CompetitorResult(
            candidate=candidate,
            same_category_count_in_catchment=None,
            saturation_score=None,
            nearest_three_distances_miles=[],
            status="failed",
        )

    catchment_poly = shape(catchment_geojson["geometry"])
    in_catchment = sum(1 for lat, lon in points if catchment_poly.contains(Point(lon, lat)))

    distances = sorted(_haversine_miles(candidate.lat, candidate.lon, lat, lon) for lat, lon in points)
    nearest_three = [round(d, 2) for d in distances[:3]]

    # Phase 0: saturation needs a population estimate for the catchment, which
    # this tool doesn't receive (see get_census_profile's population_density_per_sqmi).
    # Wire that cross-reference in the Phase 1 orchestrator rather than guessing here.
    return CompetitorResult(
        candidate=candidate,
        same_category_count_in_catchment=in_catchment,
        saturation_score=None,
        nearest_three_distances_miles=nearest_three,
        status="degraded",
    )
