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
import os
import re
import shutil
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


def remote_size_bytes(url: str) -> int | None:
    try:
        resp = httpx.head(url, follow_redirects=True, timeout=20.0)
        resp.raise_for_status()
        return int(resp.headers.get("content-length", 0)) or None
    except (httpx.HTTPError, ValueError):
        return None


def download_extract(url: str, dest_path: str, expected_bytes: int | None = None) -> None:
    """Downloads the extract, reusing an existing complete file.

    These downloads run to hundreds of megabytes, so re-fetching one because a
    later step failed is a genuinely painful waste -- a failed Houston attempt
    left 682MB on disk and the retry would have pulled it all again.
    """
    if expected_bytes and os.path.exists(dest_path):
        actual = os.path.getsize(dest_path)
        if actual == expected_bytes:
            print(f"  reusing already-downloaded extract ({actual / 1e6:.0f} MB) — skipping download")
            return
        print(f"  found a partial/stale extract ({actual / 1e6:.0f} MB of {expected_bytes / 1e6:.0f} MB), re-downloading")

    print(f"  downloading {url}")
    tmp_path = dest_path + ".part"
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=None) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            downloaded = 0
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        print(f"\r  {downloaded / 1e6:.0f} / {total / 1e6:.0f} MB", end="", flush=True)
        print()
        # Only promote to the real filename once complete, so an interrupted
        # download can never masquerade as a usable extract.
        os.replace(tmp_path, dest_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def next_free_port(used_ports: set[int]) -> int:
    port = 8081
    while port in used_ports:
        port += 1
    return port


def docker_memory_bytes() -> int | None:
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.MemTotal}}"], capture_output=True, text=True
    )
    try:
        return int(probe.stdout.strip())
    except (ValueError, AttributeError):
        return None


def plan_heap(pbf_bytes: int) -> tuple[int, str | None]:
    """Returns (heap_bytes, warning).

    The ORS image defaults to a 2GB heap, which silently OOMs on anything
    bigger than a city-sized extract -- a 715MB Texas extract died with
    java.lang.OutOfMemoryError after ~15 minutes of building, which surfaced
    only as "onboarding didn't work". Building a routing graph needs roughly
    7x the extract size in heap.
    """
    needed = max(2 * 1024**3, int(pbf_bytes * 7))
    available = docker_memory_bytes()
    if available is None:
        return needed, None

    usable = int(available * 0.75)  # leave headroom for the JVM's non-heap use
    if needed <= usable:
        return needed, None

    warning = (
        f"This extract ({pbf_bytes / 1e6:.0f} MB) wants about {needed / 1024**3:.1f} GB of heap to build,\n"
        f"     but Docker is only allocated {available / 1024**3:.1f} GB. The build will likely fail with an\n"
        f"     out-of-memory error. Either raise Docker Desktop's memory limit\n"
        f"     (Settings → Resources → Memory), or skip onboarding and use the public\n"
        f"     OpenRouteService API, which needs no local build at all."
    )
    return usable, warning


def container_hit_oom(container_name: str) -> bool:
    logs = subprocess.run(
        ["docker", "logs", "--tail", "200", container_name], capture_output=True, text=True
    )
    return "OutOfMemoryError" in (logs.stdout + logs.stderr)


def preflight(city_name: str) -> list[str]:
    """Everything that must be true before we spend time and bandwidth.

    Originally none of this was checked up front, so onboarding Houston
    downloaded 715MB and only then discovered Docker wasn't running -- and
    reported it as a raw CalledProcessError traceback.
    """
    problems = []

    if shutil.which("docker") is None:
        problems.append(
            "Docker isn't installed (or isn't on PATH). A local routing engine needs it.\n"
            "     Install Docker Desktop, or skip onboarding entirely and use the public\n"
            "     OpenRouteService API instead — that needs no Docker and covers any city."
        )
    else:
        probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
        if probe.returncode != 0:
            problems.append(
                "Docker is installed but the daemon isn't running.\n"
                "     Start Docker Desktop and wait for it to finish starting, then retry.\n"
                "     (Or use the public OpenRouteService API, which needs no Docker.)"
            )

    if city_registry.get_city(city_name) is not None:
        problems.append(
            f"'{city_name}' is already onboarded. Remove it from cities.json first if you\n"
            "     want to rebuild it from scratch."
        )

    return problems


def onboard(city_name: str, skip_confirm: bool) -> None:
    print(f"Onboarding '{city_name}'...")

    # Check everything cheap before anything expensive. A failure here costs
    # seconds; discovering the same problem after the download costs the
    # download.
    problems = preflight(city_name)
    if problems:
        print("\n  Can't onboard yet:\n")
        for p in problems:
            print(f"   • {p}\n")
        sys.exit(1)

    try:
        geo = geocode_city(city_name)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"\n  Couldn't look up '{city_name}': {exc}")
        print("  Check the spelling, or try a fuller name like 'Houston, TX'.")
        sys.exit(1)
    print(f"  geocoded to: {geo['display_name']}")

    bbbike_url = try_bbbike_extract(city_name)
    if bbbike_url:
        url = bbbike_url
        print(f"  found in BBBike's pre-made city catalog: {url}")
    else:
        url = geofabrik_fallback_url(geo)
        if url is None:
            print("\n  No map data source available for this location.")
            print(f"  '{city_name}' isn't in BBBike's ~240-city catalog, and there's no Geofabrik")
            print("  region mapping for its country yet (extend _COUNTRY_TO_CONTINENT_SLUG to add one).")
            print("  You can still analyse this city using the public OpenRouteService API — it needs")
            print("  no onboarding at all.")
            sys.exit(1)
        print(f"  not in BBBike's catalog; falling back to a Geofabrik regional extract:")
        print(f"  {url}")

    size = remote_size_bytes(url)
    if size:
        print(f"  extract size: {size / 1e6:.0f} MB")
    if not bbbike_url and not skip_confirm:
        prompt = f"  This is a whole-region extract ({size / 1e6:.0f} MB). Continue? [y/N] " if size \
            else "  This is a whole-region extract (can be several GB). Continue? [y/N] "
        if input(prompt).strip().lower() != "y":
            print("  Aborted.")
            sys.exit(0)

    slug = _slug(city_name)
    files_dir = os.path.join(os.path.dirname(__file__), "ors-docker", "files")
    os.makedirs(files_dir, exist_ok=True)
    pbf_path = os.path.join(files_dir, f"{slug}.osm.pbf")
    try:
        download_extract(url, pbf_path, expected_bytes=size)
    except httpx.HTTPError as exc:
        print(f"\n  Download failed: {exc}")
        print("  Check your connection and retry — a completed download is reused, not re-fetched.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n  Download cancelled; the partial file was cleaned up.")
        sys.exit(1)

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
    graphs_dir = os.path.join(os.path.dirname(__file__), "ors-docker", "graphs", slug)
    os.makedirs(graphs_dir, exist_ok=True)

    heap_bytes, heap_warning = plan_heap(os.path.getsize(pbf_path))
    heap_gb = max(2, heap_bytes // 1024**3)
    if heap_warning:
        print(f"\n  ⚠ {heap_warning}\n")
        if not skip_confirm and input("  Try anyway? [y/N] ").strip().lower() != "y":
            print("  Aborted.")
            sys.exit(0)

    print(f"  starting ORS container '{container_name}' on port {port} with a {heap_gb}GB heap")
    print("  (graph build takes a few minutes; large regional extracts take considerably longer)...")
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    started = subprocess.run(
        [
            "docker", "run", "-d",
            "--name", container_name,
            "-p", f"{port}:8082",
            "-v", f"{files_dir}:/home/ors/files",
            # Persist the built routing graph outside the container. Without
            # this, a graph build (minutes of CPU on a large extract) is thrown
            # away the moment the container is recreated.
            "-v", f"{graphs_dir}:/home/ors/graphs",
            "-v", f"{config_path}:/home/ors/config/ors-config.yml",
            "-e", "REBUILD_GRAPHS=False",
            "-e", "ORS_CONFIG_LOCATION=/home/ors/config/ors-config.yml",
            "-e", f"XMS={max(1, heap_bytes // (2 * 1024**3))}g",
            "-e", f"XMX={heap_gb}g",
            "openrouteservice/openrouteservice:latest",
        ],
        capture_output=True, text=True,
    )
    if started.returncode != 0:
        detail = (started.stderr or "").strip().splitlines()
        hint = detail[0] if detail else f"docker exited {started.returncode}"
        print(f"\n  Couldn't start the routing container: {hint}")
        if "port is already allocated" in (started.stderr or ""):
            print(f"  Port {port} is in use by something else. Free it, or remove a stale container")
            print("  with: docker ps -a | grep site-selection-ors")
        print(f"\n  The downloaded map data was kept at {pbf_path}")
        print("  so a retry won't need to download it again.")
        sys.exit(1)

    print("  waiting for the graph to build and the service to come up...")
    for tick in range(120):
        try:
            r = httpx.get(f"http://localhost:{port}/ors/v2/health", timeout=5.0)
            if r.status_code == 200 and r.json().get("status") == "ready":
                print("  ORS is up.")
                break
        except httpx.HTTPError:
            pass

        # Surface an out-of-memory build as soon as it happens. Waiting the full
        # ten minutes to report "didn't come up" hides the actual cause, which
        # is both specific and fixable.
        if tick % 6 == 5 and container_hit_oom(container_name):
            print(f"\n  The graph build ran out of memory.")
            print(f"  This extract needs more heap than Docker currently allows ({heap_gb}GB was used).")
            print("  Raise Docker Desktop's memory limit (Settings → Resources → Memory) and retry,")
            print("  or use the public OpenRouteService API, which needs no local build.")
            print(f"\n  '{city_name}' was NOT registered. The map data at {pbf_path} is kept for a retry.")
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            sys.exit(1)

        time.sleep(5)
    else:
        # Don't register a city whose routing engine never came up -- doing so
        # makes it selectable in the UI while every isochrone for it fails.
        # The container is left running because a large extract can legitimately
        # take longer than this to build, and killing it discards that work.
        print(f"\n  The routing engine didn't come up within 10 minutes.")
        print(f"  A large regional extract can genuinely take longer, so it may still be building.")
        print(f"  Check progress:  docker logs --tail 20 {container_name}")
        print(f"  Once it reports ready, re-run this command — the download and any completed")
        print(f"  graph build are reused, so it will pick up where it left off.")
        print(f"\n  '{city_name}' was NOT registered, so it won't appear as a usable city until then.")
        sys.exit(1)

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
