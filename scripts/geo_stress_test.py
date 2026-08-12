"""Stress test: run every tool against candidates far outside the system's
built coverage (Austin ORS graph, Austin zoning layer, US-only Census/BLS).
The point isn't to get good answers -- it's to check every tool degrades
honestly (status='failed'/'no_coverage') instead of returning silently wrong
data when asked about a place it has no business answering for.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import CandidateSite
from tools.census import get_census_profile
from tools.isochrone import get_isochrone
from tools.labor import get_labor_profile
from tools.poi_density import get_poi_density
from tools.zoning import get_zoning_risk

LOCATIONS = [
    ("Manhattan, NY", CandidateSite(address="350 5th Ave, New York, NY 10118", lat=40.747849, lon=-73.985077), "36061"),
    ("Ithaca, NY", CandidateSite(address="171 E State St, Ithaca, NY 14850", lat=42.439530, lon=-76.497470), "36109"),
    ("Bangalore, India", CandidateSite(address="MG Road, Bengaluru, Karnataka 560001, India", lat=12.9756, lon=77.6068), None),
    ("Chennai, India", CandidateSite(address="T Nagar, Chennai, Tamil Nadu 600017, India", lat=13.0418, lon=80.2341), None),
]

results = []

for label, candidate, county_fips in LOCATIONS:
    print(f"\n{'='*60}\n{label}: {candidate.address}\n{'='*60}")
    row = {"location": label, "address": candidate.address}

    iso = get_isochrone(candidate, mode="drive", minutes=10)
    print(f"isochrone: status={iso.status} area_has_geometry={bool(iso.catchment_geojson)} access_score={iso.population_weighted_access_score}")
    row["isochrone_status"] = iso.status
    row["isochrone_score"] = iso.population_weighted_access_score
    row["isochrone_has_geometry"] = bool(iso.catchment_geojson)

    demo = get_census_profile(candidate)
    print(f"census: status={demo.status} income={demo.median_household_income} vs_city_pct={demo.vs_city_median_income_pct}")
    row["census_status"] = demo.status
    row["census_income"] = demo.median_household_income
    row["census_vs_city_pct"] = demo.vs_city_median_income_pct

    poi = get_poi_density(candidate, iso.catchment_geojson, "coffee shop")
    print(f"poi_density: status={poi.status} count={poi.same_category_count_in_catchment} nearest3={poi.nearest_three_distances_miles}")
    row["poi_status"] = poi.status
    row["poi_count"] = poi.same_category_count_in_catchment
    row["poi_nearest3"] = poi.nearest_three_distances_miles

    if county_fips:
        labor = get_labor_profile(candidate, county_fips=county_fips, occupation_code="35-3023")
    else:
        labor = get_labor_profile(candidate, county_fips="00000", occupation_code="35-3023")
    print(f"labor: status={labor.status} wage_p50={labor.wage_p50} unemployment={labor.unemployment_rate_pct}")
    row["labor_status"] = labor.status
    row["labor_wage_p50"] = labor.wage_p50

    zoning = get_zoning_risk(candidate, candidate.address)
    print(f"zoning: status={zoning.status} risk_level={zoning.risk_level}")
    row["zoning_status"] = zoning.status
    row["zoning_risk_level"] = zoning.risk_level

    results.append(row)

out_path = Path(__file__).resolve().parent / "geo_stress_test_results.json"
out_path.write_text(json.dumps(results, indent=2))
print(f"\n\nWrote {out_path}")
