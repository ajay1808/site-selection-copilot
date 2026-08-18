"""LangGraph orchestrator for the Site Selection Copilot -- implements §4.1
of the build spec: parse the query, decide which sub-agents are needed
(logging every skip), run them, and hand everything to the Synthesis Agent.
Session-level caching skips re-running tools when a follow-up only changes
priority_weights.
"""

import os
import time
from typing import Literal, Optional, TypedDict

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from city_registry import default_city
from schemas import CandidateSite, RankedReport, SiteSelectionQuery
from synthesis import run_synthesis
from tools.census import get_census_profile
from tools.isochrone import get_isochrone
from tools.labor import get_labor_profile
from tools.poi_density import get_poi_density
from tools.zoning import get_zoning_risk
from tracing_setup import tracer

load_dotenv()

ORCHESTRATOR_SYSTEM_PROMPT = """You are the Orchestrator for the Site Selection Copilot, a multi-agent system that
recommends where a business should open its next physical location.

Given a parsed SiteSelectionQuery, decide which of the five sub-agents this query
needs:
  - get_isochrone: physical accessibility / drive-or-walk-time catchment
  - get_census_profile: who lives near the site (income, age, density)
  - get_poi_density: market saturation / competitor proximity
  - get_labor_profile: staffing cost and labor availability -- SKIP for concepts
    that are fully automated/unstaffed (e.g. a vending or kiosk business)
  - get_zoning_risk: permit risk -- always call for any physical retail or
    food-service concept

Default to calling all five. Skip an agent ONLY when the query gives an explicit,
defensible reason. Every skip must be logged with a one-sentence reason -- this log
is scored in evaluation. Never skip an agent just to save latency or tokens."""

_MODEL = "claude-sonnet-4-5"
AGENT_NAMES = ["get_isochrone", "get_census_profile", "get_poi_density", "get_labor_profile", "get_zoning_risk"]


class AgentDecision(BaseModel):
    agents_called: list[
        Literal["get_isochrone", "get_census_profile", "get_poi_density", "get_labor_profile", "get_zoning_risk"]
    ]
    agents_skipped: dict[str, str]


class ClarityCheck(BaseModel):
    is_clear: bool
    clarifying_question: Optional[str] = None


def check_clarity(query: SiteSelectionQuery, anthropic_key: str = None) -> ClarityCheck:
    llm = ChatAnthropic(model=_MODEL, temperature=0, api_key=anthropic_key or os.environ["ANTHROPIC_API_KEY"])
    structured = llm.with_structured_output(ClarityCheck)
    return structured.invoke(
        [
            (
                "system",
                "You are a guardrail check on a site-selection query's business_type field. "
                "Set is_clear=false if business_type is empty, nonsensical, a joke, or not an actual "
                "kind of physical retail/food-service business that could plausibly be researched "
                "(e.g. 'asdkfj', 'a time machine store', 'my feelings') -- and write ONE specific "
                "clarifying question to ask the user. Otherwise set is_clear=true and leave the "
                "question empty. Never guess a business_type the user didn't actually state.",
            ),
            ("user", query.model_dump_json(indent=2)),
        ]
    )


def parse_query(
    query_text: str, previous_query: Optional[SiteSelectionQuery] = None, anthropic_key: str = None
) -> SiteSelectionQuery:
    llm = ChatAnthropic(model=_MODEL, temperature=0, api_key=anthropic_key or os.environ["ANTHROPIC_API_KEY"])
    structured = llm.with_structured_output(SiteSelectionQuery)

    system = (
        "Parse the user's site-selection request into a SiteSelectionQuery. "
        "priority_weights keys are access/demographics/competition/labor/zoning and must "
        "sum to 1.0 -- infer sensible defaults if the user doesn't specify explicit weights."
    )
    if previous_query is not None:
        system += (
            "\n\nThis is a follow-up in the same session. The previous turn's query was:\n"
            f"{previous_query.model_dump_json(indent=2)}\n"
            "If the new message only adjusts some fields (e.g. priority_weights) without restating "
            "business_type, candidate_region, or hard_constraints, carry those fields forward "
            "unchanged from the previous query rather than re-inferring or dropping them."
        )

    return structured.invoke([("system", system), ("user", query_text)])


def decide_agents(query: SiteSelectionQuery, anthropic_key: str = None) -> AgentDecision:
    llm = ChatAnthropic(model=_MODEL, temperature=0, api_key=anthropic_key or os.environ["ANTHROPIC_API_KEY"])
    structured = llm.with_structured_output(AgentDecision)
    return structured.invoke(
        [
            ("system", ORCHESTRATOR_SYSTEM_PROMPT),
            ("user", query.model_dump_json(indent=2)),
        ]
    )


class GraphState(TypedDict):
    query_text: str
    candidates: list[CandidateSite]
    city: str
    api_keys: dict
    cached_query: Optional[SiteSelectionQuery]
    cached_candidates: Optional[list[CandidateSite]]
    cached_data: Optional[list[dict]]
    cached_agents_called: Optional[list[str]]
    cached_agents_skipped: Optional[dict[str, str]]
    query: SiteSelectionQuery
    clarification_needed: Optional[str]
    agents_called: list[str]
    agents_skipped: dict[str, str]
    candidate_data: list[dict]
    report: Optional[RankedReport]
    synthesis_trace: Optional[dict]


def node_parse_query(state: GraphState) -> dict:
    with tracer.start_as_current_span("orchestrator.parse_query"):
        anthropic_key = (state.get("api_keys") or {}).get("anthropic")
        return {
            "query": parse_query(
                state["query_text"], previous_query=state.get("cached_query"), anthropic_key=anthropic_key
            )
        }


def node_check_clarity(state: GraphState) -> dict:
    with tracer.start_as_current_span("orchestrator.check_clarity") as span:
        anthropic_key = (state.get("api_keys") or {}).get("anthropic")
        check = check_clarity(state["query"], anthropic_key=anthropic_key)
        span.set_attribute("is_clear", check.is_clear)
        return {"clarification_needed": None if check.is_clear else check.clarifying_question}


def node_end_clarification(state: GraphState) -> dict:
    return {"report": None}


def node_decide_agents(state: GraphState) -> dict:
    with tracer.start_as_current_span("orchestrator.decide_agents") as span:
        anthropic_key = (state.get("api_keys") or {}).get("anthropic")
        decision = decide_agents(state["query"], anthropic_key=anthropic_key)
        span.set_attribute("agents_called", ",".join(decision.agents_called))
        return {"agents_called": decision.agents_called, "agents_skipped": decision.agents_skipped}


def node_run_subagents(state: GraphState) -> dict:
    query = state["query"]
    called = state["agents_called"]
    keys = state.get("api_keys") or {}
    candidate_data = []

    for i, candidate in enumerate(state["candidates"]):
        if i > 0:
            time.sleep(2.0)  # be a good citizen on the public Overpass instance

        result = {"candidate": candidate.model_dump()}
        isochrone = None
        if "get_isochrone" in called:
            isochrone = get_isochrone(
                candidate, mode="drive", minutes=10, city=state["city"],
                ors_mode=keys.get("ors_mode"), api_key=keys.get("ors"),
            )
            result["isochrone"] = isochrone.model_dump()
        if "get_census_profile" in called:
            result["demographics"] = get_census_profile(candidate, api_key=keys.get("census")).model_dump()
        if "get_poi_density" in called:
            catchment = isochrone.catchment_geojson if isochrone else {}
            result["competitors"] = get_poi_density(candidate, catchment, query.business_type).model_dump()
        if "get_labor_profile" in called:
            labor = get_labor_profile(candidate, business_type=query.business_type, api_key=keys.get("bls"))
            result["labor"] = labor.model_dump()
        if "get_zoning_risk" in called:
            result["zoning"] = get_zoning_risk(candidate, candidate.address).model_dump()

        candidate_data.append(result)

    return {"candidate_data": candidate_data}


def node_use_cache(state: GraphState) -> dict:
    return {
        "candidate_data": state["cached_data"],
        "agents_called": state["cached_agents_called"],
        "agents_skipped": state["cached_agents_skipped"],
    }


def _all_data_failed(candidate_data: list[dict]) -> bool:
    """True if not a single called sub-agent returned status='ok' for any
    candidate -- i.e. there's nothing usable to rank on at all."""
    for c in candidate_data:
        for key in ("isochrone", "demographics", "competitors", "labor", "zoning"):
            result = c.get(key)
            if result and result.get("status") == "ok":
                return False
    return True


def node_synthesize(state: GraphState) -> dict:
    with tracer.start_as_current_span("orchestrator.synthesize") as span:
        if _all_data_failed(state["candidate_data"]):
            span.set_attribute("insufficient_data", True)
            report = RankedReport(
                query=state["query"],
                top_candidates=[],
                top_tradeoff=(
                    "Insufficient data: every called sub-agent failed for every candidate. "
                    "No recommendation can be made -- retry once the underlying data sources are available."
                ),
                agents_called=state["agents_called"],
                agents_skipped=state["agents_skipped"],
            )
            synthesis_trace = None
        else:
            anthropic_key = (state.get("api_keys") or {}).get("anthropic")
            report, synthesis_trace = run_synthesis(state["query"], state["candidate_data"], api_key=anthropic_key)
            report.agents_called = state["agents_called"]
            report.agents_skipped = state["agents_skipped"]
        return {"report": report, "synthesis_trace": synthesis_trace}


def route_after_clarity(state: GraphState) -> str:
    if state.get("clarification_needed"):
        return "end_clarification"

    query, cached_query = state["query"], state.get("cached_query")
    cached_candidates = state.get("cached_candidates")
    if cached_query is None or cached_candidates is None:
        return "decide_agents"

    same_candidates = [c.address for c in state["candidates"]] == [c.address for c in cached_candidates]
    same_region = query.candidate_region == cached_query.candidate_region
    same_constraints = query.hard_constraints == cached_query.hard_constraints
    return "use_cache" if (same_candidates and same_region and same_constraints) else "decide_agents"


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("parse_query", node_parse_query)
    graph.add_node("check_clarity", node_check_clarity)
    graph.add_node("end_clarification", node_end_clarification)
    graph.add_node("decide_agents", node_decide_agents)
    graph.add_node("run_subagents", node_run_subagents)
    graph.add_node("use_cache", node_use_cache)
    graph.add_node("synthesize", node_synthesize)

    graph.set_entry_point("parse_query")
    graph.add_edge("parse_query", "check_clarity")
    graph.add_conditional_edges(
        "check_clarity",
        route_after_clarity,
        {"end_clarification": "end_clarification", "use_cache": "use_cache", "decide_agents": "decide_agents"},
    )
    graph.add_edge("end_clarification", END)
    graph.add_edge("decide_agents", "run_subagents")
    graph.add_edge("run_subagents", "synthesize")
    graph.add_edge("use_cache", "synthesize")
    graph.add_edge("synthesize", END)

    return graph.compile()


class OrchestratorResult:
    def __init__(
        self,
        query: Optional[SiteSelectionQuery],
        clarification_needed: Optional[str],
        report: Optional[RankedReport],
        synthesis_trace: Optional[dict] = None,
        candidate_data: Optional[list[dict]] = None,
    ):
        self.query = query
        self.clarification_needed = clarification_needed
        self.report = report
        self.synthesis_trace = synthesis_trace
        self.candidate_data = candidate_data


class Session:
    """Holds cross-turn state so a follow-up that only changes priority_weights
    skips re-calling every tool (spec §4.1, step 5)."""

    def __init__(self):
        self.app = build_graph()
        self.query: Optional[SiteSelectionQuery] = None
        self.candidates: Optional[list[CandidateSite]] = None
        self.candidate_data: Optional[list[dict]] = None
        self.agents_called: list[str] = []
        self.agents_skipped: dict[str, str] = {}

    def run(
        self,
        query_text: str,
        candidates: list[CandidateSite],
        city: str = None,
        api_keys: dict = None,
    ) -> OrchestratorResult:
        result = self.app.invoke(
            {
                "query_text": query_text,
                "candidates": candidates,
                "city": city or default_city(),
                "api_keys": api_keys or {},
                "cached_query": self.query,
                "cached_candidates": self.candidates,
                "cached_data": self.candidate_data,
                "cached_agents_called": self.agents_called,
                "cached_agents_skipped": self.agents_skipped,
            }
        )

        self.query = result["query"]
        clarification = result.get("clarification_needed")

        if clarification:
            return OrchestratorResult(query=self.query, clarification_needed=clarification, report=None)

        self.candidates = candidates
        self.candidate_data = result["candidate_data"]
        self.agents_called = result["agents_called"]
        self.agents_skipped = result["agents_skipped"]

        return OrchestratorResult(
            query=self.query,
            clarification_needed=None,
            report=result["report"],
            synthesis_trace=result.get("synthesis_trace"),
            candidate_data=result["candidate_data"],
        )


if __name__ == "__main__":
    candidates = [
        CandidateSite(address="1100 Congress Ave, Austin, TX 78701", lat=30.2747, lon=-97.7404),
        CandidateSite(address="900 E 11th St, Austin, TX 78702", lat=30.2701, lon=-97.7313),
        CandidateSite(address="3110 Esperanza Crossing, Austin, TX 78758", lat=30.4013, lon=-97.7226),
    ]

    session = Session()

    print("=== Turn 1: full query ===")
    result1 = session.run(
        "Best spot for a fast-casual restaurant in Austin, TX. Budget-conscious, drive-thru not required.",
        candidates,
    )
    report1 = result1.report
    print("agents_called:", report1.agents_called)
    print("agents_skipped:", report1.agents_skipped)
    print("#1:", report1.top_candidates[0].candidate.address)
    print("tradeoff:", report1.top_tradeoff)

    print("\n=== Turn 2: follow-up, weights only (should skip tool calls) ===")
    result2 = session.run(
        "Actually I care much more about avoiding competition than income demographics.",
        candidates,
    )
    report2 = result2.report
    print("agents_called:", report2.agents_called)
    print("#1:", report2.top_candidates[0].candidate.address)
    print("tradeoff:", report2.top_tradeoff)
