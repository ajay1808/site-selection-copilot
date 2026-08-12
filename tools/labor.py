import os

import httpx
from dotenv import load_dotenv

from schemas import CandidateSite, LaborResult
from tracing_setup import traced

load_dotenv()

BLS_API_KEY = os.environ.get("BLS_API_KEY")
BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# county_fips -> 7-digit BLS metro area (CBSA) code, zero-padded. Phase 0 is
# Austin-only; extend this map when Phase 3 adds Houston/Dallas coverage.
_COUNTY_TO_MSA_AREA_CODE = {
    "48453": "0012420",  # Travis County, TX -> Austin-Round Rock-Georgetown MSA
}

_DATATYPE_ANNUAL_25TH = "12"
_DATATYPE_ANNUAL_MEDIAN = "13"
_DATATYPE_ANNUAL_75TH = "14"


def _oews_series_id(area_code: str, occupation_code: str, datatype: str) -> str:
    return f"OEUM{area_code}000000{occupation_code}{datatype}"


def _laus_unemployment_series_id(county_fips: str) -> str:
    return f"LAUCN{county_fips}{'0' * 8}03"


@traced("get_labor_profile")
def get_labor_profile(candidate: CandidateSite, county_fips: str, occupation_code: str) -> LaborResult:
    area_code = _COUNTY_TO_MSA_AREA_CODE.get(county_fips)
    if area_code is None:
        return LaborResult(
            candidate=candidate,
            county_fips=county_fips,
            relevant_occupation_code=occupation_code,
            wage_p25=None,
            wage_p50=None,
            wage_p75=None,
            unemployment_rate_pct=None,
            status="failed",
        )

    occ_code = occupation_code.replace("-", "")
    series_ids = {
        "p25": _oews_series_id(area_code, occ_code, _DATATYPE_ANNUAL_25TH),
        "p50": _oews_series_id(area_code, occ_code, _DATATYPE_ANNUAL_MEDIAN),
        "p75": _oews_series_id(area_code, occ_code, _DATATYPE_ANNUAL_75TH),
        "unemployment": _laus_unemployment_series_id(county_fips),
    }

    try:
        resp = httpx.post(
            BLS_URL,
            json={"seriesid": list(series_ids.values()), "registrationkey": BLS_API_KEY, "latest": True},
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
            county_fips=county_fips,
            relevant_occupation_code=occupation_code,
            wage_p25=None,
            wage_p50=None,
            wage_p75=None,
            unemployment_rate_pct=None,
            status="failed",
        )
