from pydantic import BaseModel, Field
from typing import Literal, Optional


class SiteSelectionQuery(BaseModel):
    business_type: str                      # e.g. "fast-casual restaurant"
    candidate_region: str                   # e.g. "East Austin, TX"
    priority_weights: dict[str, float]      # keys: access, demographics, competition,
                                             # labor, zoning — must sum to 1.0
    hard_constraints: list[str] = []        # e.g. "rent under $8k/mo", "needs drive-thru"


class CandidateSite(BaseModel):
    address: str                            # kept clean -- it gets geocoded downstream
    lat: float
    lon: float
    neighborhood: Optional[str] = None      # display label only, never part of `address`


class IsochroneResult(BaseModel):
    candidate: CandidateSite
    catchment_geojson: dict
    population_weighted_access_score: float   # 0-100
    mode: Literal["drive", "walk"]
    minutes: int
    status: Literal["ok", "degraded", "failed"]
    failure_reason: Optional[str] = None


class DemographicsResult(BaseModel):
    candidate: CandidateSite
    median_household_income: Optional[float]
    median_age: Optional[float]
    population_density_per_sqmi: Optional[float]
    vs_city_median_income_pct: Optional[float]
    status: Literal["ok", "failed"]
    failure_reason: Optional[str] = None


class CompetitorResult(BaseModel):
    candidate: CandidateSite
    same_category_count_in_catchment: Optional[int]
    saturation_score: Optional[float]       # competitors per 10k residents
    nearest_three_distances_miles: list[float]
    status: Literal["ok", "degraded", "failed"]
    catchment_basis: Optional[str] = None
    failure_reason: Optional[str] = None


class LaborResult(BaseModel):
    candidate: CandidateSite
    county_fips: str
    relevant_occupation_code: str
    wage_p25: Optional[float]
    wage_p50: Optional[float]
    wage_p75: Optional[float]
    unemployment_rate_pct: Optional[float]
    status: Literal["ok", "failed"]
    failure_reason: Optional[str] = None


class ZoningResult(BaseModel):
    candidate: CandidateSite
    risk_level: Literal["low", "medium", "high", "unknown"]
    citation: Optional[str]                 # zoning code section, if found
    status: Literal["ok", "no_coverage", "failed"]
    failure_reason: Optional[str] = None


class RankedCandidate(BaseModel):
    candidate: CandidateSite
    rank: int
    rationale: str                          # every number here must trace to an
                                             # input field — see synthesis prompt
    data_gaps: list[str]                    # explicit list of anything unknown/missing


class RankedReport(BaseModel):
    query: SiteSelectionQuery
    top_candidates: list[RankedCandidate]   # max 3
    top_tradeoff: str                       # one sentence, #1 vs #2
    agents_called: list[str]
    agents_skipped: dict[str, str]          # agent name -> reason for skipping
