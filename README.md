# Site Selection Copilot

You ask it something like *"best spot for a fast-casual restaurant near East Austin, budget-conscious"* — it looks up real data about a handful of candidate addresses (how easy they are to reach, who lives nearby, how many competitors are already there, what it costs to staff the place, and how risky the zoning is), then writes you a ranked top-3 with a plain-English reason for each pick. Every number in that reason is checked against the real data before you see it — if it can't be verified, it gets pulled rather than shown.

Right now it only really knows Austin, TX. Ask it about Manhattan or Bangalore and it will tell you honestly what it doesn't know, instead of guessing.

## What it's made of

Five small tools, each pulling from one real, public data source:

| Tool | What it answers | Where the data comes from |
|---|---|---|
| `get_isochrone` | "How far can someone actually drive to reach this in 10 minutes?" | OpenRouteService (self-hosted, Austin map data) |
| `get_census_profile` | "Who lives in that area — income, age, density?" | US Census Bureau (ACS 5-year survey) |
| `get_poi_density` | "How many competitors are already there?" | OpenStreetMap |
| `get_labor_profile` | "What would it cost to staff this, and is unemployment high or low here?" | Bureau of Labor Statistics |
| `get_zoning_risk` | "Am I even allowed to open a restaurant/shop here?" | Austin's city zoning map + a small reference lookup for tricky cases |

An orchestrator (built with LangGraph) decides which of these five to call for a given question, runs them, and hands everything to a synthesis step that writes the final ranked recommendation — and is not allowed to make up a number that isn't in the data it was given.

## Running it

```bash
docker compose up -d          # starts the local routing engine (one-time: needs the Austin map loaded first)
source .venv/bin/activate
python orchestrator.py        # runs a sample two-turn conversation
```

You'll need three free API keys in a `.env` file — Census, BLS, and Anthropic — see `.env` for the format.

## Where things stand

- **Phase 0–1** (the five data tools + the orchestrator that ties them together): done, tested against real Austin addresses.
- **Phase 2** (does it actually work well?): done. Ran it against 8 real, recent Austin restaurant/retail openings and checked whether it would've picked the real winning spot — it did, 7 out of 8 times. Also stress-tested it against places it was never built for (Manhattan, Ithaca, Bangalore, Chennai) to make sure it fails *honestly* rather than making things up — it does.
- **Phase 3** (more cities, a nicer demo, deploying it somewhere public): not started.

## Honest limitations

- **Austin only, really.** The routing engine and zoning lookup only have Austin data loaded. Census and labor data are technically nationwide, but nothing else is, so results outside Austin are mostly "I don't know."
- **No memory of the past.** All the data is *live* — if you ask "where would this have opened in 2023," it can't rewind. It only knows today.
- **It ranks candidates you give it, not the whole city.** There's no "scan all of Austin" feature yet — you tell it which addresses to consider, and it ranks those.
