"""Deterministic compiler: logical IR -> physical DAG variants (spec sections 7, 8, 10).

Variants share the same fragments and therefore the same certified semantics:

* ``fused``        -- one statement (CTEs); fewest writes, least observability
* ``staged``       -- materialised boundaries at canonical cohort, key
                      reduction and quote outcomes; enables grain/cardinality
                      checks between stages and suffix adaptation
* ``partitioned``  -- staged prefix, then N disjoint hash partitions of the
                      quote-grain outcome aggregation merged by SUM

Each node lists the rewrite rules that justify it.  The compiler also emits
the rejected naive raw-join alternative so the rejection is traceable.
"""
from __future__ import annotations

import re
from typing import Any

from .. import COMPILER_VERSION
from ..core.hashing import fingerprint
from . import sqlgen
from .dialect import SOURCE_ALLOWLIST, Dialect

PARAMS = {"state", "lob", "cohort_start", "cohort_end", "window_days", "cutoff_ts", "cutoff_date"}


class CompileError(ValueError):
    pass


def _params_in(sql: str, dialect: Dialect) -> list[str]:
    pat = r"%\((\w+)\)s" if dialect.name == "snowflake" else r"\$(\w+)"
    return sorted(set(re.findall(pat, sql)))


def _node(node_id: str, logical: list[str], sql: str | None, deps: list[str], grain: list[str],
          validations: list[dict], rules: list[str], dialect: Dialect, kind: str = "sql",
          python: dict | None = None) -> dict[str, Any]:
    n = {"node_id": node_id, "kind": kind, "logical_node_ids": logical, "depends_on": deps,
         "output_grain": grain, "validations": validations, "rewrite_rules": rules,
         "materialization": "stage" if kind == "sql" else "memory", "resource_class": "xsmall",
         "envelope": {}}
    if sql is not None:
        n["sql"] = sql
        n["params"] = _params_in(sql, dialect)
    if python is not None:
        n["python"] = python
    return n


def _cohort_validations() -> list[dict]:
    return [{"id": "grain_canonical_quote", "type": "unique_key", "key": ["quote_id"], "blocking": True},
            {"id": "no_future_knowledge", "type": "max_le_param", "column": "known_at", "param": "cutoff_ts", "blocking": True},
            {"id": "cohort_bounds", "type": "between_params", "column": "issue_date",
             "low": "cohort_start", "high": "cohort_end", "blocking": True}]


def _aggregate_validations(dims: list[str], conservation: bool) -> list[dict]:
    v = [{"id": "grain_output", "type": "unique_key", "key": dims, "blocking": True},
         {"id": "numerator_not_above_denominator", "type": "numerator_le_denominator", "blocking": True},
         {"id": "non_negative_counts", "type": "non_negative", "columns": ["eligible", "converted", "immature_excluded"],
          "blocking": True}]
    if conservation:
        v.append({"id": "denominator_conservation", "type": "conservation", "against": "canonical_cohort", "blocking": True})
    return v


def _python_node(ir: dict[str, Any], dep: str, dims: list[str], dialect: Dialect) -> dict[str, Any]:
    spec = next(n for n in ir["nodes"] if n["logical_node_id"] == "rate_interval")["python"]
    return _node("rate_interval", ["rate_interval"], None, [dep], dims,
                 [{"id": "row_preservation", "type": "python_rows_preserved", "blocking": True}], [], dialect,
                 kind="python", python=spec)


def _staged_prefix(dialect: Dialect) -> list[dict[str, Any]]:
    return [
        _node("canonical_cohort", ["canonical_cohort"], sqlgen.canonical_cohort(dialect), [], ["quote_id"],
              _cohort_validations(), ["RW_STAGE_MATERIALIZE", "RW_PREDICATE_PUSHDOWN"], dialect),
        _node("quote_keys", ["quote_keys"], sqlgen.quote_keys(), ["canonical_cohort"], ["quote_id"],
              [{"id": "grain_quote_keys", "type": "unique_key", "key": ["quote_id"], "blocking": True}],
              ["RW_STAGE_MATERIALIZE"], dialect),
        _node("bind_outcome", ["bind_outcome"], sqlgen.bind_outcome(dialect), ["quote_keys"], ["quote_id"],
              [{"id": "one_outcome_per_quote", "type": "unique_key", "key": ["quote_id"], "blocking": True},
               {"id": "outcome_keys_in_cohort", "type": "subset_of", "key": "quote_id", "of": "quote_keys",
                "blocking": True}],
              ["RW_SEMIJOIN_KEY_REDUCTION", "RW_STAGE_MATERIALIZE"], dialect),
    ]


def compile_fused(ir: dict[str, Any], dialect: Dialect) -> dict[str, Any]:
    dims = ir["dimensions"]
    nodes = [_node("metric_aggregate", ["canonical_cohort", "quote_keys", "bind_outcome", "quote_outcomes",
                                        "metric_aggregate"],
                   sqlgen.fused(dialect, dims), [], dims, _aggregate_validations(dims, conservation=False),
                   ["RW_FUSE_CTE", "RW_SEMIJOIN_KEY_REDUCTION", "RW_PREDICATE_PUSHDOWN"], dialect),
             _python_node(ir, "metric_aggregate", dims, dialect)]
    return _plan(ir, dialect, "fused", 1, nodes, "metric_aggregate",
                 "Small, inexpensive adjacent operators kept together; no intermediate writes. "
                 "Denominator conservation cannot be checked against a materialised cohort.")


def compile_staged(ir: dict[str, Any], dialect: Dialect) -> dict[str, Any]:
    dims = ir["dimensions"]
    nodes = _staged_prefix(dialect) + [
        _node("quote_outcomes", ["quote_outcomes"], sqlgen.quote_outcomes(), ["canonical_cohort", "bind_outcome"],
              ["quote_id"],
              [{"id": "grain_quote_outcomes", "type": "unique_key", "key": ["quote_id"], "blocking": True},
               {"id": "left_join_preserves_cohort", "type": "rowcount_equals", "of": "canonical_cohort", "blocking": True}],
              ["RW_STAGE_MATERIALIZE"], dialect),
        _node("metric_aggregate", ["metric_aggregate"], sqlgen.metric_aggregate(dims), ["quote_outcomes"], dims,
              _aggregate_validations(dims, conservation=True), ["RW_STAGE_MATERIALIZE"], dialect),
        _python_node(ir, "metric_aggregate", dims, dialect),
    ]
    return _plan(ir, dialect, "staged", 1, nodes, "metric_aggregate",
                 "Materialise at the canonical-cohort grain boundary and after the one-to-many bind reduction "
                 "so grain, fan-out and conservation are validated before the aggregate.")


def partition_suffix(ir: dict[str, Any], dialect: Dialect, n: int) -> list[dict[str, Any]]:
    dims = ir["dimensions"]
    parts = [f"outcome_part_{i}" for i in range(n)]
    nodes = [
        _node(p, ["quote_outcomes", "metric_aggregate"], sqlgen.outcome_partition(dialect, dims, i, n),
              ["canonical_cohort", "bind_outcome"], dims,
              [{"id": f"{p}_numerator_le_denominator", "type": "numerator_le_denominator", "blocking": True}],
              ["RW_DISJOINT_HASH_PARTITION_COUNTS", "RW_STAGE_MATERIALIZE"], dialect)
        for i, p in enumerate(parts)]
    nodes.append(_node("merge_partitions", ["metric_aggregate"], sqlgen.merge_partitions(dims, parts), parts, dims,
                       _aggregate_validations(dims, conservation=True), ["RW_DISJOINT_HASH_PARTITION_COUNTS"], dialect))
    nodes.append(_python_node(ir, "merge_partitions", dims, dialect))
    return nodes


def compile_partitioned(ir: dict[str, Any], dialect: Dialect, n: int) -> dict[str, Any]:
    if n < 1:
        raise CompileError("partition count must be >= 1")
    nodes = _staged_prefix(dialect) + partition_suffix(ir, dialect, n)
    return _plan(ir, dialect, "partitioned", n, nodes, "merge_partitions",
                 f"Staged prefix, then {n} disjoint quote_id hash partitions after the bind reduction; "
                 "counts merge exactly and the rate is recomputed after the merge.")


def _plan(ir: dict[str, Any], dialect: Dialect, variant: str, n: int, nodes: list[dict], final_sql: str,
          rationale: str) -> dict[str, Any]:
    plan = {"variant": variant, "partitions": n, "engine": dialect.name, "compiler_version": COMPILER_VERSION,
            "recipe": ir["recipe"], "dimensions": ir["dimensions"], "nodes": nodes, "final_sql_node": final_sql,
            "outputs": ["rate_interval"], "decomposition_rationale": rationale,
            "rewrite_rules": sorted({r for nd in nodes for r in nd["rewrite_rules"]})}
    check_dag(plan)
    plan["physical_fingerprint"] = fingerprint({k: v for k, v in plan.items()})
    return plan


def topo_order(plan: dict[str, Any]) -> list[str]:
    deps = {n["node_id"]: set(n["depends_on"]) for n in plan["nodes"]}
    order, ready = [], sorted(k for k, v in deps.items() if not v)
    remaining = {k: set(v) for k, v in deps.items()}
    while ready:
        k = ready.pop(0)
        order.append(k)
        for j, v in remaining.items():
            if k in v:
                v.discard(k)
                if not v and j not in order and j not in ready:
                    ready.append(j)
        ready.sort()
    if len(order) != len(deps):
        raise CompileError("plan graph is cyclic")
    return order


def check_dag(plan: dict[str, Any], max_nodes: int = 64) -> None:
    ids = [n["node_id"] for n in plan["nodes"]]
    if len(ids) != len(set(ids)):
        raise CompileError("duplicate node ids")
    if len(ids) > max_nodes:
        raise CompileError("plan exceeds node bound")
    for n in plan["nodes"]:
        for d in n["depends_on"]:
            if d not in ids:
                raise CompileError(f"{n['node_id']} depends on unknown {d}")
        for v in n["validations"]:
            for ref in (v.get("of"), v.get("against")):
                if ref and ref not in ids and v["type"] != "subset_of":
                    raise CompileError(f"{n['node_id']} validation references unknown {ref}")
    topo_order(plan)
    for o in plan["outputs"]:
        if o not in ids:
            raise CompileError(f"required output {o} not produced")


def allowed_relations() -> set[str]:
    return set(SOURCE_ALLOWLIST)
