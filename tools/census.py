import os

import httpx
from dotenv import load_dotenv

from schemas import CandidateSite, DemographicsResult
from tracing_setup import traced

load_dotenv()

CENSUS_API_KEY = os.environ.get("CENSUS_API_KEY")
ACS_YEAR = "2023"
SQ_METERS_PER_SQ_MILE = 2_589_988.11

# City-level comparison point, keyed by the candidate's state FIPS so a
# candidate outside covered territory never gets compared against the wrong
# city's median. Phase 0 is Austin-only; extend both maps when Phase 3 adds
# Houston/Dallas coverage.
_CITY_PLACE_FIPS = {"Austin": ("48", "05000")}
_STATE_FIPS_TO_CITY = {"48": "Austin"}

_CENSUS_MISSING = -666666666  # Census API's sentinel for "not available"
_city_median_income_cache: dict[str, float | None] = {}


def _clean(value) -> float | None:
    if value is None:
        return None
    num = float(value)
    return None if num == _CENSUS_MISSING else num


def _get_city_median_income(city: str) -> float | None:
    if city not in _city_median_income_cache:
        state, place = _CITY_PLACE_FIPS[city]
        resp = httpx.get(
            f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5",
            params={"get": "B19013_001E", "for": f"place:{place}", "in": f"state:{state}", "key": CENSUS_API_KEY},
            timeout=15.0,
        )
        resp.raise_for_status()
        _, row = resp.json()
        _city_median_income_cache[city] = _clean(row[0])
    return _city_median_income_cache[city]


@traced("get_census_profile")
def get_census_profile(candidate: CandidateSite) -> DemographicsResult:
    try:
        geo_resp = httpx.get(
            "https://geocoding.geo.census.gov/geocoder/geographies/coordinates",
            params={
                "x": candidate.lon,
                "y": candidate.lat,
                "benchmark": "Public_AR_Current",
                "vintage": "Current_Current",
                "layers": "Census Tracts",
                "format": "json",
            },
            timeout=15.0,
        )
        geo_resp.raise_for_status()
        tracts = geo_resp.json()["result"]["geographies"]["Census Tracts"]
        tract = tracts[0]
        state, county, tract_code = tract["STATE"], tract["COUNTY"], tract["TRACT"]
        aland_sqmi = float(tract["AREALAND"]) / SQ_METERS_PER_SQ_MILE

        acs_resp = httpx.get(
            f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5",
            params={
                "get": "B19013_001E,B01002_001E,B01003_001E",
                "for": f"tract:{tract_code}",
                "in": f"state:{state}+county:{county}",
                "key": CENSUS_API_KEY,
            },
            timeout=15.0,
        )
        acs_resp.raise_for_status()
        _, row = acs_resp.json()
        income, age, population = _clean(row[0]), _clean(row[1]), _clean(row[2])

        density = population / aland_sqmi if population is not None and aland_sqmi > 0 else None

        comparison_city = _STATE_FIPS_TO_CITY.get(state)
        city_income = _get_city_median_income(comparison_city) if comparison_city else None
        vs_city_pct = (
            (income - city_income) / city_income * 100
            if income is not None and city_income is not None
            else None
        )

        return DemographicsResult(
            candidate=candidate,
            median_household_income=income,
            median_age=age,
            population_density_per_sqmi=round(density, 1) if density is not None else None,
            vs_city_median_income_pct=round(vs_city_pct, 1) if vs_city_pct is not None else None,
            status="ok",
        )
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return DemographicsResult(
            candidate=candidate,
            median_household_income=None,
            median_age=None,
            population_density_per_sqmi=None,
            vs_city_median_income_pct=None,
            status="failed",
        )
