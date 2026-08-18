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


def _failed(candidate, mode, minutes, reason: str = None) -> IsochroneResult:
    return IsochroneResult(
        candidate=candidate,
        catchment_geojson={},
        population_weighted_access_score=0.0,
        mode=mode,
        minutes=minutes,
        status="failed",
        failure_reason=reason,
    )


def _post_with_retry(url: str, payload: dict, headers: dict = None, attempts: int = 3):
    """Routing is a single point of failure for two sub-agents (this one and,
    downstream, competitor density), so a transient blip shouldn't sink a whole
    candidate. Retries on timeouts, 5xx and 429 with backoff; returns the
    response, or raises the last error for the caller to record."""
    backoffs = [1.0, 3.0]
    last_error = None
    for attempt in range(attempts):
        try:
            resp = httpx.post(url, json=payload, headers=headers or {}, timeout=30.0)
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            last_error = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}"
        if attempt < len(backoffs):
            time.sleep(backoffs[attempt])
    raise httpx.HTTPError(last_error or "request failed after retries")


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
    fallback_note = None

    if ors_mode == "api":
        api_key = api_key or os.environ.get("ORS_API_KEY")
        if not api_key:
            return _failed(candidate, mode, minutes, "No OpenRouteService API key configured.")

        _throttle_public_api()
        try:
            resp = _post_with_retry(
                f"{ORS_PUBLIC_API_URL}/v2/isochrones/{profile}",
                payload,
                headers={"Authorization": api_key},
            )
            quota_status[api_key] = {
                "limit": resp.headers.get("x-ratelimit-limit"),
                "remaining": resp.headers.get("x-ratelimit-remaining"),
                "reset_epoch": resp.headers.get("x-ratelimit-reset"),
            }
            # ORS uses 403 ("Access to this API has been disallowed") for a bad
            # or revoked key, and 429 once the quota is spent -- verified
            # against the live API. Conflating them sends users to fix the
            # wrong problem.
            if resp.status_code == 403:
                return _failed(
                    candidate, mode, minutes,
                    "OpenRouteService rejected the API key. Check it's correct and still active.",
                )
            if resp.status_code == 429:
                return _failed(
                    candidate, mode, minutes,
                    "OpenRouteService rate limit hit — the free plan allows 500 isochrone requests/day.",
                )
            resp.raise_for_status()
            feature = resp.json()["features"][0]
        except httpx.HTTPError as exc:
            return _failed(candidate, mode, minutes, f"Routing service unreachable after retries ({exc}).")
        except (KeyError, IndexError):
            return _failed(candidate, mode, minutes, "Routing service returned no catchment for this point.")
    else:
        base_url = _docker_base_url(city)
        docker_problem = None
        feature = None

        if base_url is None:
            docker_problem = f"'{city}' has no local routing engine"
        else:
            try:
                resp = _post_with_retry(f"{base_url}/v2/isochrones/{profile}", payload)
                resp.raise_for_status()
                feature = resp.json()["features"][0]
            except httpx.HTTPError:
                docker_problem = f"the local routing container for '{city}' isn't responding"
            except (KeyError, IndexError):
                docker_problem = f"the point is outside the map area loaded for '{city}'"

        if feature is None:
            # Docker mode is easy to end up in accidentally (it's a click in the
            # settings dialog) and silently stops working whenever Docker isn't
            # running -- which took out accessibility scoring for every candidate
            # at once. If a public API key is available, use it rather than
            # failing the whole analysis, and say that's what happened.
            fallback_key = api_key or os.environ.get("ORS_API_KEY")
            if not fallback_key:
                return _failed(
                    candidate, mode, minutes,
                    f"Routing unavailable: {docker_problem}, and no OpenRouteService API key is set to fall back to.",
                )
            _throttle_public_api()
            try:
                resp = _post_with_retry(
                    f"{ORS_PUBLIC_API_URL}/v2/isochrones/{profile}",
                    payload,
                    headers={"Authorization": fallback_key},
                )
                quota_status[fallback_key] = {
                    "limit": resp.headers.get("x-ratelimit-limit"),
                    "remaining": resp.headers.get("x-ratelimit-remaining"),
                    "reset_epoch": resp.headers.get("x-ratelimit-reset"),
                }
                resp.raise_for_status()
                feature = resp.json()["features"][0]
                fallback_note = f"Note: {docker_problem}, so this used the public OpenRouteService API instead."
            except (httpx.HTTPError, KeyError, IndexError) as exc:
                return _failed(
                    candidate, mode, minutes,
                    f"Routing unavailable: {docker_problem}, and the public API fallback also failed ({exc}).",
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
        failure_reason=fallback_note,  # succeeded, but say so if it took the fallback path
    )
