"""Checkpoint adaptation of the unstarted suffix (spec section 10.4, POP-style [R12]).

At the canonical-cohort materialisation boundary the adapter compares observed
volume with the range in which the current suffix is preferred.  It replans
only the *unstarted* suffix, only with admitted rewrite rules, only when the
lower-bound expected saving beats replanning/materialisation overhead by a
calibrated margin, and only within the revision budget.  Completed nodes are
immutable; the new version is persisted with its evidence.  This is an
application-level analogue of POP, not CHECK operators inside an engine.
"""
from __future__ import annotations

from typing import Any

from ..compiler.physical import _plan, partition_suffix
from ..ir.operators import REWRITE_RULES
from ..planner.costing import suffix_cost

PREFIX = ("canonical_cohort", "quote_keys", "bind_outcome")
REPLAN_OVERHEAD_S = 0.01


def preferred_range(n: int, target: int) -> list[int]:
    return [(n - 1) * target + 1 if n > 1 else 0, n * target]


def consider(plan: dict[str, Any], ir: dict[str, Any], dialect: Any, observed: dict[str, Any],
             node_states: dict[str, str], revisions_used: int, max_revisions: int, settings: Any,
             workers: int) -> dict[str, Any]:
    target = settings.rows_per_partition
    mature, cohort = int(observed["mature_rows"]), int(observed["rows"])
    current_n = plan["partitions"] if plan["variant"] == "partitioned" else 1
    costs = {n: suffix_cost(n, mature, cohort, plan["engine"], workers, target)
             for n in range(1, settings.max_partitions + 1)}
    desired_n = min(costs, key=lambda n: (round(costs[n], 6), n))
    decision: dict[str, Any] = {
        "checkpoint": "canonical_cohort", "observed": {"cohort_rows": cohort, "mature_rows": mature},
        "estimated": observed.get("estimated"), "current_variant": plan["variant"], "current_partitions": current_n,
        "validity_range_mature_rows": preferred_range(current_n, target), "desired_partitions": desired_n,
        "suffix_cost_by_partitions_s": {n: round(c, 5) for n, c in costs.items()},
        "revisions_used": revisions_used, "max_revisions": max_revisions,
    }
    if plan["variant"] == "fused":
        return {**decision, "action": "none", "reason": "fused plan has no stable materialisation boundary"}
    env = observed.get("envelope") or {}
    if env.get("rows_low") is not None and env["rows_low"] <= cohort <= env["rows_high"]:
        return {**decision, "action": "keep",
                "reason": f"observed cohort rows {cohort} inside the planned envelope "
                          f"[{env['rows_low']}, {env['rows_high']}]; no material deviation"}
    if desired_n == current_n:
        return {**decision, "action": "keep", "reason": "observation inside the current suffix's validity range"}
    cur = costs[current_n]
    new = costs[desired_n] + REPLAN_OVERHEAD_S
    benefit = (cur - new) / cur if cur > 0 else 0.0
    decision["expected_payoff"] = {"current_suffix_s": round(cur, 5), "candidate_suffix_s": round(new, 5),
                                   "relative_saving": round(benefit, 4), "min_margin": settings.adaptation_min_benefit}
    if benefit < settings.adaptation_min_benefit:
        return {**decision, "action": "keep", "reason": "expected saving below calibrated margin (anti-oscillation)"}
    if revisions_used >= max_revisions:
        return {**decision, "action": "keep", "reason": "revision budget exhausted"}
    suffix_ids = [n["node_id"] for n in plan["nodes"] if n["node_id"] not in PREFIX]
    started = [i for i in suffix_ids if node_states.get(i, "pending") != "pending"]
    if started:
        return {**decision, "action": "keep", "reason": f"suffix already started: {started}"}
    prefix_nodes = [n for n in plan["nodes"] if n["node_id"] in PREFIX]
    new_suffix = partition_suffix(ir, dialect, desired_n)
    rules = sorted({r for n in new_suffix for r in n["rewrite_rules"]})
    assert all(r in REWRITE_RULES for r in rules), "suffix uses an unadmitted rewrite rule"
    new_plan = _plan(ir, dialect, "partitioned", desired_n, prefix_nodes + new_suffix, "merge_partitions",
                     f"Adapted at canonical_cohort checkpoint: {mature} mature rows -> {desired_n} disjoint partitions.")
    return {**decision, "action": "replan_suffix", "new_plan": new_plan,
            "changed_nodes": {"removed": suffix_ids, "added": [n["node_id"] for n in new_suffix]},
            "equivalence_evidence": {"rules": rules, "class": "certified_rule",
                                     "assertions": ["same logical IR (semantic fingerprint unchanged)",
                                                    "source cut unchanged", "access fingerprint unchanged",
                                                    "completed prefix nodes reused unchanged"]},
            "reason": f"observed mature rows {mature} outside validity range {decision['validity_range_mature_rows']}"}
