"""LLM-as-judge per build spec §4.3 -- grades a single candidate rationale
against its upstream sub-agent JSON and the user's priority_weights."""

import os

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, Field

from tracing_setup import traced

load_dotenv()

JUDGE_SYSTEM_PROMPT = """You are grading a single candidate-site rationale written by the Site Selection
Copilot. You will be given: the upstream sub-agent JSON for that candidate, the
user's priority_weights, and the rationale text. Score each dimension 1-5:

- evidence_groundedness: does every number in the rationale appear in the upstream
  JSON? (5 = fully grounded, 1 = contains fabricated or unsupported figures)
- logical_consistency: does the reasoning follow from the stated priority_weights,
  or does it contradict them? (5 = fully consistent, 1 = contradicts stated
  priorities)
- tradeoff_clarity: is the tradeoff against the next-best candidate stated clearly
  and specifically, not vaguely? (5 = specific and actionable, 1 = absent or vague)
- actionability: could a real site-selection analyst act on this without needing to
  ask a follow-up question? (5 = fully actionable, 1 = too vague to act on)

Output JSON: {"evidence_groundedness": int, "logical_consistency": int,
"tradeoff_clarity": int, "actionability": int, "notes": str}

Be strict on evidence_groundedness in particular -- this is the dimension the whole
system is designed around. A single unsupported number should cap that score at 2,
regardless of how well-written the rationale otherwise is."""

_MODEL = "claude-sonnet-4-5"


class JudgeScore(BaseModel):
    evidence_groundedness: int = Field(ge=1, le=5)
    logical_consistency: int = Field(ge=1, le=5)
    tradeoff_clarity: int = Field(ge=1, le=5)
    actionability: int = Field(ge=1, le=5)
    notes: str


@traced("llm_judge")
def judge_rationale(candidate_json: dict, priority_weights: dict, rationale: str, tradeoff: str) -> JudgeScore:
    llm = ChatAnthropic(model=_MODEL, temperature=0, api_key=os.environ["ANTHROPIC_API_KEY"])
    structured = llm.with_structured_output(JudgeScore)

    import json

    user_content = json.dumps(
        {
            "upstream_sub_agent_json": candidate_json,
            "priority_weights": priority_weights,
            "rationale": rationale,
            "stated_top_tradeoff": tradeoff,
        },
        indent=2,
    )
    return structured.invoke([("system", JUDGE_SYSTEM_PROMPT), ("user", user_content)])
