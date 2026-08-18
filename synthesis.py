"""Synthesis Agent -- receives structured sub-agent JSON for a set of
candidates and produces a cited, ranked RankedReport. Implements §4.2 of the
build spec, including its hard citation rule, enforced here by a validator
retry loop rather than trusted on the model's word.
"""

import json
import os

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

from citation_validator import validate_rationale
from schemas import RankedReport, SiteSelectionQuery
from tracing_setup import traced
from weight_reallocation import reallocate_weights

load_dotenv()

SYNTHESIS_SYSTEM_PROMPT = """You are the Synthesis Agent for the Site Selection Copilot. You receive structured
JSON from up to five upstream agents for a set of candidate sites, plus the user's
priority_weights and hard_constraints. You do not call any tools yourself.

1. Filter out any candidate that violates a hard_constraint.

2. Score each remaining candidate against ITS OWN "adjusted_priority_weights" field,
   not the raw priority_weights. adjusted_priority_weights has already had weight
   redistributed away from any dimension that's missing or not "ok" for that specific
   candidate, proportionally onto the dimensions that do have data -- so a candidate
   missing zoning data isn't penalized for it, it's just judged on what's actually
   known about it. Still add an explicit data_gaps entry for anything missing (e.g.
   "zoning risk could not be determined for this address; verify with the city before
   committing") so the user knows what wasn't factored in.

3. Rank up to the top 3 remaining candidates -- never more than the number of
   candidates you were actually given, and never list the same candidate twice.
   If fewer than 3 remain after filtering, just rank those.

4. For each, write a one-paragraph rationale. HARD RULE: every quantitative claim
   (a number, percentage, or dollar figure) in the rationale must be a value that
   appears verbatim in the JSON you were given for that candidate. If you want to
   make a claim you can't support with an input field, phrase it qualitatively
   instead, or leave it out. You are checked by an automated citation validator that
   rejects and retries responses containing unsupported numbers -- write
   conservatively.

5. Write one sentence stating the clearest tradeoff between the #1 and #2 ranked
   candidates.

6. Output valid JSON matching the RankedReport schema. No free text outside the
   schema."""

_MAX_RETRIES = 3
_MODEL = "claude-sonnet-4-5"

# None of the five data sources measure commercial rent or occupancy cost, so a
# stated budget priority has no dimension to land in -- priority_weights only
# covers access/demographics/competition/labor/zoning. Left unhandled, the model
# fills the gap with the nearest available proxy (median household income) and
# ranks the *most expensive* neighbourhood top for a cost-sensitive user, which
# is precisely backwards. Detecting the intent lets us say what we can't measure
# instead of quietly answering a different question.
_COST_INTENT_KEYWORDS = [
    "budget", "cheap", "afford", "low rent", "low-rent", "inexpensive",
    "economical", "cost-conscious", "cost conscious", "low cost", "low-cost",
    "lean", "bootstrap", "minimize cost", "keep costs",
]

COST_BLIND_SPOT_GAP = (
    "Occupancy cost was a stated priority but is not measured: none of the five data sources "
    "carry commercial rent or lease rates, so this ranking does not account for affordability. "
    "Note that high median household income often signals higher rent, so the most affluent "
    "areas here may be the least budget-friendly — verify asking rents before deciding."
)

_COST_PROMPT_BLOCK = """

IMPORTANT -- the user expressed a budget or cost priority, and you CANNOT evaluate it.
No upstream agent provides commercial rent, lease rates, or any occupancy cost. Therefore:
- Do NOT treat a high median household income as favourable for this user. Affluent areas
  typically carry higher rent, which is unmeasured here, so affluence is at best ambiguous
  and may be a cost risk.
- Do NOT substitute wage data (labor) for occupancy cost -- staffing cost is not rent.
- Say plainly in the rationale of any high-income candidate that its affordability is unknown.
- Rank on the factors you can actually measure, and let the tradeoff sentence make the
  affordability gap explicit rather than implying the ranking accounts for budget."""


def detect_cost_sensitivity(*texts: str) -> bool:
    haystack = " ".join(t.lower() for t in texts if t)
    return any(kw in haystack for kw in _COST_INTENT_KEYWORDS)


@traced("synthesis_agent")
def run_synthesis(
    query: SiteSelectionQuery, candidate_data: list[dict], api_key: str = None, raw_query_text: str = ""
) -> tuple[RankedReport, dict]:
    """Returns (report, trace) -- trace records the reallocated weights per
    candidate and the citation-validator retry history, so a "view the
    thinking" UI has something real to show instead of just the final text.
    """
    llm = ChatAnthropic(model=_MODEL, temperature=0, api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
    structured_llm = llm.with_structured_output(RankedReport)

    cost_sensitive = detect_cost_sensitivity(
        raw_query_text, query.business_type, " ".join(query.hard_constraints)
    )
    system_prompt = SYNTHESIS_SYSTEM_PROMPT + (_COST_PROMPT_BLOCK if cost_sensitive else "")

    enriched_data = []
    reallocation_trace = {}
    for entry in candidate_data:
        adjusted = reallocate_weights(query.priority_weights, entry)
        reallocation_trace[entry["candidate"]["address"]] = adjusted
        enriched_data.append({**entry, "adjusted_priority_weights": adjusted})

    # Validate against the union of every candidate's data, not just the one
    # being described. A good rationale compares candidates ("density of 5501.8
    # versus Westlake's 816.5") and the model was legitimately handed all of
    # that data -- scoping the check to a single candidate flagged those
    # comparative figures as fabricated and withheld the best-written
    # rationales. The guarantee that matters is unchanged: every number must
    # come from real upstream data rather than being invented.
    all_sources = {"candidates": enriched_data, "query": query.model_dump()}
    user_payload = {"query": query.model_dump(), "candidate_data": enriched_data}

    trace = {"reallocated_weights": reallocation_trace, "citation_retries": []}

    feedback = ""
    report = None
    for attempt in range(_MAX_RETRIES):
        messages = [
            ("system", system_prompt),
            ("user", json.dumps(user_payload, indent=2) + feedback),
        ]
        report = structured_llm.invoke(messages)

        problems = []
        for rc in report.top_candidates:
            ok, unsupported = validate_rationale(rc.rationale, all_sources)
            if not ok:
                problems.append((rc.candidate.address, unsupported))

        trace["citation_retries"].append(
            {"attempt": attempt + 1, "problems": [{"address": a, "unsupported_numbers": n} for a, n in problems]}
        )

        if not problems:
            _flag_cost_blind_spot(report, cost_sensitive)
            return report, trace

        feedback = (
            "\n\nThe following rationales contained numbers not present in the source JSON above. "
            "Rewrite them using ONLY numbers that appear verbatim in the source data, or phrase "
            "qualitatively instead:\n" + "\n".join(f"- {addr}: {nums}" for addr, nums in problems)
        )

    # Retries exhausted -- degrade honestly rather than ship an unverified rationale.
    for rc in report.top_candidates:
        ok, _ = validate_rationale(rc.rationale, all_sources)
        if not ok:
            rc.rationale = (
                "Rationale withheld: automated citation check could not verify all figures "
                "against source data after repeated attempts. Manual review required."
            )
            rc.data_gaps.append("citation_validation_failed")
    _flag_cost_blind_spot(report, cost_sensitive)
    return report, trace


def _flag_cost_blind_spot(report: RankedReport, cost_sensitive: bool) -> None:
    """Adds the affordability gap to every candidate, in code rather than by
    asking the model nicely. The user stated a priority the system genuinely
    cannot evaluate, so this warning has to be guaranteed, not best-effort."""
    if not cost_sensitive:
        return
    for rc in report.top_candidates:
        if not any("Occupancy cost" in g for g in rc.data_gaps):
            rc.data_gaps.insert(0, COST_BLIND_SPOT_GAP)
