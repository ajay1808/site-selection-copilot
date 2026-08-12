"""Adversarial / guardrail suite per build spec §5.4. Runs all four cases for
real against the live system (including actually stopping the ORS container
for the down-service case) rather than mocking the behavior being tested.
"""

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import CandidateSite, DemographicsResult, IsochroneResult, LaborResult, ZoningResult, CompetitorResult
from orchestrator import Session

RESULTS = []


def record(case: str, passed: bool, detail: str):
    RESULTS.append({"case": case, "passed": passed, "detail": detail})
    print(f"[{'PASS' if passed else 'FAIL'}] {case}: {detail}")


# --- Case 1: nonsensical business type -> should ask a clarifying question, never guess ---
print("\n=== Case 1: nonsensical business_type ===")
session = Session()
candidates = [CandidateSite(address="1100 Congress Ave, Austin, TX 78701", lat=30.2747, lon=-97.7404)]
result = session.run("I want to open a store that sells nostalgia and vibes, not sure what it actually is", candidates)
passed = result.clarification_needed is not None and result.report is None
record("nonsensical_business_type", passed, f"clarification_needed={result.clarification_needed!r}")


# --- Case 2: candidate city with zero zoning coverage -> status='no_coverage', never infer from another city ---
print("\n=== Case 2: zero zoning coverage (Houston) ===")
from tools.zoning import get_zoning_risk

houston_candidate = CandidateSite(address="1000 Main St, Houston, TX 77002", lat=29.7589, lon=-95.3677)
zoning_result = get_zoning_risk(houston_candidate, houston_candidate.address)
passed = zoning_result.status == "no_coverage" and zoning_result.risk_level == "unknown"
record("zero_zoning_coverage", passed, f"status={zoning_result.status}, risk_level={zoning_result.risk_level}")


# --- Case 3: ORS instance down -> isochrone status='failed', never fabricate a catchment ---
print("\n=== Case 3: ORS instance down ===")
subprocess.run(["docker", "stop", "site-selection-ors"], cwd=Path(__file__).resolve().parent.parent, capture_output=True)
time.sleep(2)

from tools.isochrone import get_isochrone

austin_candidate = CandidateSite(address="1100 Congress Ave, Austin, TX 78701", lat=30.2747, lon=-97.7404)
iso_result = get_isochrone(austin_candidate, mode="drive", minutes=10)
passed = iso_result.status == "failed" and iso_result.catchment_geojson == {}
record("ors_down", passed, f"status={iso_result.status}, catchment_geojson={iso_result.catchment_geojson!r}")

print("Restarting ORS container...")
subprocess.run(["docker", "start", "site-selection-ors"], cwd=Path(__file__).resolve().parent.parent, capture_output=True)
for i in range(20):
    hstatus = subprocess.run(
        ["docker", "inspect", "--format={{.State.Health.Status}}", "site-selection-ors"], capture_output=True, text=True
    ).stdout.strip()
    if hstatus == "healthy":
        print("ORS healthy again.")
        break
    time.sleep(5)


# --- Case 4: all five agents fail simultaneously -> explicit insufficient-data response, never a confident empty ranking ---
print("\n=== Case 4: all five agents fail simultaneously ===")


def failing_isochrone(candidate, mode, minutes):
    return IsochroneResult(candidate=candidate, catchment_geojson={}, population_weighted_access_score=0.0, mode=mode, minutes=minutes, status="failed")


def failing_census(candidate, city="Austin"):
    return DemographicsResult(candidate=candidate, median_household_income=None, median_age=None, population_density_per_sqmi=None, vs_city_median_income_pct=None, status="failed")


def failing_poi(candidate, catchment_geojson, category):
    return CompetitorResult(candidate=candidate, same_category_count_in_catchment=None, saturation_score=None, nearest_three_distances_miles=[], status="failed")


def failing_labor(candidate, county_fips, occupation_code):
    return LaborResult(candidate=candidate, county_fips=county_fips, relevant_occupation_code=occupation_code, wage_p25=None, wage_p50=None, wage_p75=None, unemployment_rate_pct=None, status="failed")


def failing_zoning(candidate, address):
    return ZoningResult(candidate=candidate, risk_level="unknown", citation=None, status="failed")


with patch("orchestrator.get_isochrone", failing_isochrone), \
     patch("orchestrator.get_census_profile", failing_census), \
     patch("orchestrator.get_poi_density", failing_poi), \
     patch("orchestrator.get_labor_profile", failing_labor), \
     patch("orchestrator.get_zoning_risk", failing_zoning):
    session = Session()
    result = session.run("Best spot for a coffee shop in Austin, TX.", candidates)

report = result.report
passed = (
    report is not None
    and report.top_candidates == []
    and "insufficient data" in report.top_tradeoff.lower()
)
record("all_agents_fail", passed, f"top_candidates={report.top_candidates if report else None}, top_tradeoff={report.top_tradeoff if report else None!r}")


print("\n=== Summary ===")
n_passed = sum(r["passed"] for r in RESULTS)
print(f"{n_passed}/{len(RESULTS)} passed")
for r in RESULTS:
    print(f"  [{'PASS' if r['passed'] else 'FAIL'}] {r['case']}")
