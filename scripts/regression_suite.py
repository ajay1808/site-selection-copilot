"""Regression suite per build spec §5.3 -- ~15 fixed synthetic queries
(not from the golden dataset) covering different business types, weight
configurations, and edge cases. Captures baseline output for future diffing;
a significant shift on a re-run should block merge until reviewed.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import CandidateSite
from orchestrator import Session

C = lambda addr, lat, lon: CandidateSite(address=addr, lat=lat, lon=lon)

DOWNTOWN = C("1100 Congress Ave, Austin, TX 78701", 30.2747, -97.7404)
EAST_11TH = C("900 E 11th St, Austin, TX 78702", 30.2701, -97.7313)
DOMAIN = C("3110 Esperanza Crossing, Austin, TX 78758", 30.4013, -97.7226)
SOUTH_CONGRESS = C("1600 S Congress Ave, Austin, TX 78704", 30.2500, -97.7500)
WEST_LAKE_HILLS = C("3300 Bee Cave Rd, Austin, TX 78746", 30.2903, -97.8113)
MUELLER = C("1911 Aldrich St, Austin, TX 78723", 30.2989, -97.7047)
SE_RIVERSIDE = C("2438 E Riverside Dr, Austin, TX 78741", 30.2405, -97.7280)
RUNDBERG = C("1000 E Rundberg Ln, Austin, TX 78753", 30.3550, -97.6930)
UT_CAMPUS = C("2400 Guadalupe St, Austin, TX 78705", 30.2903, -97.7431)
CIRCLE_C = C("5501 W Slaughter Ln, Austin, TX 78749", 30.2036, -97.8570)
CRESTVIEW = C("7301 Burnet Rd, Austin, TX 78757", 30.3483, -97.7357)
ARBOR_TRAILS = C("4301 W William Cannon Dr, Austin, TX 78749", 30.2220, -97.8387)

CASES = [
    ("baseline_spec_example", "Best spot for a fast-casual restaurant near East Austin, budget-conscious, drive-thru not required.", [EAST_11TH, SOUTH_CONGRESS, MUELLER]),
    ("coffee_shop_access_weighted", "Where should a coffee shop open in downtown Austin? High foot traffic matters most.", [DOWNTOWN, SOUTH_CONGRESS, UT_CAMPUS]),
    ("gym_low_competition", "Looking for a gym location in North Austin. I want low competition from other gyms.", [DOMAIN, RUNDBERG, CRESTVIEW]),
    ("grocery_demographics_weighted", "Grocery store site in Southwest Austin -- demographics (family income) matter most.", [CIRCLE_C, ARBOR_TRAILS, WEST_LAKE_HILLS]),
    ("unstaffed_kiosk_skip_labor", "We're opening an unstaffed vending-machine kiosk business in Austin.", [DOMAIN, DOWNTOWN, MUELLER]),
    ("bar_zoning_weighted", "Bar and nightlife venue near East 6th Street -- zoning risk is my biggest concern.", [EAST_11TH, DOWNTOWN, SOUTH_CONGRESS]),
    ("pharmacy_north_austin", "Pharmacy location in North Austin.", [RUNDBERG, DOMAIN, CRESTVIEW]),
    ("fast_food_avoid_saturation", "Fast food restaurant, budget-conscious, avoid saturated markets.", [SE_RIVERSIDE, RUNDBERG, CIRCLE_C]),
    ("upscale_steakhouse_affluent", "Upscale steakhouse in West Austin, catering to an affluent clientele.", [WEST_LAKE_HILLS, DOWNTOWN, MUELLER]),
    ("coworking_nonretail_edge", "Opening a coworking office space in Austin.", [DOWNTOWN, DOMAIN, MUELLER]),
    ("hard_constraint_rent_cap", "Fast-casual restaurant in Austin with rent under $8,000/month.", [DOWNTOWN, EAST_11TH, DOMAIN]),
    ("hard_constraint_drive_thru", "Restaurant with a drive-thru required, Austin.", [RUNDBERG, CIRCLE_C, SE_RIVERSIDE]),
    ("explicit_custom_weights", "Coffee shop in Austin. I care 50% about access, 30% about competition, 10% demographics, 5% labor, 5% zoning.", [DOWNTOWN, SOUTH_CONGRESS, MUELLER]),
    ("adversarial_nonsensical", "asdkfjasldkfj purple monkey dishwasher", [DOWNTOWN]),
    ("adversarial_vague_region", "Somewhere good to open a business in Texas.", [DOWNTOWN, DOMAIN, WEST_LAKE_HILLS]),
]


def main():
    results = []
    for i, (case_id, query_text, candidates) in enumerate(CASES):
        print(f"\n=== [{i+1}/{len(CASES)}] {case_id} ===")
        if i > 0:
            time.sleep(1.0)

        session = Session()
        result = session.run(query_text, candidates)

        if result.clarification_needed:
            print(f"  clarification_needed: {result.clarification_needed}")
            results.append(
                {
                    "case_id": case_id,
                    "query_text": query_text,
                    "clarification_needed": result.clarification_needed,
                    "report": None,
                }
            )
            continue

        report = result.report
        print(f"  agents_called: {report.agents_called}")
        print(f"  agents_skipped: {report.agents_skipped}")
        print(f"  top_candidates: {[rc.candidate.address for rc in report.top_candidates]}")

        results.append(
            {
                "case_id": case_id,
                "query_text": query_text,
                "clarification_needed": None,
                "report": report.model_dump(),
            }
        )

    out_path = Path(__file__).resolve().parent / "regression_baseline.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nWrote baseline for {len(results)} cases to {out_path}")


if __name__ == "__main__":
    main()
