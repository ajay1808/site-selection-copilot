"""Phase 0/1 linear pipeline: hardcoded tool-call sequence, no LLM orchestration.

Validates that all five sub-agent tools produce correct, consistent data for a
fixed list of candidates. The Phase 1 orchestrator replaces this hardcoded
sequence with LLM-driven routing (per §4.1 of the build spec).
"""

import time

from schemas import CandidateSite, IsochroneResult, DemographicsResult, CompetitorResult, LaborResult, ZoningResult
from tools.census import get_census_profile
from tools.isochrone import get_isochrone
from tools.labor import get_labor_profile
from tools.poi_density import get_poi_density
from tools.zoning import get_zoning_risk


class CandidateReport:
    def __init__(
        self,
        candidate: CandidateSite,
        isochrone: IsochroneResult,
        demographics: DemographicsResult,
        competitors: CompetitorResult,
        labor: LaborResult,
        zoning: ZoningResult,
    ):
        self.candidate = candidate
        self.isochrone = isochrone
        self.demographics = demographics
        self.competitors = competitors
        self.labor = labor
        self.zoning = zoning


def run_pipeline(
    candidates: list[CandidateSite],
    category: str,
    county_fips: str = "48453",
    occupation_code: str = "35-3023",
    mode: str = "drive",
    minutes: int = 10,
) -> list[CandidateReport]:
    reports = []
    for i, candidate in enumerate(candidates):
        if i > 0:
            time.sleep(2.0)  # be a good citizen on the public Overpass instance
        isochrone = get_isochrone(candidate, mode=mode, minutes=minutes)
        demographics = get_census_profile(candidate)
        competitors = get_poi_density(candidate, isochrone.catchment_geojson, category)
        labor = get_labor_profile(candidate, county_fips=county_fips, occupation_code=occupation_code)
        zoning = get_zoning_risk(candidate, candidate.address)
        reports.append(CandidateReport(candidate, isochrone, demographics, competitors, labor, zoning))
    return reports


if __name__ == "__main__":
    candidates = [
        CandidateSite(address="1100 Congress Ave, Austin, TX 78701", lat=30.2747, lon=-97.7404),
        CandidateSite(address="900 E 11th St, Austin, TX 78702", lat=30.2701, lon=-97.7313),
        CandidateSite(address="3110 Esperanza Crossing, Austin, TX 78758", lat=30.4013, lon=-97.7226),
    ]

    reports = run_pipeline(candidates, category="fast-casual restaurant")

    for r in reports:
        print(f"\n=== {r.candidate.address} ===")
        print(f"access_score:        {r.isochrone.population_weighted_access_score}  ({r.isochrone.status})")
        print(f"median_income:       {r.demographics.median_household_income}  ({r.demographics.status})")
        print(f"vs_city_income_pct:  {r.demographics.vs_city_median_income_pct}")
        print(f"pop_density_sqmi:    {r.demographics.population_density_per_sqmi}")
        print(f"competitors_in_area: {r.competitors.same_category_count_in_catchment}  ({r.competitors.status})")
        print(f"nearest_3_miles:     {r.competitors.nearest_three_distances_miles}")
        print(f"wage_p25/p50/p75:    {r.labor.wage_p25} / {r.labor.wage_p50} / {r.labor.wage_p75}  ({r.labor.status})")
        print(f"unemployment_pct:    {r.labor.unemployment_rate_pct}")
        print(f"zoning_risk:         {r.zoning.risk_level}  ({r.zoning.status}) -- {r.zoning.citation}")
