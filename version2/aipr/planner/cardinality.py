"""Join-cardinality obligations (spec section 9).

For an equijoin on key k the exact matched cardinality is sum_k n_left(k) *
n_right(k).  Average fan-out conceals heavy hitters, so the envelope keeps the
max multiplicity and the probe's heavy-hitter list.  The naive raw join of the
cohort to bind events is evaluated only to be *rejected* with the
``quote_grain_violation`` rule: existence semantics require one outcome per quote.
"""
from __future__ import annotations

from typing import Any

JOIN_PATTERNS = {
    "existence": "EXISTS or reduce to one matching quote key",
    "child_facts_at_parent_grain": "aggregate children independently before joining",
    "versioned_dimension": "resolve as-of intervals and assert non-overlap",
    "business_bridge": "explicit allocation weights and denominator rules",
    "range_join": "coarse equijoin buckets then exact residual predicate",
    "legit_many_to_many": "partition and aggregate early where algebra permits; budget explicitly",
    "skewed_keys": "isolate heavy hitters",
    "unknown_relationship": "probe, clarify or block; never guess",
}


def q_error(estimated: float | None, actual: float | None) -> float | None:
    """Q-error with explicit zero handling (zero cases reported separately, not clamped)."""
    if estimated is None or actual is None:
        return None
    if estimated == 0 or actual == 0:
        return None if estimated == actual == 0 else float("inf")
    return max(estimated / actual, actual / estimated)


def join_envelopes(profile: dict[str, Any], uncertainty: float) -> dict[str, Any]:
    s = profile["stats"]
    size = s.get("mature_cohort_size", {}).get("value", {})
    bm = s.get("bind_multiplicity", {}).get("value", {})
    cohort = size.get("cohort_rows")
    mature = size.get("mature_rows")
    matched = bm.get("matched_keys")
    raw = bm.get("raw_join_rows")

    def env(x):
        if x is None:
            return {"rows_est": None, "rows_low": None, "rows_high": None}
        return {"rows_est": x, "rows_low": int(x / uncertainty), "rows_high": int(x * uncertainty) + 1}

    return {
        "canonical_cohort": env(cohort),
        "quote_keys": env(mature),
        "bind_outcome": env(matched if matched is not None else (int(mature * 0.3) if mature else None)),
        "quote_outcomes": env(cohort),
        "raw_join": {"rows_est": raw, "max_multiplicity": bm.get("max_multiplicity"),
                     "avg_multiplicity": bm.get("avg_multiplicity"), "p95_multiplicity": bm.get("p95_multiplicity"),
                     "heavy_hitters": bm.get("heavy_hitters", [])},
    }


def rejected_alternatives(envelopes: dict[str, Any]) -> list[dict[str, Any]]:
    raw = envelopes["raw_join"]
    return [{
        "plan": "raw_join",
        "rule": "quote_grain_violation",
        "pattern": "existence",
        "preferred_action": JOIN_PATTERNS["existence"],
        "detail": (f"joining mature quote keys to every BIND event yields sum n_left*n_right = {raw['rows_est']} rows "
                   f"(max multiplicity {raw['max_multiplicity']}); counting them would multiply converted quotes. "
                   "DISTINCT would not repair an undefined grain."),
    }]
