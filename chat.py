"""Interactive CLI for trying the Site Selection Copilot yourself.

Usage:
    python chat.py

You'll be asked for a few candidate addresses first (the system ranks a
list you give it -- it doesn't scan a whole city on its own yet), then you
can ask it questions in plain English. Type 'quit' to exit.
"""

import sys

from schemas import CandidateSite
from tools.zoning import _geocode
from orchestrator import Session

DEFAULT_CANDIDATES = [
    "1100 Congress Ave, Austin, TX 78701",
    "900 E 11th St, Austin, TX 78702",
    "3110 Esperanza Crossing, Austin, TX 78758",
    "1600 S Congress Ave, Austin, TX 78704",
]


def collect_candidates() -> list[CandidateSite]:
    print("Enter candidate addresses one per line (blank line to use defaults, 'done' to finish):")
    addresses = []
    while True:
        line = input("  address> ").strip()
        if not line:
            if not addresses:
                addresses = DEFAULT_CANDIDATES
                print(f"  using {len(addresses)} default Austin addresses")
            break
        if line.lower() == "done":
            break
        addresses.append(line)

    candidates = []
    for addr in addresses:
        print(f"  geocoding: {addr} ...", end=" ", flush=True)
        latlon = _geocode(addr)
        if latlon is None:
            print("FAILED (couldn't geocode -- skipping)")
            continue
        lat, lon = latlon
        candidates.append(CandidateSite(address=addr, lat=lat, lon=lon))
        print(f"ok ({lat:.4f}, {lon:.4f})")
    return candidates


def print_report(report):
    print(f"\nagents_called:  {report.agents_called}")
    if report.agents_skipped:
        print(f"agents_skipped: {report.agents_skipped}")
    print(f"\n{'='*70}")
    if not report.top_candidates:
        print(report.top_tradeoff)
    for rc in report.top_candidates:
        print(f"\n#{rc.rank}  {rc.candidate.address}")
        print(f"    {rc.rationale}")
        if rc.data_gaps:
            print(f"    data_gaps: {rc.data_gaps}")
    if report.top_tradeoff and report.top_candidates:
        print(f"\ntradeoff: {report.top_tradeoff}")
    print(f"{'='*70}\n")


def main():
    print("=== Site Selection Copilot ===\n")
    candidates = collect_candidates()
    if not candidates:
        print("No candidates geocoded successfully -- exiting.")
        sys.exit(1)

    print(f"\nReady. Considering {len(candidates)} candidates:")
    for c in candidates:
        print(f"  - {c.address}")

    print("\nAsk it something, e.g. 'Best spot for a fast-casual restaurant, budget-conscious.'")
    print("Type 'quit' to exit.\n")

    session = Session()
    while True:
        try:
            query = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye!")
            break
        if not query:
            continue
        if query.lower() in ("quit", "exit"):
            print("bye!")
            break

        result = session.run(query, candidates)
        if result.clarification_needed:
            print(f"\nCopilot: {result.clarification_needed}\n")
        else:
            print_report(result.report)


if __name__ == "__main__":
    main()
