"""Approved operator catalog (spec section 7).

Each operator declares accepted input grains, output grain behaviour, temporal
assumptions, determinism class and the rewrite rules that may be applied to it.
The compiler refuses any node whose operator is not in this catalog or not in
the recipe's ``approved_operators`` list.
"""
from __future__ import annotations

OPERATORS: dict[str, dict] = {
    "scoped_scan": {"engine": ["sql"], "output_grain": "input", "determinism": "deterministic",
                    "temporal": "applies knowledge cutoff to recorded_at", "rewrites": ["RW_PREDICATE_PUSHDOWN"]},
    "canonical_event_selection": {"engine": ["sql"], "output_grain": "partition_key", "determinism": "deterministic",
                                  "temporal": "first event by explicit total order; known-by cutoff",
                                  "rewrites": ["RW_STAGE_MATERIALIZE", "RW_FUSE_CTE"]},
    "projection": {"engine": ["sql"], "output_grain": "input", "determinism": "deterministic",
                   "temporal": "none", "rewrites": ["RW_STAGE_MATERIALIZE", "RW_FUSE_CTE"]},
    "filter": {"engine": ["sql"], "output_grain": "input", "determinism": "deterministic",
               "temporal": "none", "rewrites": ["RW_PREDICATE_PUSHDOWN"]},
    "key_reduction": {"engine": ["sql"], "output_grain": "key", "determinism": "deterministic",
                      "temporal": "window predicate retained exactly", "rewrites": ["RW_SEMIJOIN_KEY_REDUCTION"]},
    "window_exists": {"engine": ["sql"], "output_grain": "left", "determinism": "deterministic",
                      "temporal": "event-window resolution with knowledge cutoff",
                      "rewrites": ["RW_SEMIJOIN_KEY_REDUCTION", "RW_STAGE_MATERIALIZE", "RW_FUSE_CTE"]},
    "validated_relationship_join": {"engine": ["sql"], "output_grain": "left", "determinism": "deterministic",
                                    "temporal": "none", "rewrites": ["RW_STAGE_MATERIALIZE", "RW_FUSE_CTE"]},
    "aggregate_sufficient_statistics": {"engine": ["sql"], "output_grain": "dimensions", "determinism": "deterministic",
                                        "temporal": "none", "rewrites": ["RW_DISJOINT_HASH_PARTITION_COUNTS", "RW_FUSE_CTE"]},
    "union_disjoint_partitions": {"engine": ["sql"], "output_grain": "dimensions", "determinism": "deterministic",
                                  "temporal": "none", "rewrites": []},
    "approved_python_operator": {"engine": ["python"], "output_grain": "input", "determinism": "declared_by_operator",
                                 "temporal": "none", "rewrites": []},
    "assertion": {"engine": ["sql", "python"], "output_grain": "none", "determinism": "deterministic",
                  "temporal": "none", "rewrites": []},
}

# Rewrite rules with preconditions and evidence class (spec section 7.1).
REWRITE_RULES: dict[str, dict] = {
    "RW_FUSE_CTE": {
        "description": "Inline stage relations as CTEs in one statement",
        "preconditions": ["each stage is a pure relational expression", "no Python operator inside the fused region"],
        "evidence_class": "certified_rule"},
    "RW_STAGE_MATERIALIZE": {
        "description": "Materialise a CTE as a governed stage relation at a validation boundary",
        "preconditions": ["stage relation is read under the same source cut", "stage inherits source access policy"],
        "evidence_class": "certified_rule"},
    "RW_SEMIJOIN_KEY_REDUCTION": {
        "description": "Reduce bind events to quote keys (semi-join + GROUP BY key) before joining back at quote grain",
        "preconditions": ["outcome semantics is EXISTS within window", "reduction keeps exact window predicate",
                          "inner-join region only (no preserved outer side is reduced)"],
        "evidence_class": "certified_rule"},
    "RW_DISJOINT_HASH_PARTITION_COUNTS": {
        "description": "Split quote-grain outcomes into hash partitions of quote_id, aggregate counts per part, merge by SUM",
        "preconditions": ["partition key equals the outcome grain key (quote_id)", "metric state is counts/sums only",
                          "rate recomputed after merge", "every key maps to exactly one partition"],
        "evidence_class": "certified_rule"},
    "RW_PREDICATE_PUSHDOWN": {
        "description": "Apply scope and knowledge predicates at the scan",
        "preconditions": ["predicate references only scanned columns"],
        "evidence_class": "certified_rule"},
}
