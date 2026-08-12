"""Tool-selection precision/recall per build spec §5.2 -- compares the
orchestrator's actual agents_called against the hand-labeled correct agent
set for each golden case (see EXPECTED_AGENTS in run_golden_eval.py)."""

import json
from pathlib import Path

from run_golden_eval import EXPECTED_AGENTS

RESULTS_PATH = Path(__file__).resolve().parent / "golden_eval_results.json"


def score_case(agents_called: set, expected: set) -> dict:
    if not agents_called:
        return {"precision": 0.0, "recall": 0.0}
    tp = len(agents_called & expected)
    precision = tp / len(agents_called)
    recall = tp / len(expected) if expected else 1.0
    return {"precision": precision, "recall": recall}


def main():
    cases = json.loads(RESULTS_PATH.read_text())
    valid = [c for c in cases if "error" not in c]

    per_case = []
    for c in valid:
        scores = score_case(set(c["agents_called"]), EXPECTED_AGENTS)
        per_case.append({"case_id": c["case_id"], **scores, "agents_called": c["agents_called"]})
        print(f"case {c['case_id']}: precision={scores['precision']:.2f} recall={scores['recall']:.2f} called={c['agents_called']}")

    mean_precision = sum(s["precision"] for s in per_case) / len(per_case) if per_case else 0.0
    mean_recall = sum(s["recall"] for s in per_case) / len(per_case) if per_case else 0.0

    print(f"\nMean precision: {mean_precision:.2f}")
    print(f"Mean recall:    {mean_recall:.2f}")

    out_path = Path(__file__).resolve().parent / "tool_selection_results.json"
    out_path.write_text(json.dumps({"per_case": per_case, "mean_precision": mean_precision, "mean_recall": mean_recall}, indent=2))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
