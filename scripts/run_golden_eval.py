"""Runs the golden dataset through the real orchestrator and captures
everything needed for Recall@3, tool-selection precision/recall, and the
LLM-as-judge pass -- one run, all three metrics scored from its output.

Known limitations (documented honestly rather than hidden):
1. All data sources are live/current-day -- there is no historical snapshot
   capability, so this is NOT a true "would the system have recommended this
   before decision_date using only pre-decision-date data" test. It measures
   whether current data supports the historical decision, which is a weaker
   but still informative signal.
2. The system has no autonomous candidate-generation tool (out of scope for
   Phases 0-1). Recall@3 here is scored over a fixed candidate set: the real
   chosen address plus real distractor addresses drawn from elsewhere in
   Austin, not an open-ended search across the whole metro.
"""

import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import CandidateSite
from orchestrator import Session

DISTRACTOR_POOL = [
    ("1100 Congress Ave, Austin, TX 78701", 30.2747, -97.7404),
    ("900 E 11th St, Austin, TX 78702", 30.2701, -97.7313),
    ("1600 S Congress Ave, Austin, TX 78704", 30.2500, -97.7500),
    ("3300 Bee Cave Rd, Austin, TX 78746", 30.2903, -97.8113),
    ("1911 Aldrich St, Austin, TX 78723", 30.2989, -97.7047),
    ("2438 E Riverside Dr, Austin, TX 78741", 30.2405, -97.7280),
    ("1000 E Rundberg Ln, Austin, TX 78753", 30.3550, -97.6930),
    ("2400 Guadalupe St, Austin, TX 78705", 30.2903, -97.7431),
    ("5501 W Slaughter Ln, Austin, TX 78749", 30.2036, -97.8570),
]

# All 8 golden cases are ordinary physical retail/food-service concepts with
# no stated reason to skip any sub-agent -- so the hand-labeled "correct"
# agent set is all five, for every case.
EXPECTED_AGENTS = {"get_isochrone", "get_census_profile", "get_poi_density", "get_labor_profile", "get_zoning_risk"}


def load_golden_cases():
    path = Path(__file__).resolve().parent / "golden_dataset.csv"
    with open(path) as f:
        return list(csv.DictReader(f))


def pick_distractors(case_id: int, n: int = 3) -> list[CandidateSite]:
    offset = (case_id - 1) * 3
    picks = [DISTRACTOR_POOL[(offset + i) % len(DISTRACTOR_POOL)] for i in range(n)]
    return [CandidateSite(address=a, lat=lat, lon=lon) for a, lat, lon in picks]


def main():
    cases = load_golden_cases()
    results = []

    for case in cases:
        case_id = int(case["case_id"])
        print(f"\n=== Case {case_id}: {case['business_type']} -> {case['actual_chosen_neighborhood']} ===")

        actual = CandidateSite(
            address=case["actual_chosen_address"], lat=float(case["lat"]), lon=float(case["lon"])
        )
        candidates = [actual] + pick_distractors(case_id)

        session = Session()
        query_text = f"Best spot for a {case['business_type']} in {case['candidate_metro']}."
        result = session.run(query_text, candidates)

        if result.clarification_needed:
            print(f"  UNEXPECTED clarification request: {result.clarification_needed}")
            results.append({"case_id": case_id, "error": "clarification_needed", "detail": result.clarification_needed})
            continue

        report = result.report
        top3_addresses = [rc.candidate.address for rc in report.top_candidates[:3]]
        hit = actual.address in top3_addresses
        agents_called = set(report.agents_called)

        print(f"  actual: {actual.address}")
        print(f"  top3:   {top3_addresses}")
        print(f"  recall@3 hit: {hit}")
        print(f"  agents_called: {sorted(agents_called)}")
        if report.agents_skipped:
            print(f"  agents_skipped: {report.agents_skipped}")

        results.append(
            {
                "case_id": case_id,
                "business_type": case["business_type"],
                "actual_address": actual.address,
                "candidate_addresses": [c.address for c in candidates],
                "top3_addresses": top3_addresses,
                "recall_hit": hit,
                "agents_called": sorted(agents_called),
                "agents_skipped": report.agents_skipped,
                "tool_selection_correct": agents_called == EXPECTED_AGENTS,
                "report": report.model_dump(),
                "candidate_data": session.candidate_data,
            }
        )
        time.sleep(1.0)

    out_path = Path(__file__).resolve().parent / "golden_eval_results.json"
    out_path.write_text(json.dumps(results, indent=2))

    valid = [r for r in results if "error" not in r]
    recall_at_3 = sum(r["recall_hit"] for r in valid) / len(valid) if valid else 0.0
    tool_selection_acc = sum(r["tool_selection_correct"] for r in valid) / len(valid) if valid else 0.0

    print("\n=== Summary ===")
    print(f"Cases run: {len(results)} ({len(valid)} valid, {len(results) - len(valid)} errored)")
    print(f"Recall@3: {recall_at_3:.2f} ({sum(r['recall_hit'] for r in valid)}/{len(valid)})")
    print(f"Tool-selection exact-match rate: {tool_selection_acc:.2f} ({sum(r['tool_selection_correct'] for r in valid)}/{len(valid)})")
    print(f"Full results written to {out_path}")


if __name__ == "__main__":
    main()
