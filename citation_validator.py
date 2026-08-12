"""Checks that every number in a synthesis rationale traces back to the
source sub-agent JSON for that candidate, per the build spec's hard rule
(§4.2): a rationale may not contain a quantitative claim that doesn't appear
verbatim (within a small rounding tolerance) in its input data.
"""

import re

_NUMBER_RE = re.compile(r"-?\$?\d[\d,]*\.?\d*%?")


def extract_numbers(text: str) -> list[float]:
    numbers = []
    for match in _NUMBER_RE.finditer(text):
        cleaned = match.group().replace("$", "").replace(",", "").replace("%", "")
        try:
            numbers.append(float(cleaned))
        except ValueError:
            continue
    return numbers


def collect_source_numbers(source: dict) -> set[float]:
    numbers = set()

    def walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
        elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
            numbers.add(round(float(obj), 2))

    walk(source)
    return numbers


def validate_rationale(rationale: str, source: dict, tolerance: float = 0.05) -> tuple[bool, list[float]]:
    """Returns (is_valid, unsupported_numbers). tolerance allows exact-value
    float rounding noise; a wider +/-1 band also covers a human rounding a
    stated figure (e.g. writing "20% below" for a source value of -19.8)."""
    source_numbers = collect_source_numbers(source)
    unsupported = [
        n
        for n in extract_numbers(rationale)
        if not any(abs(n - s) <= tolerance for s in source_numbers)
        and not any(abs(round(n) - round(s)) <= 1 for s in source_numbers)
    ]
    return len(unsupported) == 0, unsupported
