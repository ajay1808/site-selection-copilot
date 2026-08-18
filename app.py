"""Streamlit UI for the Site Selection Copilot.

Run locally with:
    streamlit run app.py

For a public deployment (Streamlit Community Cloud etc), set PUBLIC_MODE=true
in the environment/secrets. That mode requires each visitor to bring their own
API keys (kept in st.session_state only -- never written to disk, never
shared across sessions), skips Docker routing entirely (not available on
managed hosting), and doesn't try to persist anything between runs.
"""

import json
import os
import subprocess
import sys

import streamlit as st
from dotenv import load_dotenv

import candidate_generator
import city_registry
import credentials
from orchestrator import Session
from schemas import CandidateSite
from tools.zoning import _geocode

load_dotenv()

GITHUB_URL = "https://github.com/ajay1808/site-selection-copilot"


def _public_mode_enabled() -> bool:
    if os.environ.get("PUBLIC_MODE", "false").lower() == "true":
        return True
    try:
        return str(st.secrets.get("PUBLIC_MODE", "false")).lower() == "true"
    except Exception:
        return False  # no secrets.toml present -- fine, just means local/env-var only


PUBLIC_MODE = _public_mode_enabled()
REQUIRED_KEYS = ["anthropic", "census", "bls", "ors"]
KEY_ENV_NAMES = {"anthropic": "ANTHROPIC_API_KEY", "census": "CENSUS_API_KEY", "bls": "BLS_API_KEY", "ors": "ORS_API_KEY"}
KEY_LABELS = {"anthropic": "Anthropic", "census": "Census", "bls": "BLS", "ors": "ORS"}
KEY_SIGNUP_LINKS = {
    "anthropic": "[console.anthropic.com](https://console.anthropic.com) — needs billing set up",
    "census": "[api.census.gov/data/key_signup.html](https://api.census.gov/data/key_signup.html) — instant & free",
    "bls": "[bls.gov/developers](https://www.bls.gov/developers/) — instant & free",
    "ors": "[openrouteservice.org/dev](https://openrouteservice.org/dev/#/signup) — instant & free, 500 requests/day",
}

st.set_page_config(page_title="Site Selection Copilot", page_icon="🗺️", layout="wide")

st.markdown(
    """
    <style>
    [data-testid="stSidebar"] { min-width: 400px; max-width: 480px; }
    </style>
    """,
    unsafe_allow_html=True,
)

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
    if "api_keys" not in st.session_state:
        st.session_state.api_keys = {}


def geocode_and_add(address: str):
    latlon = _geocode(address)
    if latlon is None:
        st.sidebar.error(f"couldn't geocode: {address}")
        return
    lat, lon = latlon
    st.session_state.candidates.append(CandidateSite(address=address, lat=lat, lon=lon))


def candidate_label(c: CandidateSite) -> str:
    return f"{c.address} ({c.neighborhood})" if c.neighborhood else c.address


def suggest_and_add(city_name: str, n: int):
    """Fills the candidate list with real commercial addresses spread across
    the named city. Adds nothing if the lookup comes back empty."""
    with st.spinner(f"Finding commercial addresses across {city_name}…"):
        try:
            found = candidate_generator.generate_candidates(city_name, n=n)
        except Exception as exc:
            st.error(f"Couldn't reach the map data service — try again in a moment. ({type(exc).__name__})")
            return

    if not found:
        st.warning(
            f"No mapped commercial addresses found for “{city_name}”. "
            "Check the spelling, or try a more specific name like “Austin, TX”."
        )
        return

    existing = {c.address for c in st.session_state.candidates}
    added = [c for c in found if c.address not in existing]
    st.session_state.candidates.extend(added)
    st.rerun()


def render_about():
    with st.expander("ℹ️ What is this?", expanded=False):
        st.markdown(
            f"""
You ask something like *"best spot for a coffee shop, budget-conscious"* and this
looks up real data about your candidate addresses — how easy they are to reach,
who lives nearby, how many competitors are already there, what it would cost to
staff the place, and how risky the zoning is — then ranks them with a plain-English
reason for each pick. **Every number in that reason is checked against the real data
before you see it** — if it can't be verified, it gets pulled rather than shown.

Five real, live data sources power it: OpenRouteService (accessibility),
US Census Bureau (demographics), OpenStreetMap (competitors), the Bureau of Labor
Statistics (staffing cost), and municipal zoning data.

📂 **[View the source code & full write-up on GitHub]({GITHUB_URL})**
            """
        )


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


@st.dialog("🔑 Connect your API keys")
def local_key_dialog():
    st.caption(
        "Local keys live in your `.env` file. Paste one here to set it for this session, "
        "or check the box to also save it to `.env` so you don't have to re-enter it next time."
    )
    have = {k: bool(os.environ.get(v)) for k, v in KEY_ENV_NAMES.items()}

    for key in REQUIRED_KEYS:
        label = KEY_LABELS[key]
        status_icon = "✅" if have[key] else "⬜"
        st.markdown(f"**{status_icon} {label}** — {KEY_SIGNUP_LINKS[key]}")
        col1, col2 = st.columns([4, 1])
        value = col1.text_input(
            f"{label} API key", value=os.environ.get(KEY_ENV_NAMES[key], ""),
            type="password", key=f"dlg_{key}", label_visibility="collapsed",
        )
        if col2.button("Save", key=f"dlg_save_{key}", disabled=not value, use_container_width=True):
            credentials.apply_credentials({KEY_ENV_NAMES[key]: value}, persist=True)
            st.rerun()

    st.divider()
    st.markdown("**Routing mode** for accessibility scoring:")
    routing_mode = st.radio(
        "Routing mode", ["Public ORS API (no setup)", "Self-hosted Docker (no daily limit)"],
        index=0 if os.environ.get("ORS_MODE", "docker") == "api" else 1, label_visibility="collapsed",
    )
    new_mode = "api" if routing_mode.startswith("Public") else "docker"
    if new_mode != os.environ.get("ORS_MODE", "docker"):
        os.environ["ORS_MODE"] = new_mode
        st.rerun()
    if new_mode == "docker":
        st.caption("Onboard cities for Docker mode in the sidebar under “➕ Onboard a new city”.")

    st.divider()
    st.caption("Or upload a `.env` / `.json` file with any of the four keys:")
    uploaded = st.file_uploader("credentials file", type=["env", "json", "txt"], label_visibility="collapsed")
    persist = st.checkbox("Also save uploaded keys to .env", value=True)
    if uploaded is not None:
        parsed = credentials.parse_credentials_file(uploaded.name, uploaded.getvalue())
        if parsed:
            st.caption(f"Found: {', '.join(parsed.keys())}")
            if st.button("Apply uploaded credentials"):
                credentials.apply_credentials(parsed, persist=persist)
                st.rerun()
        else:
            st.warning("No recognized keys found in that file.")


def render_public_key_gate():
    st.title("🗺️ Site Selection Copilot")
    st.caption("Ask a question, get a ranked, cited recommendation. Every number is checked against real data before you see it.")
    render_about()

    st.header("🔑 Bring your own API keys to start")
    st.caption(
        "This runs on *your* keys, not the operator's — kept in your browser session only, "
        "never written to disk, never visible to other visitors. Closing the tab clears them."
    )

    for key in REQUIRED_KEYS:
        st.markdown(f"**{KEY_LABELS[key]}** — {KEY_SIGNUP_LINKS[key]}")

    with st.form("api_key_gate"):
        anthropic_in = st.text_input("Anthropic API key", type="password")
        census_in = st.text_input("Census API key", type="password")
        bls_in = st.text_input("BLS API key", type="password")
        ors_in = st.text_input("ORS API key", type="password")
        submitted = st.form_submit_button("Start", use_container_width=True, type="primary")

    st.divider()
    st.caption("Or upload a `.env` / `.json` file with the same four keys:")
    uploaded = st.file_uploader("credentials file", type=["env", "json", "txt"], label_visibility="collapsed")
    upload_keys = {}
    if uploaded is not None:
        parsed = credentials.parse_credentials_file(uploaded.name, uploaded.getvalue())
        upload_keys = {k: parsed.get(v) for k, v in KEY_ENV_NAMES.items() if parsed.get(v)}
        if upload_keys:
            st.caption(f"Found: {', '.join(upload_keys.keys())}")

    candidate_keys = {
        "anthropic": anthropic_in or upload_keys.get("anthropic"),
        "census": census_in or upload_keys.get("census"),
        "bls": bls_in or upload_keys.get("bls"),
        "ors": ors_in or upload_keys.get("ors"),
    }

    if submitted or uploaded is not None:
        missing = [KEY_LABELS[k] for k in REQUIRED_KEYS if not candidate_keys.get(k)]
        if missing:
            st.error(f"Still need: {', '.join(missing)}")
        else:
            st.session_state.api_keys = {**candidate_keys, "ors_mode": "api"}
            st.rerun()

    st.stop()


# ---- UI ----

init_state()

if PUBLIC_MODE and not all(st.session_state.api_keys.get(k) for k in REQUIRED_KEYS):
    render_public_key_gate()

st.title("🗺️ Site Selection Copilot")
st.caption("Ask a question, get a ranked, cited recommendation. Every number is checked against real data before you see it.")
render_about()

if not PUBLIC_MODE:
    have_all = all(os.environ.get(v) for v in KEY_ENV_NAMES.values())
    missing_labels = [KEY_LABELS[k] for k, v in KEY_ENV_NAMES.items() if not os.environ.get(v)]
    status_col, button_col = st.columns([5, 1])
    if have_all:
        status_col.success("✅ API keys connected")
    else:
        status_col.warning(f"⚠️ Missing API keys: {', '.join(missing_labels)}")
    if button_col.button("🔑 Manage API keys", use_container_width=True):
        local_key_dialog()
    if not have_all and not st.session_state.get("_key_dialog_auto_shown"):
        st.session_state._key_dialog_auto_shown = True
        local_key_dialog()

with st.sidebar:
    st.header("Query setup")

    if PUBLIC_MODE:
        st.success("✅ Using your session's API keys")
        st.caption("Routing: public ORS API")
        if st.button("🔁 Change keys"):
            st.session_state.api_keys = {}
            st.rerun()
        from tools.isochrone import quota_status
        q = quota_status.get(st.session_state.api_keys.get("ors"))
        if q and q.get("remaining") is not None:
            st.caption(f"Your ORS quota: {q['remaining']} / {q['limit']} requests left today")
        city = None  # not used in public mode -- API routing needs no city registration
        default_city_label = ""  # nothing registered to default to; the user names their city
    else:
        cities = city_registry.list_cities()
        city = st.selectbox("City", cities, index=cities.index(city_registry.default_city()) if city_registry.default_city() in cities else 0)
        default_city_label = city
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

    with st.expander("📍 Suggest addresses across a city"):
        st.caption("Real commercial addresses, spread across the city — a starting point if you don't have specific sites in mind.")
        sb_city = st.text_input("City", value=default_city_label, key="sb_suggest_city", placeholder="e.g. Austin, TX")
        sb_n = st.slider("How many", 3, 8, 5, key="sb_suggest_n")
        if st.button("Suggest addresses", key="sb_suggest_go", disabled=not sb_city, use_container_width=True):
            suggest_and_add(sb_city, sb_n)

    for i, c in enumerate(st.session_state.candidates):
        col1, col2 = st.columns([5, 1])
        col1.caption(candidate_label(c))
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

    st.divider()
    st.caption(f"[📂 Source on GitHub]({GITHUB_URL})")

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
    st.subheader("Start with a few candidate sites")
    st.caption(
        "This compares specific addresses against each other. Add your own in the sidebar — "
        "or, if you're still exploring a market, start from real commercial addresses spread across the city."
    )
    col_city, col_n, col_go = st.columns([3, 1, 1])
    main_city = col_city.text_input(
        "City", value=default_city_label, key="main_suggest_city",
        placeholder="e.g. Austin, TX", label_visibility="collapsed",
    )
    main_n = col_n.number_input("How many", 3, 8, 5, key="main_suggest_n", label_visibility="collapsed")
    if col_go.button("Suggest sites", disabled=not main_city, use_container_width=True, type="primary"):
        suggest_and_add(main_city, int(main_n))
    st.caption("Suggestions are real, currently-mapped commercial addresses — nothing invented.")
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
                run_kwargs = {"city": city}
                if PUBLIC_MODE:
                    run_kwargs["api_keys"] = st.session_state.api_keys
                result = st.session_state.session.run(query_text, st.session_state.candidates, **run_kwargs)

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
