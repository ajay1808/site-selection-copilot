"""Runs the LLM-as-judge (§4.3) on every top-3 rationale produced by the
golden dataset eval. Reports mean per dimension across all rationales."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from judge import judge_rationale

RESULTS_PATH = Path(__file__).resolve().parent / "golden_eval_results.json"
OUT_PATH = Path(__file__).resolve().parent / "judge_results.json"


def find_source_json(case: dict, candidate_address: str) -> dict:
    for entry in case["candidate_data"]:
        if entry["candidate"]["address"] == candidate_address:
            return entry
    return {}


def main():
    cases = json.loads(RESULTS_PATH.read_text())
    all_scores = []
    per_case = []

    for case in cases:
        if "error" in case:
            continue
        report = case["report"]
        for rc in report["top_candidates"]:
            print(f"Judging case {case['case_id']}, rank {rc['rank']}: {rc['candidate']['address']}")
            source_json = find_source_json(case, rc["candidate"]["address"])
            score = judge_rationale(
                candidate_json=source_json,
                priority_weights=report["query"]["priority_weights"],
                rationale=rc["rationale"],
                tradeoff=report["top_tradeoff"],
            )
            record = {
                "case_id": case["case_id"],
                "candidate_address": rc["candidate"]["address"],
                "rank": rc["rank"],
                **score.model_dump(),
            }
            all_scores.append(score)
            per_case.append(record)
            print(f"  {score.model_dump()}")

    OUT_PATH.write_text(json.dumps(per_case, indent=2))

    if all_scores:
        dims = ["evidence_groundedness", "logical_consistency", "tradeoff_clarity", "actionability"]
        print("\n=== Mean scores across all rationales ===")
        for d in dims:
            mean = sum(getattr(s, d) for s in all_scores) / len(all_scores)
            print(f"  {d}: {mean:.2f}")
        print(f"n = {len(all_scores)} rationales judged")
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
