"""Parsing and applying user-supplied credentials -- either typed into the UI
or uploaded as a .env or JSON file. Two formats because .env (dotenv) is the
de facto standard for local API keys, and flat JSON is the common alternative
for anything that isn't shell-flavored.
"""

import json
import os

RECOGNIZED_KEYS = ["CENSUS_API_KEY", "BLS_API_KEY", "ANTHROPIC_API_KEY", "ORS_API_KEY", "ORS_MODE"]

_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


def parse_credentials_file(filename: str, content: bytes) -> dict:
    text = content.decode("utf-8", errors="ignore")
    if filename.endswith(".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        return {k: str(v) for k, v in data.items() if k in RECOGNIZED_KEYS}

    # .env / dotenv format: KEY=VALUE per line, '#' comments, optional quotes
    parsed = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in RECOGNIZED_KEYS:
            parsed[key] = value
    return parsed


def apply_credentials(creds: dict, persist: bool = False) -> None:
    for key, value in creds.items():
        os.environ[key] = value

    if not persist or not creds:
        return

    existing = {}
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH) as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.strip().partition("=")
                    existing[k] = v

    existing.update(creds)
    with open(_ENV_PATH, "w") as f:
        for k, v in existing.items():
            f.write(f"{k}={v}\n")
