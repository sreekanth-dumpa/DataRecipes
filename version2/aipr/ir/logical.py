"""Logical information IR: *what* must be computed (spec section 7).

The logical plan carries operators, input/output grains, recipe bindings,
temporal meaning and validation obligations.  It contains no engine, SQL text,
partitions or warehouse choices -- those live in the physical plan, so the
physical fingerprint can change while the semantic fingerprint stays constant.
"""
from __future__ import annotations

from typing import Any

from .. import COMPILER_VERSION
from ..core.hashing import fingerprint
from ..recipes.registry import Recipe
from .operators import OPERATORS


class IRError(ValueError):
    pass


def build_logical_ir(intent: dict[str, Any], recipe: Recipe) -> dict[str, Any]:
    rk = {"id": recipe.id, "version": recipe.version}
    dims = list(intent["dimensions"])
    nodes = [
        {"logical_node_id": "canonical_cohort", "operator": "canonical_event_selection", "recipe": rk,
         "inputs": ["quote_iterations", "channel_crosswalk"], "input_grains": [["quote_id", "iteration_no"], ["channel_code"]],
         "output_grain": ["quote_id"],
         "selection": {"order": ["issue_date", "iteration_no"], "filters": ["status = ISSUED", "eligible", "lob", "state"],
                       "dimension_join": {"keys": ["channel_code"], "relationship": "many_to_one", "unmatched": "unknown"}},
         "temporal": {"effective_anchor": "quote_issue_date", "knowledge_cutoff_ref": "intent.knowledge_cutoff",
                      "maturity": "issue_date + window <= date(cutoff)"},
         "obligations": ["no_future_knowledge", "preserve_quote_grain"]},
        {"logical_node_id": "quote_keys", "operator": "projection", "recipe": rk,
         "inputs": ["canonical_cohort"], "input_grains": [["quote_id"]], "output_grain": ["quote_id"],
         "projection": ["quote_id", "issue_date"], "predicate": "mature", "obligations": ["preserve_quote_grain"]},
        {"logical_node_id": "bind_outcome", "operator": "window_exists", "recipe": rk,
         "inputs": ["quote_keys", "bind_events"], "input_grains": [["quote_id"], ["bind_event_id"]],
         "output_grain": ["quote_id"],
         "join": {"keys": ["quote_id"], "relationship": "one_to_many", "semantics": "exists_within_window",
                  "reduction": "one_outcome_per_quote"},
         "temporal": {"effective_anchor": "bind_date", "knowledge_cutoff_ref": "intent.knowledge_cutoff",
                      "window": {"from": "issue_date", "to": "issue_date + window_days", "inclusive_end": True}},
         "obligations": ["no_future_knowledge", "preserve_quote_grain"]},
        {"logical_node_id": "quote_outcomes", "operator": "validated_relationship_join", "recipe": rk,
         "inputs": ["canonical_cohort", "bind_outcome"], "input_grains": [["quote_id"], ["quote_id"]],
         "output_grain": ["quote_id"],
         "join": {"keys": ["quote_id"], "relationship": "one_to_zero_or_one", "semantics": "left_preserve_cohort"},
         "obligations": ["preserve_quote_grain", "denominator_conservation"]},
        {"logical_node_id": "metric_aggregate", "operator": "aggregate_sufficient_statistics", "recipe": rk,
         "inputs": ["quote_outcomes"], "input_grains": [["quote_id"]], "output_grain": dims,
         "statistics": recipe.body["sufficient_statistics"], "derived": recipe.body["derived_metrics"],
         "obligations": ["numerator_not_above_denominator", "denominator_conservation"]},
        {"logical_node_id": "rate_interval", "operator": "approved_python_operator", "recipe": rk,
         "inputs": ["metric_aggregate"], "input_grains": [dims], "output_grain": dims,
         "python": {"operator_id": "wilson_interval", "version": "1.0.0", "params": {"z": 1.96}},
         "obligations": []},
    ]
    allowed = set(recipe.body["approved_operators"])
    for n in nodes:
        if n["operator"] not in OPERATORS or n["operator"] not in allowed:
            raise IRError(f"operator {n['operator']} is not approved for {recipe.key}")
    ir = {
        "ir_version": "1",
        "compiler_version": COMPILER_VERSION,
        "recipe": rk,
        "dimensions": dims,
        "parameter_schema": sorted(recipe.body["parameters"].keys()),
        "nodes": nodes,
        "outputs": ["rate_interval"],
    }
    validate_ir(ir)
    return ir


def validate_ir(ir: dict[str, Any]) -> None:
    ids = {n["logical_node_id"] for n in ir["nodes"]}
    external = {"quote_iterations", "channel_crosswalk", "bind_events"}
    for n in ir["nodes"]:
        for i in n["inputs"]:
            if i not in ids and i not in external:
                raise IRError(f"{n['logical_node_id']} consumes unknown input {i}")
    for o in ir["outputs"]:
        if o not in ids:
            raise IRError(f"required output {o} not produced")


def template_fingerprint(ir: dict[str, Any]) -> str:
    """Identity of the reusable plan *definition* (meaning shape, not bound values)."""
    return fingerprint({k: ir[k] for k in ("ir_version", "compiler_version", "recipe", "dimensions",
                                           "parameter_schema", "nodes", "outputs")})
