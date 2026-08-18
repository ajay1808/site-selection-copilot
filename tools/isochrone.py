import math
import os
import time

import httpx
from dotenv import load_dotenv
from shapely.geometry import shape

from city_registry import default_city, get_city
from schemas import CandidateSite, IsochroneResult
from tracing_setup import traced

load_dotenv()

_PROFILE_BY_MODE = {"drive": "driving-car", "walk": "foot-walking"}

ORS_PUBLIC_API_URL = "https://api.openrouteservice.org"

# Confirmed live against the real API on 2026-08-12 via the x-ratelimit-*
# response headers (not third-party docs, which turned out stale/JS-gated):
# the isochrones endpoint on the free plan allows 500 requests/day, rolling.
# There's no per-minute figure in the headers ORS actually returns, so this
# per-minute figure is a conservative, community-reported estimate -- treat
# it as a safety margin, not a guarantee.
_PUBLIC_API_PER_MINUTE_LIMIT = 20

# Populated from the real x-ratelimit-* response headers after every public
# API call, keyed by API key so concurrent users (e.g. on a shared public
# deployment, each with their own key) never see each other's quota.
quota_status: dict[str, dict] = {}

_recent_request_times: list[float] = []


def _throttle_public_api():
    now = time.monotonic()
    _recent_request_times[:] = [t for t in _recent_request_times if now - t < 60]
    if len(_recent_request_times) >= _PUBLIC_API_PER_MINUTE_LIMIT:
        sleep_for = 60 - (now - _recent_request_times[0])
        if sleep_for > 0:
            time.sleep(sleep_for)
    _recent_request_times.append(time.monotonic())


def _docker_base_url(city: str) -> str | None:
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


def _failed(candidate, mode, minutes) -> IsochroneResult:
    return IsochroneResult(
        candidate=candidate,
        catchment_geojson={},
        population_weighted_access_score=0.0,
        mode=mode,
        minutes=minutes,
        status="failed",
    )


@traced("get_isochrone")
def get_isochrone(
    candidate: CandidateSite, mode: str, minutes: int, city: str = None, ors_mode: str = None, api_key: str = None
) -> IsochroneResult:
    city = city or default_city()
    profile = _PROFILE_BY_MODE[mode]
    payload = {
        "locations": [[candidate.lon, candidate.lat]],
        "range": [minutes * 60],
        "range_type": "time",
    }

    ors_mode = (ors_mode or os.environ.get("ORS_MODE", "docker")).lower()

    if ors_mode == "api":
        api_key = api_key or os.environ.get("ORS_API_KEY")
        if not api_key:
            return _failed(candidate, mode, minutes)

        _throttle_public_api()
        try:
            resp = httpx.post(
                f"{ORS_PUBLIC_API_URL}/v2/isochrones/{profile}",
                json=payload,
                headers={"Authorization": api_key},
                timeout=30.0,
            )
            quota_status[api_key] = {
                "limit": resp.headers.get("x-ratelimit-limit"),
                "remaining": resp.headers.get("x-ratelimit-remaining"),
                "reset_epoch": resp.headers.get("x-ratelimit-reset"),
            }
            resp.raise_for_status()
            data = resp.json()
            feature = data["features"][0]
        except (httpx.HTTPError, KeyError, IndexError):
            return _failed(candidate, mode, minutes)
    else:
        base_url = _docker_base_url(city)
        if base_url is None:
            return _failed(candidate, mode, minutes)
        try:
            resp = httpx.post(f"{base_url}/v2/isochrones/{profile}", json=payload, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
            feature = data["features"][0]
        except (httpx.HTTPError, KeyError, IndexError):
            return _failed(candidate, mode, minutes)

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
