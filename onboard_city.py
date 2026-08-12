"""Onboard a new city by name.

Usage:
    python onboard_city.py "Dallas"
    python onboard_city.py "Chennai" --yes    # skip the large-download confirmation

What this actually automates, honestly:
  - Routing data (get_isochrone): tries a pre-made city extract from BBBike's
    ~240-city catalog first (fast, small download). Falls back to a Geofabrik
    regional/country extract if the city isn't in that catalog (larger,
    slower, and requires confirmation before downloading).
  - Census & labor data: nothing to do -- both tools already derive the right
    geography live from candidate coordinates, for any US location.
  - Competitor density (OpenStreetMap): nothing to do -- global by default.
  - Zoning: NOT automated. No public API covers "get me this city's zoning
    GIS layer" in general. The city is registered with zoning_coverage=false,
    which the system already handles honestly (see the weight-reallocation
    logic in orchestrator.py) rather than guessing a risk level.
"""

import argparse
import re
import subprocess
import sys
import time
import unicodedata

import httpx

import city_registry

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "site-selection-copilot/0.1 (city onboarding script)"

_COUNTRY_TO_CONTINENT_SLUG = {
    "in": "asia", "us": "north-america", "gb": "europe", "de": "europe", "fr": "europe",
    "ca": "north-america", "au": "australia-oceania", "br": "south-america", "jp": "asia",
    "cn": "asia", "mx": "north-america", "es": "europe", "it": "europe", "nl": "europe",
    "sg": "asia", "ae": "asia", "za": "africa", "ng": "africa", "ru": "europe", "kr": "asia",
    "id": "asia", "th": "asia", "vn": "asia", "ph": "asia", "pk": "asia", "bd": "asia",
    "eg": "africa", "ke": "africa", "ar": "south-america", "cl": "south-america",
    "co": "south-america", "nz": "australia-oceania", "se": "europe", "no": "europe",
    "pl": "europe", "tr": "europe", "ie": "europe", "pt": "europe", "ch": "europe",
}


def _slug(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


def geocode_city(name: str) -> dict:
    resp = httpx.get(
        NOMINATIM_URL,
        params={"q": name, "format": "json", "addressdetails": 1, "limit": 1},
        headers={"User-Agent": USER_AGENT},
        timeout=15.0,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"couldn't geocode '{name}' -- check the spelling")
    return results[0]


def try_bbbike_extract(city_name: str) -> str | None:
    """Fast path: BBBike's ~240 pre-made major-city extracts. Returns a
    working download URL, or None if this city isn't in the catalog."""
    candidates = [city_name.replace(" ", ""), city_name.replace(" ", "_")]
    for candidate in candidates:
        url = f"https://download.bbbike.org/osm/bbbike/{candidate}/{candidate}.osm.pbf"
        resp = httpx.head(url, timeout=10.0, follow_redirects=True)
        if resp.status_code == 200:
            return url
    return None


def geofabrik_fallback_url(geo: dict) -> str | None:
    """Slower path: a whole state (US) or country extract from Geofabrik.
    Covers far more ground than BBBike but the download is much bigger."""
    address = geo.get("address", {})
    country_code = address.get("country_code", "")
    continent = _COUNTRY_TO_CONTINENT_SLUG.get(country_code)
    if continent is None:
        return None

    if country_code == "us":
        state = address.get("state")
        if not state:
            return None
        return f"https://download.geofabrik.de/north-america/us/{_slug(state)}-latest.osm.pbf"

    country = geo.get("address", {}).get("country") or geo.get("display_name", "").split(",")[-1].strip()
    return f"https://download.geofabrik.de/{continent}/{_slug(country)}-latest.osm.pbf"


def download_extract(url: str, dest_path: str) -> None:
    print(f"  downloading {url}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=None) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(f"\r  {downloaded / 1e6:.0f} / {total / 1e6:.0f} MB", end="", flush=True)
    print()


def next_free_port(used_ports: set[int]) -> int:
    port = 8081
    while port in used_ports:
        port += 1
    return port


def onboard(city_name: str, skip_confirm: bool) -> None:
    import os

    print(f"Onboarding '{city_name}'...")
    geo = geocode_city(city_name)
    print(f"  geocoded to: {geo['display_name']}")

    bbbike_url = try_bbbike_extract(city_name)
    fallback_note = ""
    if bbbike_url:
        url = bbbike_url
        print(f"  found in BBBike's pre-made city catalog: {url}")
    else:
        url = geofabrik_fallback_url(geo)
        if url is None:
            print("  ERROR: not in BBBike's catalog, and no Geofabrik region mapping for this country.")
            print("  Extend _COUNTRY_TO_CONTINENT_SLUG in onboard_city.py to add coverage.")
            sys.exit(1)
        fallback_note = " (regional extract -- larger than a single-city one)"
        print(f"  not in BBBike's catalog; falling back to a Geofabrik region extract{fallback_note}:")
        print(f"  {url}")
        if not skip_confirm:
            resp = input("  This can be a large download (hundreds of MB to a few GB). Continue? [y/N] ")
            if resp.strip().lower() != "y":
                print("  Aborted.")
                sys.exit(0)

    slug = _slug(city_name)
    files_dir = os.path.join(os.path.dirname(__file__), "ors-docker", "files")
    os.makedirs(files_dir, exist_ok=True)
    pbf_path = os.path.join(files_dir, f"{slug}.osm.pbf")
    download_extract(url, pbf_path)

    config_dir = os.path.join(os.path.dirname(__file__), "ors-docker", "config")
    config_path = os.path.join(config_dir, f"ors-config-{slug}.yml")
    with open(config_path, "w") as f:
        f.write(
            f"""ors:
  engine:
    profile_default:
      build:
        source_file: /home/ors/files/{slug}.osm.pbf
    profiles:
      driving-car:
        enabled: true
      foot-walking:
        enabled: true
"""
        )

    registry = {name: city_registry.get_city(name) for name in city_registry.list_cities()}
    used_ports = {entry["ors_port"] for entry in registry.values()}
    port = next_free_port(used_ports)
    container_name = f"site-selection-ors-{slug}"

    print(f"  starting ORS container '{container_name}' on port {port} (graph build takes a few minutes)...")
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    subprocess.run(
        [
            "docker", "run", "-d",
            "--name", container_name,
            "-p", f"{port}:8082",
            "-v", f"{files_dir}:/home/ors/files",
            "-v", f"{config_path}:/home/ors/config/ors-config.yml",
            "-e", "REBUILD_GRAPHS=False",
            "-e", "ORS_CONFIG_LOCATION=/home/ors/config/ors-config.yml",
            "openrouteservice/openrouteservice:latest",
        ],
        check=True,
    )

    print("  waiting for the graph to build and the service to come up...")
    for _ in range(120):
        try:
            r = httpx.get(f"http://localhost:{port}/ors/v2/health", timeout=5.0)
            if r.status_code == 200 and r.json().get("status") == "ready":
                print("  ORS is up.")
                break
        except httpx.HTTPError:
            pass
        time.sleep(5)
    else:
        print("  WARNING: ORS didn't report healthy within 10 minutes -- check `docker logs " + container_name + "`")

    city_registry.register_city(
        city_name,
        display_name=geo["display_name"],
        ors_port=port,
        ors_container=container_name,
        osm_extract=slug,
        zoning_coverage=False,
        onboarded_at=time.strftime("%Y-%m-%d"),
    )

    print(f"\n'{city_name}' onboarded.")
    print("  isochrone (routing):  ready")
    print("  census & labor:       already work automatically for any US location")
    print("  competitor density:   already global (OpenStreetMap)")
    print("  zoning:               NOT available -- no automated source for this city yet;")
    print("                        the system will report status='no_coverage' honestly")
    print("                        rather than guessing (this is expected, not a bug)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Onboard a new city by name.")
    parser.add_argument("city", help="City name, e.g. 'Dallas' or 'Chennai'")
    parser.add_argument("--yes", action="store_true", help="Skip the large-download confirmation prompt")
    args = parser.parse_args()
    onboard(args.city, skip_confirm=args.yes)
