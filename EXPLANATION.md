# How it actually works, in detail

This is the deep-dive companion to the README: what each agent does, exactly which
API it calls and with what parameters, how the orchestrator decides what to run,
and where the honest gaps are. If the README is "what it does," this is "how, and
what specifically it's reading."

---

## 1. Architecture at a glance

```
User query
   │
   ▼
orchestrator.py (LangGraph StateGraph)
   │
   ├─ parse_query        → SiteSelectionQuery (business_type, priority_weights, hard_constraints)
   ├─ check_clarity       → asks a clarifying question and stops, if business_type is nonsense
   ├─ decide_agents        → which of the 5 tools apply to this query (default: all 5)
   ├─ run_subagents        → calls the 5 tools below, per candidate
   ├─ weight reallocation  → per candidate, redistributes weight away from missing data
   └─ synthesize            → synthesis.py: ranks, writes reasons, self-checks citations
   │
   ▼
RankedReport (top 3, each with a cited rationale + data_gaps)
```

Everything downstream of `run_subagents` operates on **structured JSON only** —
the synthesis model never sees a raw API response, and never calls a tool itself.
This is deliberate: it's the boundary that makes the citation validator possible
(§5 below) — every number the model is allowed to write down has to trace back to
a field in that JSON.

---

## 2. The five agents

### 2.1 `get_isochrone` — physical accessibility

**File:** `tools/isochrone.py`

**What it answers:** "how much area (and by extension, how many people) can
reach this exact point within N minutes by car or on foot?"

**Data source:** [OpenRouteService](https://openrouteservice.org/) (ORS) — in
one of two modes, chosen via `ORS_MODE`:

- **`api` (default)** — the real public ORS API (`api.openrouteservice.org`),
  authenticated with a free key (`ORS_API_KEY`, header `Authorization: <key>`,
  no "Bearer" prefix). Covers any city worldwide immediately, no per-city setup.
- **`docker`** — a self-hosted ORS instance per onboarded city, each on its own
  port (`cities.json` tracks which). Only works for cities `onboard_city.py`
  has actually built a routing graph for.

**Exact call:** `POST {base_url}/v2/isochrones/{driving-car|foot-walking}` with
`{"locations": [[lon, lat]], "range": [minutes*60], "range_type": "time"}`.
ORS returns a GeoJSON polygon — the actual drivable/walkable reachable area,
routed over real streets, not a circle.

**Real, verified rate limits for the public API:** rather than trust
third-party docs (which turned out inconsistent, and ORS's own plans page is
behind client-side JS a fetcher can't read), the actual limit was confirmed by
making a real request and reading the API's own `x-ratelimit-*` response
headers: **500 isochrone requests/day**, on a rolling 24-hour window (the
`x-ratelimit-reset` timestamp moved by exactly 86,400 seconds between two
consecutive calls). There's no per-minute figure in those headers, so
`tools/isochrone.py` applies a conservative, community-reported 20-requests/
minute client-side throttle as a safety margin — not a guarantee, since ORS
doesn't expose that number directly. The module tracks `quota_status` (limit /
remaining / reset) from the live headers on every call, and the Streamlit UI
displays it once at least one request has been made — real observed quota,
not an estimate.

**How the access score is computed:** the polygon's area is calculated in square
miles (via `shapely`, with a flat-earth approximation — accurate enough at
city scale), then normalized against a nominal 50-sq-mi reference (roughly what
a 10-minute drive isochrone spans in a mid-density US city) and clamped to 0–100.
This is explicitly a placeholder for true population-weighted access scoring —
see §7.

**Failure modes handled:** ORS down/unreachable/rate-limited, no API key set in
`api` mode, city not onboarded in `docker` mode, candidate outside a loaded map
extract's bounds, malformed response — all collapse to `status: "failed"`,
empty `catchment_geojson`, `access_score: 0.0`. No fabricated catchment.

---

### 2.2 `get_census_profile` — who lives nearby

**File:** `tools/census.py`

**What it answers:** median household income, median age, population density,
and how that income compares to the surrounding city's own median.

**Data source:** the real [US Census Bureau Geocoder](https://geocoding.geo.census.gov/)
and [ACS 5-Year API](https://www.census.gov/data/developers/data-sets/acs-5year.html)
— nationwide, live government data, not a local extract.

**Exact call sequence:**
1. `GET .../geocoder/geographies/coordinates` with `layers=Census Tracts,Incorporated
   Places` — one call, two answers: which census tract the point falls in (for
   demographics) *and* which incorporated city it's in (for the comparison below).
   This is a real geocode, not a lookup table — it works for literally any US
   coordinate.
2. `GET .../acs/acs5?get=B19013_001E,B01002_001E,B01003_001E&for=tract:...` —
   pulls median household income (B19013_001E), median age (B01002_001E), and
   total population (B01003_001E) for that exact tract.
3. Population density is computed as population ÷ the tract's land area
   (`AREALAND`, already returned by the geocoder, converted from sq meters).
4. **The city-comparison field is fully automatic, not hardcoded.** Step 1's
   "Incorporated Places" result gives the candidate's actual city and its own
   Census place FIPS code. That FIPS code is used to pull *that specific city's*
   own median household income (same ACS endpoint, `for=place:...`), and
   `vs_city_median_income_pct` is the candidate tract's income relative to that.
   A point outside any incorporated place (unincorporated county land)
   legitimately gets `None` here, not a guess.

   *This replaced an earlier version that defaulted the comparison city to
   Austin regardless of where the candidate actually was — caught by testing a
   Manhattan address and finding its income compared against Austin's median.
   See the README for that story.*

**Failure modes handled:** geocode returns no match (e.g. non-US coordinates),
ACS fields come back as Census's own "not available" sentinel (`-666666666`,
mapped to `None`) — both collapse to `status: "failed"` or a partial result
with the specific missing fields set to `None`, never a fabricated number.

---

### 2.3 `get_poi_density` — competitor density

**File:** `tools/poi_density.py`

**What it answers:** how many same-category businesses are already in the
catchment, and how far away the three nearest ones are.

**Data source:** [OpenStreetMap](https://www.openstreetmap.org/) via the public
Overpass API — global coverage, no API key, but rate-limited, so this tool
caches per-query results and retries with backoff (5s / 15s / 30s) on HTTP 429.

**Category → OSM tag mapping:** `business_type` (free text like "coffee shop")
is mapped to real OSM tags via a small lookup table (`_CATEGORY_OSM_FILTERS`) —
e.g. "coffee shop" → `amenity=cafe` OR `shop=coffee`; "fast-casual restaurant"
→ `amenity=restaurant` OR `amenity=fast_food`. An unmapped category returns
`status: "failed"` rather than guessing a tag.

**Search radius:** computed from the *actual* isochrone catchment polygon
(candidate → farthest point on the catchment boundary, +1 mile buffer to catch
competitors just outside it) — not a fixed radius. This means `get_poi_density`
depends on `get_isochrone` having already run for that candidate; if isochrone
failed, there's no catchment to search and this tool fails too (a real,
intentional coupling, not a bug — see the geo-stress-test findings for what
this looks like against out-of-coverage cities).

**What it does *not* compute:** `saturation_score` (competitors per 10k
residents) is always `None`. Doing this correctly needs a population estimate
for the catchment, which this tool doesn't receive as input — that's a Phase 2
placeholder still open (see §7). Because of this, `status` is `"degraded"`
rather than `"ok"` even when the count and nearest-three data are good, which
is also why the weight-reallocation logic (§4) treats "competition" as
partially unavailable more often than it strictly needs to.

---

### 2.4 `get_labor_profile` — staffing cost & availability

**File:** `tools/labor.py`

**What it answers:** wage percentiles (25th/median/75th) for the relevant
occupation, and the local unemployment rate.

**Data source:** two live [Bureau of Labor Statistics](https://www.bls.gov/developers/)
series — OEWS (Occupational Employment and Wage Statistics) for wages, LAUS
(Local Area Unemployment Statistics) for unemployment.

**Fully automatic geography — no per-city setup:**
`GET .../geocoder/geographies/coordinates` with `layers=Census Tracts,Metropolitan
Statistical Areas` returns both the county FIPS (from the tract) and the CBSA
(metro area) code in one call, for any US location. Neither needs to be
registered anywhere — this works identically for a brand-new onboarded city as
it does for Austin.

**Occupation inference:** `business_type` free text is matched against a small
keyword table (`infer_occupation_code`) to the right SOC occupation code — e.g.
"gym" → 39-9031 (Fitness Trainers), "bar" → 35-3011 (Bartenders), "grocery" /
"retail" → 41-2031 (Retail Salespersons), falling back to Retail Salespersons
generically if nothing matches. An explicit `occupation_code` argument
overrides this when the caller already knows it.

**Exact series IDs constructed:**
- Wages: `OEUM{7-digit CBSA}000000{6-digit SOC}{datatype}` for the 25th
  percentile, median, and 75th percentile annual wage (datatype codes 12/13/14).
- Unemployment: `LAUCN{5-digit county FIPS}{8 zeros}03`.

Both formats were verified against BLS's own documented field layout (not
guessed) after an early attempt with a transposed digit returned "series does
not exist" — see the README's list of caught bugs.

**Failure modes handled:** candidate outside any US metro area, BLS API error,
a metro/occupation combination BLS doesn't publish data for — all collapse to
`status: "failed"`.

---

### 2.5 `get_zoning_risk` — permit risk

**File:** `tools/zoning.py`

**What it answers:** is this a low/medium/high-risk zoning district for a
retail/food-service concept, or genuinely unknown.

**Data source:** a real municipal GIS layer — for Austin specifically, the
city's ArcGIS `FeatureServer` (`PLANNINGCADASTRE_zoning_small_map_scale`),
queried by point-in-polygon. **This is the one agent that is not automatically
available for a new city** — there's no general "give me any city's zoning
map" API, so every other city onboarded via `onboard_city.py` gets
`zoning_coverage: false` and this tool correctly returns `no_coverage` for it.

**Exact call:** `GET .../FeatureServer/0/query` with the candidate's lon/lat as
an `esriGeometryPoint`, `spatialRel=esriSpatialRelIntersects`, requesting the
`ZONING_BASE` field. Address text is first geocoded to a point via the Census
Geocoder's `onelineaddress` endpoint.

**The buffer fallback (a real edge case, not hypothetical):** street-address
geocoding often lands the point on the road centerline, which sits in the gap
*between* two zoning polygons rather than inside either one. If the exact-point
query comes back empty, the tool retries with a 20-meter buffer and picks the
*nearest* polygon (by actual geometric distance, via `shapely`) rather than an
arbitrary one from the result set — this was found and fixed by testing a real
address (Franklin Barbecue's block on E 11th St) that legitimately hit this gap.

**Risk classification:** a base zoning district code (e.g. `CS`, `SF`, `PUD`)
maps to low/medium/high risk via a hand-built table (`_ZONING_RISK`) reflecting
Austin's Land Development Code Ch. 25-2 — commercial districts are low risk,
residential/industrial are high risk, office-adjacent districts are medium.
Codes that are genuinely case-by-case (`PUD` — a custom negotiated ordinance;
`UNZ` — outside city jurisdiction entirely) are **not** force-fit into this
table. Instead, a small ChromaDB collection holds a factual explanation of why
each is case-by-case, retrieved and returned as the citation, with
`risk_level: "unknown"` rather than a guessed score.

**Status semantics, precisely:**
- `no_coverage` — the point geocoded fine, but there's no zoning polygon there
  (either genuinely outside city zoning jurisdiction, or the city isn't onboarded).
- `failed` — the address couldn't even be geocoded, or the API call errored.

These are deliberately different signals; conflating them was one of the
adversarial-suite test cases (§6.4) — a Houston address must return
`no_coverage`, never a value inferred from Austin's own zoning rules.

---

## 3. The orchestrator (`orchestrator.py`)

A LangGraph `StateGraph` with these nodes, in order:

1. **`parse_query`** — an LLM call that turns free text into a `SiteSelectionQuery`
   (business_type, candidate_region, priority_weights summing to 1.0,
   hard_constraints). On a follow-up turn, the previous turn's parsed query is
   included as context, so "now weight competition more" doesn't lose the
   original business_type and region.
2. **`check_clarity`** — a second, narrow LLM call: is `business_type` an actual
   kind of business someone could research, or nonsense/too vague? If unclear,
   the graph routes straight to `end_clarification` and returns the clarifying
   question — no tools are called, no synthesis happens.
3. **`decide_agents`** — an LLM call that decides which of the 5 tools this
   query actually needs, defaulting to all five, logging a one-sentence reason
   for any skip (only ever exercised for genuinely unstaffed concepts so far).
4. **`run_subagents`** — deterministic Python, not an LLM call: for each
   candidate, calls whichever of the 5 tools were selected, in sequence (with a
   2-second pause between candidates to stay a good citizen on the public
   Overpass API).
5. **`synthesize`** — see §4 and §5 below. First checks whether *every* called
   tool failed for *every* candidate; if so, returns an explicit "insufficient
   data" report without spending an LLM call on it at all. Otherwise hands off
   to the Synthesis Agent.

**Session-level caching:** if a follow-up query changes only `priority_weights`
(same candidates, same region, same hard_constraints), the graph skips
`decide_agents` and `run_subagents` entirely and reuses the previous turn's raw
tool outputs — only `parse_query` and `synthesize` re-run. This is a real
latency difference, not just a formality: a full run against 3 candidates
takes ~50s; a cached weight-only follow-up takes ~20s (two LLM calls only).

---

## 4. Weight reallocation — how missing data affects scoring

**File:** `weight_reallocation.py`

Before synthesis, each candidate gets its own **adjusted** priority-weight
split, computed from *that candidate's* actual data availability:

```
for each dimension (access / demographics / competition / labor / zoning):
    if that candidate's corresponding tool returned status == "ok":
        keep its stated weight
    else:
        fold its weight proportionally into the dimensions that ARE available
```

Example: if `priority_weights = {access: 0.25, demographics: 0.25,
competition: 0.2, labor: 0.15, zoning: 0.15}` and a candidate's zoning came
back `no_coverage` and competition came back `degraded` (see §2.3), the
remaining 0.65 of weight (access + demographics + labor) gets scaled up to
cover the full 1.0 — access and demographics each end up around 0.38, labor
around 0.23 — rather than the candidate just being scored as if 35% of its
picture were blank.

The Synthesis Agent is explicitly told to score against each candidate's
`adjusted_priority_weights`, not the raw stated ones, while still logging a
`data_gaps` entry for anything missing — so the user sees both the honest
gap *and* a ranking that isn't artificially penalized by it.

**A known simplification:** because `get_poi_density` almost always returns
`status: "degraded"` (missing `saturation_score`, see §2.3) rather than `"ok"`,
the competition dimension gets reallocated away more often than is strictly
fair — the count and nearest-competitor data it *does* have is still passed
to the model and used in the rationale, just not counted as "fully available"
for reallocation purposes. Tightening this to a partial-credit model instead
of a binary ok/not-ok cutoff is a reasonable next improvement.

---

## 5. Synthesis and the citation validator

**Files:** `synthesis.py`, `citation_validator.py`

The Synthesis Agent is a single structured-output LLM call (`ChatAnthropic(...)
.with_structured_output(RankedReport)`) that receives *only* the JSON produced
by the tools — never calls a tool itself, never sees a raw API response.

**The hard rule, enforced in code, not just prompted:** after synthesis
produces a `RankedReport`, `citation_validator.py` extracts every number
(`\$?\d[\d,]*\.?\d*%?`) from each candidate's rationale and checks it against
every numeric value present in that candidate's source JSON (with a small
rounding tolerance). Any unsupported number triggers a retry — the model is
shown exactly which numbers didn't check out and told to rewrite using only
verified figures. This happens up to 3 times.

**If it still fails after 3 tries**, the rationale is replaced with an explicit
"manual review required" message and a `data_gaps` entry — the system ships an
honest non-answer rather than an unverified one. This path has actually fired
in testing (not just as a designed contingency) — see the README.

**A second, independent safeguard:** the Synthesis Agent is explicitly told
never to rank more candidates than it was actually given, after testing with
only 2 candidates surfaced it duplicating one to fill a 3-slot ranking.

---

## 6. Evaluation (Phase 2 — see also the golden dataset and scripts in `scripts/`)

Briefly, since the README covers the headline numbers: Recall@3 (7/8) was
measured against 8 real, independently-sourced Austin business openings
(source URLs in `scripts/golden_dataset.csv`), each with a mix of real
distractor candidates rather than the true answer being trivially the only
option. Tool-selection precision/recall, an LLM-as-judge rubric (grounded on
evidence, logical consistency, tradeoff clarity, actionability), a regression
suite of 15 synthetic queries, and a 4-case adversarial suite (nonsense query,
zero zoning coverage, ORS actually stopped mid-test, all agents mocked to
fail) are all implemented in `scripts/` with real, re-runnable results —
nothing there is a mocked or simulated number.

### 6.1 The geographic stress test (Manhattan, Ithaca, Bangalore, Chennai)

Run directly against the 5 tools (not through the LLM orchestrator, to isolate
tool-level behavior) for locations the system was never built for:

| Location | isochrone | census | labor | zoning |
|---|---|---|---|---|
| Manhattan, NY | `failed` (no ORS graph loaded) | `ok` (real NYC data) | `ok` (real BLS data) | `no_coverage` |
| Ithaca, NY | `failed` | `ok` | `ok` | `no_coverage` |
| Bangalore, India | `failed` | `failed` (Census is US-only) | `failed` | `failed` (couldn't even geocode) |
| Chennai, India | `failed` | `failed` | `failed` | `failed` |

Every failure here is the *correct* answer for a system that hasn't been
onboarded for that location — no tool fabricated a plausible-looking number
for a place it has no business answering about. Note Bangalore/Chennai's
zoning shows `failed`, not `no_coverage` — because the geocode step itself
came back empty for a non-US address, a genuinely different failure than
"geocoded fine, no polygon there" (see §2.5's status semantics).

---

## 7. Known simplifications and honest gaps

Collected in one place, rather than scattered as caveats:

- **`population_weighted_access_score` (§2.1) is a placeholder.** It's a
  reachable-area ratio, not a true population-weighted score — that needs a
  gridded population raster (e.g. GHS-POP) overlaid on the isochrone polygon,
  which isn't wired in.
- **`saturation_score` (§2.3) is never computed.** Needs a population estimate
  for the catchment that `get_poi_density` doesn't currently receive.
- **Zoning coverage is manual, per-city, and currently just Austin.** See §2.5
  and the multi-city section of the README.
- **Hard constraints are judged, not verified.** None of the 5 tools return
  commercial rent/lease data, so "rent under $8,000/mo" is taken on the
  model's judgment during synthesis, not checked against a real listing.
- **No historical data.** Every source is live/current-day. The Recall@3 eval
  (§6) measures "does today's data support the historical decision," not "what
  would the system have said before the decision was made" — a real, stated
  limitation of the eval, not hidden.
- **Onboarding a new city only automates 4 of 5 tools.** `onboard_city.py`
  gets routing (via BBBike's ~240-city catalog, or a larger Geofabrik regional
  extract as a fallback) working automatically; census, labor, and competitor
  density already work automatically with zero setup; zoning does not, and
  isn't expected to without someone finding and wiring in that city's specific
  GIS source.
- **The public ORS API's 500/day quota is per key, not per user.** If this is
  ever deployed for multiple simultaneous people to share, they share that one
  quota. Fine for individual/local use; a real multi-user deployment would
  need either the Docker mode (unlimited, but per-city setup and more compute)
  or a paid ORS plan with a higher cap.
- **Uploaded/pasted credentials aren't encrypted.** `credentials.py`'s "save to
  .env" option writes plaintext to disk, the same as hand-editing the file —
  standard for local API-key storage, but worth knowing before this runs
  anywhere shared or multi-tenant.

## 8. Setup & credentials (`credentials.py`, the sidebar's "🔑 API keys & routing" panel)

Three ways to supply the four possible keys (Census, BLS, Anthropic required;
ORS optional depending on routing mode):

1. **Paste directly** into the sidebar — applied to `os.environ` for that
   session immediately.
2. **Upload a file** — `.env` (dotenv: `KEY=VALUE` per line, `#` comments) or
   flat `.json` (`{"KEY": "VALUE"}`). Either format works; only the four
   recognized key names are ever read out of it, everything else in the file
   is ignored. A "save to .env" checkbox controls whether this persists past
   the current session (existing keys in `.env` are preserved, not clobbered).
3. **Edit `.env` directly** — the traditional path, still fully supported;
   the UI is additive, not a replacement for it.

The routing-mode switch in the same panel just sets `ORS_MODE` in the
environment (`"api"` or `"docker"`) — `tools/isochrone.py` reads it fresh on
every call, so switching modes mid-session takes effect on the next query,
no restart needed.
