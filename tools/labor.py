import os

import httpx
from dotenv import load_dotenv

from schemas import CandidateSite, LaborResult
from tracing_setup import traced

load_dotenv()

BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"

_DATATYPE_ANNUAL_25TH = "12"
_DATATYPE_ANNUAL_MEDIAN = "13"
_DATATYPE_ANNUAL_75TH = "14"

# business_type keyword -> SOC occupation code, checked in order (first match
# wins). Not exhaustive -- extend as new business types come up rather than
# guessing a code at query time.
_OCCUPATION_KEYWORDS: list[tuple[list[str], str]] = [
    (["gym", "fitness", "yoga", "pilates", "climbing", "crossfit"], "39-9031"),  # Fitness Trainers & Aerobics Instructors
    (["bar", "pub", "tavern", "nightlife"], "35-3011"),  # Bartenders
    (["coffee", "cafe", "café", "bakery", "fast food", "fast-casual", "quick service", "kiosk"], "35-3023"),  # Fast Food & Counter Workers
    (["restaurant", "diner", "eatery", "bbq", "barbecue", "steakhouse", "bistro"], "35-3031"),  # Waiters & Waitresses
    (["pharmacy", "drugstore"], "29-2052"),  # Pharmacy Technicians
    (["grocery", "supermarket", "convenience", "retail", "store", "shop", "boutique"], "41-2031"),  # Retail Salespersons
    (["office", "coworking"], "43-9061"),  # Office Clerks, General
]
_DEFAULT_OCCUPATION_CODE = "41-2031"  # Retail Salespersons -- generic fallback


def infer_occupation_code(business_type: str) -> str:
    lowered = business_type.lower()
    for keywords, code in _OCCUPATION_KEYWORDS:
        if any(kw in lowered for kw in keywords):
            return code
    return _DEFAULT_OCCUPATION_CODE


def _oews_series_id(area_code: str, occupation_code: str, datatype: str) -> str:
    return f"OEUM{area_code}000000{occupation_code}{datatype}"


def _laus_unemployment_series_id(county_fips: str) -> str:
    return f"LAUCN{county_fips}{'0' * 8}03"


def _geocode_labor_geography(candidate: CandidateSite) -> tuple[str, str] | None:
    """Auto-derives (county_fips, cbsa_area_code) from coordinates -- works
    for any US location, no per-city registration needed."""
    resp = httpx.get(
        GEOCODER_URL,
        params={
            "x": candidate.lon,
            "y": candidate.lat,
            "benchmark": "Public_AR_Current",
            "vintage": "Current_Current",
            "layers": "Census Tracts,Metropolitan Statistical Areas",
            "format": "json",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    geographies = resp.json()["result"]["geographies"]

    tracts = geographies.get("Census Tracts") or []
    if not tracts:
        return None
    county_fips = tracts[0]["STATE"] + tracts[0]["COUNTY"]

    cbsas = geographies.get("Metropolitan Statistical Areas") or []
    if not cbsas:
        return None
    area_code = cbsas[0]["CBSA"].zfill(7)

    return county_fips, area_code


@traced("get_labor_profile")
def get_labor_profile(
    candidate: CandidateSite, business_type: str = "", occupation_code: str | None = None, api_key: str = None
) -> LaborResult:
    occupation_code = occupation_code or infer_occupation_code(business_type)
    api_key = api_key or os.environ.get("BLS_API_KEY")

    try:
        geo = _geocode_labor_geography(candidate)
        if geo is None:
            raise ValueError("candidate is not inside a US metro area BLS covers")
        county_fips, area_code = geo

        occ_code = occupation_code.replace("-", "")
        series_ids = {
            "p25": _oews_series_id(area_code, occ_code, _DATATYPE_ANNUAL_25TH),
            "p50": _oews_series_id(area_code, occ_code, _DATATYPE_ANNUAL_MEDIAN),
            "p75": _oews_series_id(area_code, occ_code, _DATATYPE_ANNUAL_75TH),
            "unemployment": _laus_unemployment_series_id(county_fips),
        }

        resp = httpx.post(
            BLS_URL,
            json={"seriesid": list(series_ids.values()), "registrationkey": api_key, "latest": True},
            timeout=20.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if body["status"] != "REQUEST_SUCCEEDED":
            raise ValueError(body.get("message"))

        values = {}
        for series in body["Results"]["series"]:
            data = series["data"]
            values[series["seriesID"]] = float(data[0]["value"]) if data else None

        id_to_key = {v: k for k, v in series_ids.items()}
        results = {id_to_key[sid]: val for sid, val in values.items()}

        return LaborResult(
            candidate=candidate,
            county_fips=county_fips,
            relevant_occupation_code=occupation_code,
            wage_p25=results.get("p25"),
            wage_p50=results.get("p50"),
            wage_p75=results.get("p75"),
            unemployment_rate_pct=results.get("unemployment"),
            status="ok",
        )
    except (httpx.HTTPError, KeyError, ValueError, IndexError):
        return LaborResult(
            candidate=candidate,
            county_fips="",
            relevant_occupation_code=occupation_code,
            wage_p25=None,
            wage_p50=None,
            wage_p75=None,
            unemployment_rate_pct=None,
            status="failed",
        )
