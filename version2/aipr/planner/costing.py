"""Estimates, Pareto frontier and plan choice (spec sections 2 'Estimate' and 8).

The cost model (``fixture_linear_v0``) is declared and uncalibrated: per-statement
overhead + per-row processing + materialisation write cost + a memory-pressure
term when one operator's input exceeds the per-operator row target.  Latency
follows the critical path given the worker count; credits are *notional*
(duration x warehouse rate), never billed cost.  Correctness and security are
constraints applied before ranking, never weighted terms.
"""
from __future__ import annotations

import math
from typing import Any

COST_MODEL = {
    "name": "fixture_linear_v0",
    "calibrated": False,
    "statement_overhead_s": {"duckdb": 0.02, "snowflake": 1.2},
    "per_row_s": {"duckdb": 2.0e-6, "snowflake": 4.0e-8},
    "write_per_row_s": {"duckdb": 1.0e-6, "snowflake": 2.0e-8},
    "spill_factor": 10.0,
    "warehouse_credits_per_hour": {"xsmall": 1, "small": 2, "medium": 4},
}


def _op(rows: float, engine: str, materialize: bool, target: int) -> float:
    m = COST_MODEL
    t = m["statement_overhead_s"][engine] + rows * m["per_row_s"][engine]
    if materialize:
        t += rows * m["write_per_row_s"][engine]
    if rows > target:
        t += (rows - target) * m["per_row_s"][engine] * m["spill_factor"]
    return t


def estimate_plan(plan: dict[str, Any], env: dict[str, Any], engine: str, workers: int,
                  rows_target: int, uncertainty: float, raw_rows: float | None = None) -> dict[str, Any]:
    cohort = env["canonical_cohort"]["rows_est"] or 0
    mature = env["quote_keys"]["rows_est"] or 0
    binds = raw_rows or mature * 1.3
    rows_by_node = {"canonical_cohort": cohort * 2, "quote_keys": cohort, "bind_outcome": binds + mature,
                    "quote_outcomes": cohort * 2, "metric_aggregate": cohort, "merge_partitions": 50}
    per_node: dict[str, float] = {}
    for n in plan["nodes"]:
        nid = n["node_id"]
        if n["kind"] == "python":
            per_node[nid] = 0.001
        elif plan["variant"] == "fused":
            # one statement does all work; the largest operator is the aggregation over the full cohort
            per_node[nid] = _op(cohort * 6 + binds, engine, False, rows_target * 4) + _op(cohort, engine, False, rows_target)
        elif nid.startswith("outcome_part_"):
            per_node[nid] = _op((cohort * 2 + mature) / plan["partitions"], engine, True, rows_target)
        else:
            per_node[nid] = _op(rows_by_node.get(nid, cohort), engine, True,
                                rows_target if nid in ("metric_aggregate", "quote_outcomes") else rows_target * 4)
    # critical path with bounded parallelism for partition fan-out
    parts = [v for k, v in per_node.items() if k.startswith("outcome_part_")]
    serial = sum(v for k, v in per_node.items() if not k.startswith("outcome_part_"))
    par = 0.0
    if parts:
        waves = math.ceil(len(parts) / max(1, workers))
        par = waves * max(parts)
    p50 = serial + par
    total_work = sum(per_node.values())
    rate = COST_MODEL["warehouse_credits_per_hour"]["xsmall"]
    credits = total_work * rate / 3600
    writes = sum(1 for n in plan["nodes"] if n["kind"] == "sql" and plan["variant"] != "fused")
    return {
        "model": COST_MODEL["name"], "calibrated": COST_MODEL["calibrated"],
        "duration_seconds": {"p50": round(p50, 4), "p90": round(p50 * uncertainty, 4),
                             "range": [round(p50 / uncertainty, 4), round(p50 * uncertainty, 4)]},
        "credits_notional": {"p50": round(credits, 6), "range": [round(credits / uncertainty, 6),
                                                                   round(credits * uncertainty, 6)]},
        "materialized_writes": writes,
        "per_node_seconds": {k: round(v, 5) for k, v in per_node.items()},
        "uncertainty_factor": uncertainty,
        "observability": {"fused": 1, "staged": 3, "partitioned": 3}[plan["variant"]],
        "label": "estimate",
    }


def pareto(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def dominated(a, b):  # b dominates a
        ka = (a["estimate"]["duration_seconds"]["p50"], a["estimate"]["credits_notional"]["p50"], -a["estimate"]["observability"])
        kb = (b["estimate"]["duration_seconds"]["p50"], b["estimate"]["credits_notional"]["p50"], -b["estimate"]["observability"])
        return all(y <= x for x, y in zip(ka, kb)) and any(y < x for x, y in zip(ka, kb))
    return [a for a in cands if not any(dominated(a, b) for b in cands if b is not a)]


def objective(est: dict[str, Any], weights: dict[str, float]) -> float:
    return (weights.get("latency", 1.0) * est["duration_seconds"]["p50"]
            + weights.get("cost", 1.0) * est["credits_notional"]["p50"] * 3600
            + weights.get("uncertainty", 0.1) * (est["duration_seconds"]["p90"] - est["duration_seconds"]["p50"])
            + weights.get("materialization", 0.002) * est["materialized_writes"])


def choose(cands: list[dict[str, Any]], budget_credits: float, max_runtime_s: float,
           weights: dict[str, float] | None = None, prefer: str | None = None) -> dict[str, Any]:
    weights = weights or {}
    feasible = []
    for c in cands:
        e = c["estimate"]
        reasons = []
        if e["credits_notional"]["range"][1] > budget_credits:
            reasons.append("credit range exceeds execution budget")
        if e["duration_seconds"]["p90"] > max_runtime_s:
            reasons.append("p90 duration exceeds max runtime")
        c["feasible"] = not reasons
        c["infeasible_reasons"] = reasons
        c["objective"] = round(objective(e, weights), 6)
        if not reasons:
            feasible.append(c)
    frontier = pareto(feasible)
    for c in cands:
        c["on_pareto_frontier"] = c in frontier
    if not feasible:
        return {"selected": None, "frontier": [], "reason": "no feasible candidate within budget/runtime"}
    if prefer:
        forced = [c for c in feasible if c["label"] == prefer or c["plan"]["variant"] == prefer]
        if forced:
            return {"selected": forced[0], "frontier": frontier,
                    "reason": f"user-selected physical variant '{prefer}' (semantically equivalent, admitted)"}
    best = min(frontier, key=lambda c: c["objective"])
    return {"selected": best, "frontier": frontier,
            "reason": f"lowest normalised objective on the Pareto frontier ({best['objective']})"}


def suffix_cost(n_parts: int, mature_rows: int, cohort_rows: int, engine: str, workers: int, target: int) -> float:
    """Remaining-work estimate for the partitioned outcome suffix (used by the checkpoint adapter)."""
    per = _op((cohort_rows * 2 + mature_rows) / max(1, n_parts), engine, True, target)
    return math.ceil(n_parts / max(1, workers)) * per + _op(50, engine, True, target)
