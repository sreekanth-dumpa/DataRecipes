"""Ternary Logic Partitioning metamorphic oracle (SQLancer TLP [R16]), adapted to DuckDB.

For a supported projection/filter query Q over relation R and a deterministic
predicate p:  bag(Q) == bag(Q WHERE p) + bag(Q WHERE NOT p) + bag(Q WHERE p IS NULL),
recombined with bag-preserving UNION ALL.  Aggregates, DISTINCT, ORDER BY and
LIMIT are rejected (they need their own transformations).  A pass means "no
discrepancy observed"; it says nothing about business meaning.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

import sqlglot
from sqlglot import exp


def _supported(select: exp.Select) -> str | None:
    if select.args.get("distinct"):
        return "DISTINCT requires its own transformation"
    if select.find(exp.AggFunc) or select.args.get("group"):
        return "aggregates are not supported by the plain TLP partition"
    if select.args.get("order") or select.args.get("limit"):
        return "ORDER BY / LIMIT change bag semantics"
    if select.args.get("where"):
        return "base query must not already filter (wrap it as a relation)"
    return None


def tlp_check(engine: Any, base_sql: str, predicate: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    q = sqlglot.parse_one(base_sql, read="duckdb")
    if not isinstance(q, exp.Select):
        return {"verdict": "unsupported", "reason": "only SELECT is supported"}
    why = _supported(q)
    if why:
        return {"verdict": "unsupported", "reason": why}
    parts = [q.copy().where(f"({predicate})"), q.copy().where(f"NOT ({predicate})"),
             q.copy().where(f"({predicate}) IS NULL")]
    union = " UNION ALL ".join(p.sql(dialect="duckdb") for p in parts)
    base = Counter(tuple(r.values()) for r in engine.fetch(base_sql, params))
    recombined = Counter(tuple(r.values()) for r in engine.fetch(union, params))
    ok = base == recombined
    out = {"verdict": "no_discrepancy_observed" if ok else "counterexample", "predicate": predicate,
           "base_rows": sum(base.values()), "partitioned_rows": sum(recombined.values()),
           "scope": "engine/query consistency only; not business correctness"}
    if not ok:
        out["minimized_difference"] = {"missing": list((base - recombined).items())[:5],
                                       "extra": list((recombined - base).items())[:5]}
    return out
