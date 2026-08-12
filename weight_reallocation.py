"""Redistributes a candidate's priority weight away from any dimension whose
sub-agent status isn't "ok" for that candidate, so a missing data point (a
failed API call, a city with no zoning coverage, ...) doesn't just silently
count against the candidate -- its weight gets folded proportionally into
the dimensions that actually have data, and the synthesis prompt is told to
score against that adjusted split instead of the raw stated one.
"""

_DIMENSION_TO_DATA_KEY = {
    "access": "isochrone",
    "demographics": "demographics",
    "competition": "competitors",
    "labor": "labor",
    "zoning": "zoning",
}


def reallocate_weights(priority_weights: dict[str, float], candidate_entry: dict) -> dict[str, float]:
    available = {}
    unavailable_weight = 0.0

    for dimension, weight in priority_weights.items():
        data_key = _DIMENSION_TO_DATA_KEY.get(dimension)
        result = candidate_entry.get(data_key) if data_key else None
        is_ok = bool(result) and result.get("status") == "ok"
        if is_ok:
            available[dimension] = weight
        else:
            unavailable_weight += weight

    total_available = sum(available.values())
    if total_available == 0:
        # Every dimension is missing for this candidate -- there's nothing to
        # reallocate onto. The caller should already be routing an
        # all-agents-failed report through the insufficient-data path rather
        # than reaching this per-candidate scoring step at all.
        return dict(priority_weights)

    return {
        dimension: round(weight + (weight / total_available) * unavailable_weight, 4)
        for dimension, weight in available.items()
    }
