"""Streamlit UI for the Site Selection Copilot.

Run with:
    streamlit run app.py
"""

import json
import subprocess
import sys

import streamlit as st

import city_registry
from orchestrator import Session
from schemas import CandidateSite
from tools.zoning import _geocode

st.set_page_config(page_title="Site Selection Copilot", page_icon="🗺️", layout="wide")

DIMENSION_LABELS = {
    "access": "Access",
    "demographics": "Demographics",
    "competition": "Competition",
    "labor": "Labor",
    "zoning": "Zoning",
}


def init_state():
    if "session" not in st.session_state:
        st.session_state.session = Session()
    if "candidates" not in st.session_state:
        st.session_state.candidates = []
    if "history" not in st.session_state:
        st.session_state.history = []  # list of (role, content, extra)
    if "onboard_log" not in st.session_state:
        st.session_state.onboard_log = ""


def geocode_and_add(address: str):
    latlon = _geocode(address)
    if latlon is None:
        st.sidebar.error(f"couldn't geocode: {address}")
        return
    lat, lon = latlon
    st.session_state.candidates.append(CandidateSite(address=address, lat=lat, lon=lon))


def render_thinking(candidate_data: list[dict], synthesis_trace: dict | None):
    st.markdown("##### 🔍 Agent thinking")
    reallocated = (synthesis_trace or {}).get("reallocated_weights", {})
    retries = (synthesis_trace or {}).get("citation_retries", [])

    for entry in candidate_data:
        address = entry["candidate"]["address"]
        with st.expander(address):
            adj = reallocated.get(address)
            if adj:
                st.caption("Priority weights, reallocated for this candidate's available data:")
                st.json(adj)
            for key in ("isochrone", "demographics", "competitors", "labor", "zoning"):
                if key in entry:
                    result = entry[key]
                    status = result.get("status", "?")
                    icon = {"ok": "✅", "failed": "❌", "no_coverage": "⚪", "degraded": "🟡"}.get(status, "?")
                    st.markdown(f"**{icon} {key}** — `{status}`")
                    st.json({k: v for k, v in result.items() if k != "candidate"})

    if retries:
        st.markdown("###### Citation validator retry history")
        for r in retries:
            n_problems = len(r["problems"])
            if n_problems == 0:
                st.markdown(f"- attempt {r['attempt']}: ✅ all rationales grounded")
            else:
                st.markdown(f"- attempt {r['attempt']}: ⚠️ {n_problems} rationale(s) had unsupported numbers, retrying")
                for p in r["problems"]:
                    st.caption(f"  {p['address']}: {p['unsupported_numbers']}")


def render_report(report):
    if not report.top_candidates:
        st.warning(report.top_tradeoff)
        return

    for rc in report.top_candidates:
        st.markdown(f"**#{rc.rank} — {rc.candidate.address}**")
        st.write(rc.rationale)
        if rc.data_gaps:
            for gap in rc.data_gaps:
                st.caption(f"⚠️ {gap}")
        st.divider()

    st.info(f"**Tradeoff:** {report.top_tradeoff}")

    with st.expander("agents_called / agents_skipped"):
        st.write("called:", report.agents_called)
        if report.agents_skipped:
            st.write("skipped:", report.agents_skipped)


def onboard_city_ui(city_name: str):
    st.session_state.onboard_log = ""
    with st.status(f"Onboarding '{city_name}'...", expanded=True) as status:
        proc = subprocess.Popen(
            [sys.executable, "onboard_city.py", city_name, "--yes"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        log_box = st.empty()
        for line in proc.stdout:
            st.session_state.onboard_log += line
            log_box.code(st.session_state.onboard_log)
        proc.wait()
        if proc.returncode == 0:
            status.update(label=f"'{city_name}' onboarded", state="complete")
        else:
            status.update(label=f"Onboarding '{city_name}' failed", state="error")


# ---- UI ----

init_state()

st.title("🗺️ Site Selection Copilot")
st.caption("Ask a question, get a ranked, cited recommendation. Every number is checked against real data before you see it.")

with st.sidebar:
    st.header("Setup")

    cities = city_registry.list_cities()
    city = st.selectbox("City", cities, index=cities.index(city_registry.default_city()) if city_registry.default_city() in cities else 0)
    entry = city_registry.get_city(city)
    if entry:
        zoning_note = "✅ real zoning data" if entry.get("zoning_coverage") else "⚪ no zoning source yet"
        st.caption(f"{entry['display_name']} — {zoning_note}")

    with st.expander("➕ Onboard a new city"):
        new_city = st.text_input("City name", placeholder="e.g. Houston")
        if st.button("Onboard", disabled=not new_city):
            onboard_city_ui(new_city)
            st.rerun()

    st.divider()
    st.subheader("Candidate addresses")
    if "addr_input_key" not in st.session_state:
        st.session_state.addr_input_key = 0
    new_addr = st.text_input(
        "Add an address", placeholder="1100 Congress Ave, Austin, TX 78701",
        key=f"addr_input_{st.session_state.addr_input_key}",
    )
    if st.button("Add", disabled=not new_addr):
        geocode_and_add(new_addr)
        st.session_state.addr_input_key += 1  # forces a fresh, empty widget next render
        st.rerun()

    for i, c in enumerate(st.session_state.candidates):
        col1, col2 = st.columns([5, 1])
        col1.caption(c.address)
        if col2.button("✕", key=f"rm_{i}"):
            st.session_state.candidates.pop(i)
            st.rerun()

    st.divider()
    st.subheader("Priority weights")
    st.caption("Optional — leave alone to let the copilot infer weights from your question.")
    use_manual_weights = st.checkbox("Set weights manually")
    manual_weights = {}
    if use_manual_weights:
        for dim, label in DIMENSION_LABELS.items():
            manual_weights[dim] = st.slider(label, 0.0, 1.0, 0.2, 0.05)
        total = sum(manual_weights.values()) or 1.0
        manual_weights = {k: round(v / total, 3) for k, v in manual_weights.items()}
        st.caption(f"normalized to sum to 1.0: {manual_weights}")

    st.divider()
    st.subheader("Hard constraints")
    constraints_text = st.text_area("One per line", placeholder="needs a drive-thru\nrent under $8,000/mo", height=80)

    st.divider()
    show_thinking = st.checkbox("🔍 Show agent thinking", value=False)

    if st.button("🔄 Reset conversation"):
        st.session_state.session = Session()
        st.session_state.history = []
        st.rerun()

# main chat area
for role, content, extra in st.session_state.history:
    with st.chat_message(role):
        if role == "assistant" and extra and extra.get("report"):
            render_report(extra["report"])
            if show_thinking and extra.get("candidate_data"):
                render_thinking(extra["candidate_data"], extra.get("synthesis_trace"))
        else:
            st.write(content)

if len(st.session_state.candidates) == 0:
    st.info("Add at least one candidate address in the sidebar to get started.")
else:
    prompt = st.chat_input("e.g. Best spot for a fast-casual restaurant, budget-conscious.")
    if prompt:
        st.session_state.history.append(("user", prompt, None))
        with st.chat_message("user"):
            st.write(prompt)

        query_text = prompt
        if use_manual_weights:
            query_text += f"\n\n[Priority weights: {json.dumps(manual_weights)}]"
        constraints = [c.strip() for c in constraints_text.splitlines() if c.strip()]
        if constraints:
            query_text += f"\n\n[Hard constraints: {'; '.join(constraints)}]"

        with st.chat_message("assistant"):
            with st.spinner("Running the five data tools and synthesizing a ranking..."):
                result = st.session_state.session.run(query_text, st.session_state.candidates, city=city)

            if result.clarification_needed:
                st.write(result.clarification_needed)
                st.session_state.history.append(("assistant", result.clarification_needed, None))
            else:
                render_report(result.report)
                extra = {
                    "report": result.report,
                    "candidate_data": result.candidate_data,
                    "synthesis_trace": result.synthesis_trace,
                }
                if show_thinking and result.candidate_data:
                    render_thinking(result.candidate_data, result.synthesis_trace)
                st.session_state.history.append(("assistant", "", extra))
