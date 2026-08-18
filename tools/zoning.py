import os

import chromadb
import httpx
from shapely.geometry import Point, shape

from schemas import CandidateSite, ZoningResult
from tracing_setup import traced

GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
ZONING_QUERY_URL = (
    "https://services.arcgis.com/0L95CJ0VTaxqcmED/ArcGIS/rest/services/"
    "PLANNINGCADASTRE_zoning_small_map_scale/FeatureServer/0/query"
)

# Base district -> risk for a generic retail/food-service concept. This is a
# Phase 1 approximation of Austin LDC Ch. 25-2, Subch. C, Art. 2 (Base Zoning
# Districts), not a verbatim reading of the current use tables — validate
# against the live code before treating any single result as final.
_ZONING_RISK: dict[str, str] = {
    # commercial districts where retail/restaurant is generally permitted by-right
    "CS": "low", "CH": "low", "CBD": "low", "DMU": "low", "GR": "low", "LR": "low", "TOD": "low",
    # office-oriented or special mixed-use districts: retail/restaurant is typically conditional
    "GO": "medium", "NO": "medium", "LO": "medium", "CR": "medium",
    "TND": "medium", "NBG": "medium", "ERC": "medium",
    # residential, industrial, agricultural, aviation, or reserve districts: not a principal permitted use
    "SF": "high", "MF": "high", "MH": "high", "RR": "high", "LA": "high", "AG": "high",
    "P": "high", "DR": "high", "LI": "high", "MI": "high", "R&D": "high", "IP": "high",
    "W/LO": "high", "AV": "high",
}

# Base districts that are genuinely case-by-case and can't be reduced to a
# fixed risk level — surfaced via retrieval instead of guessed.
_AMBIGUOUS_DOCS = {
    "PUD": (
        "A Planned Unit Development (PUD) is a custom base district approved through an "
        "individually negotiated ordinance rather than a standard use table. Permitted uses, "
        "density, and site requirements are defined case-by-case in that specific PUD's ordinance "
        "and site plan, so no general retail/restaurant permission can be inferred from the PUD "
        "label alone -- the specific ordinance for this property must be pulled and reviewed."
    ),
    "UNZ": (
        "Unzoned (UNZ) property lies outside the city's zoning jurisdiction -- often state, "
        "federal, or county land, or land recently annexed and not yet assigned a zoning district. "
        "Standard municipal use-permission rules do not apply; any use restrictions come from the "
        "owning jurisdiction rather than the city zoning code."
    ),
}

_CHROMA_PATH = os.path.join(os.path.dirname(__file__), "..", "chroma_data")
_client = chromadb.PersistentClient(path=_CHROMA_PATH)
_collection = _client.get_or_create_collection("zoning_ambiguous_cases")
if _collection.count() == 0:
    _collection.add(ids=list(_AMBIGUOUS_DOCS.keys()), documents=list(_AMBIGUOUS_DOCS.values()))


def _query_zoning_base(lat: float, lon: float) -> str | None:
    resp = httpx.get(
        ZONING_QUERY_URL,
        params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "ZONING_BASE",
            "returnGeometry": "false",
            "f": "json",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if features:
        return features[0]["attributes"]["ZONING_BASE"]

    # Street-address geocoding often lands on the road centerline, which sits
    # in the gap between abutting zoning polygons rather than inside one.
    # Retry with a small buffer and take the nearest polygon instead of
    # guessing -- this is a real nearby parcel, not an inferred value.
    resp = httpx.get(
        ZONING_QUERY_URL,
        params={
            "geometry": f"{lon},{lat}",
            "geometryType": "esriGeometryPoint",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "distance": 20,
            "units": "esriSRUnit_Meter",
            "outFields": "ZONING_BASE",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "json",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    features = resp.json().get("features", [])
    if not features:
        return None

    point = Point(lon, lat)
    nearest = min(
        features,
        key=lambda f: shape({"type": "Polygon", "coordinates": f["geometry"]["rings"]}).distance(point),
    )
    return nearest["attributes"]["ZONING_BASE"]


def _geocode(address: str) -> tuple[float, float] | None:
    resp = httpx.get(
        GEOCODER_URL,
        params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
        timeout=15.0,
    )
    resp.raise_for_status()
    matches = resp.json()["result"]["addressMatches"]
    if not matches:
        return None
    coords = matches[0]["coordinates"]
    return coords["y"], coords["x"]  # lat, lon


@traced("get_zoning_risk")
def get_zoning_risk(candidate: CandidateSite, address: str = None) -> ZoningResult:
    """Zoning risk for the candidate's own coordinates.

    Deliberately uses candidate.lat/lon rather than re-geocoding the address
    string. The other four sub-agents all evaluate candidate.lat/lon, so
    geocoding separately here could land on a slightly different point --
    meaning zoning was describing a different parcel than the demographics
    and competitor data for the same candidate. It also made this tool fail
    outright on any address the Census geocoder didn't recognise, even when
    the coordinates were known-good.

    `address` is accepted for call-site compatibility and is unused.
    """
    try:
        lat, lon = candidate.lat, candidate.lon

        base_code = _query_zoning_base(lat, lon)
        if base_code is None:
            # No zoning polygon at this point, even after buffering -- outside
            # Austin's zoning coverage. Never infer from another city's rules.
            return ZoningResult(candidate=candidate, risk_level="unknown", citation=None, status="no_coverage")

        risk = _ZONING_RISK.get(base_code)
        if risk is not None:
            return ZoningResult(
                candidate=candidate,
                risk_level=risk,
                citation=f"Austin LDC Ch. 25-2, Subch. C, Art. 2 -- base district {base_code}",
                status="ok",
            )

        if base_code in _AMBIGUOUS_DOCS:
            result = _collection.query(query_texts=[base_code], n_results=1)
            citation = result["documents"][0][0] if result["documents"] and result["documents"][0] else None
            return ZoningResult(candidate=candidate, risk_level="unknown", citation=citation, status="ok")

        return ZoningResult(candidate=candidate, risk_level="unknown", citation=None, status="ok")

    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return ZoningResult(candidate=candidate, risk_level="unknown", citation=None, status="failed")
