"""Registry of onboarded cities -- the one piece of state that ties together
which cities have a working routing graph (get_isochrone) and/or real zoning
coverage (get_zoning_risk). Census, labor, and competitor-density data need
no registration at all; they're derived live from candidate coordinates for
any location on Earth (competitor density) or in the US (census/labor).

cities.json schema per city:
    display_name    "Austin, TX"
    ors_port        8080              -- this city's dedicated ORS container port
    ors_container   "site-selection-ors-austin"
    osm_extract     "Austin"          -- BBBike catalog name, or a Geofabrik path
    zoning_coverage bool              -- true only if a real GIS layer is wired in
    onboarded_at    "2026-08-11"
"""

import json
import os

_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "cities.json")


def _load() -> dict:
    with open(_REGISTRY_PATH) as f:
        return json.load(f)


def _save(registry: dict) -> None:
    with open(_REGISTRY_PATH, "w") as f:
        json.dump(registry, f, indent=2)
        f.write("\n")


def list_cities() -> list[str]:
    return list(_load().keys())


def get_city(name: str) -> dict | None:
    return _load().get(name)


def default_city() -> str:
    registry = _load()
    return "Austin" if "Austin" in registry else next(iter(registry), "Austin")


def register_city(name: str, **fields) -> None:
    registry = _load()
    registry[name] = fields
    _save(registry)
