import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas import CandidateSite
from tools.census import get_census_profile
from tools.isochrone import get_isochrone

CANDIDATES = [
    CandidateSite(address="1100 Congress Ave, Austin, TX 78701", lat=30.2747, lon=-97.7404),   # Texas Capitol
    CandidateSite(address="900 E 11th St, Austin, TX 78702", lat=30.2701, lon=-97.7313),        # Franklin Barbecue / East Austin
    CandidateSite(address="3110 Esperanza Crossing, Austin, TX 78758", lat=30.4013, lon=-97.7226),  # The Domain
]

for candidate in CANDIDATES:
    print(f"\n=== {candidate.address} ===")

    iso = get_isochrone(candidate, mode="drive", minutes=10)
    print("isochrone:", json.dumps(iso.model_dump(exclude={"catchment_geojson"}), indent=2))

    demo = get_census_profile(candidate)
    print("census:", json.dumps(demo.model_dump(exclude={"candidate"}), indent=2))
