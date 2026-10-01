"""Approved Python operators (spec section 7).

The agent selects an operator id and parameters; it never sends code.  Each
operator is pinned by id/version with a determinism class.  The reference
operator is a Wilson score interval for each conversion rate: exact,
deterministic arithmetic on the validated sufficient statistics.
"""
from __future__ import annotations

import math
import platform
from typing import Any, Callable

from ..core.hashing import fingerprint


def wilson_interval(rows: list[dict[str, Any]], z: float = 1.96) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        n, k = int(r["eligible"]), int(r["converted"])
        rr = dict(r)
        if n == 0:
            rr.update(conversion_rate=None, ci_low=None, ci_high=None)
        else:
            p = k / n
            den = 1 + z * z / n
            centre = (p + z * z / (2 * n)) / den
            half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
            rr.update(conversion_rate=p, ci_low=max(0.0, centre - half), ci_high=min(1.0, centre + half))
        out.append(rr)
    return out


REGISTRY: dict[tuple[str, str], dict[str, Any]] = {
    ("wilson_interval", "1.0.0"): {
        "fn": wilson_interval, "determinism": "deterministic_float_ieee754",
        "reproducibility": "bitwise for identical inputs on the same runtime; tolerance 1e-12 across runtimes",
        "allowed_params": {"z"},
    }
}


def runtime_digest() -> str:
    return fingerprint({"python": platform.python_version(), "impl": platform.python_implementation(),
                        "operators": sorted(f"{a}@{b}" for a, b in REGISTRY)})


def run_operator(spec: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    key = (spec["operator_id"], spec["version"])
    if key not in REGISTRY:
        raise PermissionError(f"python operator {key} is not approved")
    entry = REGISTRY[key]
    params = spec.get("params", {})
    if set(params) - entry["allowed_params"]:
        raise PermissionError(f"parameters {set(params) - entry['allowed_params']} not allowed")
    fn: Callable = entry["fn"]
    out = fn(rows, **params)
    return out, {"operator": f"{key[0]}@{key[1]}", "determinism": entry["determinism"],
                 "reproducibility": entry["reproducibility"], "runtime_digest": runtime_digest()}
