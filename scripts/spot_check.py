"""Phase 0 spot-check: run the linear pipeline against 10 real, geographically
diverse Austin locations and dump results to JSON for manual review."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import CandidateSite
from pipeline import run_pipeline

CANDIDATES = [
    CandidateSite(address="1100 Congress Ave, Austin, TX 78701 (Downtown/Capitol)", lat=30.2747, lon=-97.7404),
    CandidateSite(address="900 E 11th St, Austin, TX 78702 (East Austin)", lat=30.2701, lon=-97.7313),
    CandidateSite(address="3110 Esperanza Crossing, Austin, TX 78758 (The Domain)", lat=30.4013, lon=-97.7226),
    CandidateSite(address="1600 S Congress Ave, Austin, TX 78704 (South Congress)", lat=30.2500, lon=-97.7500),
    CandidateSite(address="3300 Bee Cave Rd, Austin, TX 78746 (West Lake Hills)", lat=30.2903, lon=-97.8113),
    CandidateSite(address="1911 Aldrich St, Austin, TX 78723 (Mueller)", lat=30.2989, lon=-97.7047),
    CandidateSite(address="2438 E Riverside Dr, Austin, TX 78741 (SE Riverside)", lat=30.2405, lon=-97.7280),
    CandidateSite(address="1000 E Rundberg Ln, Austin, TX 78753 (North/Rundberg)", lat=30.3550, lon=-97.6930),
    CandidateSite(address="2400 Guadalupe St, Austin, TX 78705 (UT Campus)", lat=30.2903, lon=-97.7431),
    CandidateSite(address="5501 W Slaughter Ln, Austin, TX 78749 (Circle C, SW)", lat=30.2036, lon=-97.8570),
]

if __name__ == "__main__":
    reports = run_pipeline(CANDIDATES, category="fast-casual restaurant")

    results = []
    for r in reports:
        row = {
            "address": r.candidate.address,
            "lat": r.candidate.lat,
            "lon": r.candidate.lon,
            "access_score": r.isochrone.population_weighted_access_score,
            "isochrone_status": r.isochrone.status,
            "median_household_income": r.demographics.median_household_income,
            "vs_city_median_income_pct": r.demographics.vs_city_median_income_pct,
            "median_age": r.demographics.median_age,
            "population_density_per_sqmi": r.demographics.population_density_per_sqmi,
            "census_status": r.demographics.status,
            "competitors_in_catchment": r.competitors.same_category_count_in_catchment,
            "nearest_three_miles": r.competitors.nearest_three_distances_miles,
            "competitor_status": r.competitors.status,
        }
        results.append(row)
        print(f"\n=== {row['address']} ===")
        for k, v in row.items():
            if k != "address":
                print(f"  {k}: {v}")

    out_path = Path(__file__).resolve().parent / "spot_check_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {out_path}")
