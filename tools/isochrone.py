import math
import os

import httpx
from shapely.geometry import shape

from city_registry import default_city, get_city
from schemas import CandidateSite, IsochroneResult
from tracing_setup import traced

_PROFILE_BY_MODE = {"drive": "driving-car", "walk": "foot-walking"}


def _ors_base_url(city: str) -> str | None:
    override = os.environ.get("ORS_BASE_URL")
    if override:
        return override
    entry = get_city(city)
    if entry is None:
        return None
    return f"http://localhost:{entry['ors_port']}/ors"


def _catchment_area_sqmi(polygon_geojson: dict, center_lat: float) -> float:
    poly = shape(polygon_geojson)
    miles_per_deg_lat = 69.0
    miles_per_deg_lon = 69.0 * math.cos(math.radians(center_lat))
    return poly.area * miles_per_deg_lat * miles_per_deg_lon


@traced("get_isochrone")
def get_isochrone(candidate: CandidateSite, mode: str, minutes: int, city: str = None) -> IsochroneResult:
    city = city or default_city()
    base_url = _ors_base_url(city)
    profile = _PROFILE_BY_MODE[mode]

    if base_url is None:
        return IsochroneResult(
            candidate=candidate,
            catchment_geojson={},
            population_weighted_access_score=0.0,
            mode=mode,
            minutes=minutes,
            status="failed",
        )

    payload = {
        "locations": [[candidate.lon, candidate.lat]],
        "range": [minutes * 60],
        "range_type": "time",
    }

    try:
        resp = httpx.post(f"{base_url}/v2/isochrones/{profile}", json=payload, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        feature = data["features"][0]
    except (httpx.HTTPError, KeyError, IndexError):
        return IsochroneResult(
            candidate=candidate,
            catchment_geojson={},
            population_weighted_access_score=0.0,
            mode=mode,
            minutes=minutes,
            status="failed",
        )

    area_sqmi = _catchment_area_sqmi(feature["geometry"], candidate.lat)
    # Phase 0 placeholder: normalizes reachable area against a nominal 50-sqmi
    # catchment (roughly what a 10-min drive isochrone spans in Austin). Replace
    # with true population-weighted scoring once get_census_profile is wired
    # against real ACS tract data (Phase 1).
    access_score = max(0.0, min(100.0, (area_sqmi / 50.0) * 100.0))

    return IsochroneResult(
        candidate=candidate,
        catchment_geojson=feature,
        population_weighted_access_score=round(access_score, 1),
        mode=mode,
        minutes=minutes,
        status="ok",
    )
