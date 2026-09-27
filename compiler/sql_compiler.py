#!/usr/bin/env python3
"""
Compiles a validated recipe closure into parameterized Snowflake SQL
(Section 6.9). This is the "compile_steps" stage of the request-time path
in Section 5.

Scope and honesty about what's mechanical vs. templated:
  - Atomic recipes compile to a deterministic CTE: SELECT the declared
    output columns from the declared source_table, with WHERE predicates
    derived from recognized parameter names (knowledge_cutoff, cohort
    bounds). These predicates are placeholders — real column mapping
    depends on the Kairos context/FDP adapter, which is stubbed for this
    POC pass (Section 6.1 output is not yet wired to real source
    descriptors).
  - Composite recipes join their dependencies' CTEs on `implementation.
    join_on`, then compute `implementation.output_expressions` — split
    into an aggregate layer (expressions containing an aggregate function)
    and a derived layer (plain arithmetic over the aggregate layer's
    columns), so aliases like bind_rate can reference eligible_count and
    bound_count without illegal same-level alias references.

This module produces SQL text only. It does not execute against
Snowflake — no connection is wired up yet (see repo README / open
questions from the plan review).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from validator.validate import validate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECIPES_DIR = REPO_ROOT / "recipes"
AGGREGATE_FN_RE = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", re.IGNORECASE)

# Deterministic tie-break ordering, per Section 7: "SQL must use
# deterministic ordering when a recipe selects one record among ties."
DETERMINISTIC_ORDER_COMMENT = "-- deterministic ordering: none required (no QUALIFY/ROW_NUMBER tie-break in this recipe)"


def load_all(recipes_dir: Path) -> dict[str, dict]:
    by_key = {}
    for path in sorted(recipes_dir.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        by_key[f"{data['id']}@{data['version']}"] = data
    return by_key


def atomic_predicates(data: dict) -> list[str]:
    predicates = []
    param_names = {p["name"] for p in (data.get("parameters") or [])}
    if "knowledge_cutoff" in param_names:
        predicates.append("effective_date <= %(knowledge_cutoff)s  -- TODO: map to real FDP column via context adapter")
    if {"cohort_start", "cohort_end"} <= param_names:
        predicates.append("journey_date BETWEEN %(cohort_start)s AND %(cohort_end)s  -- TODO: map to real FDP column")
    return predicates


def compile_atomic_cte(alias: str, data: dict) -> str:
    impl = data.get("implementation", {})
    source_table = impl.get("source_table", "-- TODO: no source_table declared")
    columns = ", ".join(o["name"] for o in data.get("output", []))
    predicates = atomic_predicates(data)
    # Each predicate carries a trailing line comment, so joining with a
    # bare " AND " would comment out everything after the first predicate.
    # Put each predicate (and its comment) on its own line instead.
    if predicates:
        lines = [f"    WHERE {predicates[0]}"] + [f"      AND {p}" for p in predicates[1:]]
        where_clause = "\n" + "\n".join(lines)
    else:
        where_clause = ""
    return (
        f"  {alias} AS (\n"
        f"    -- {data['id']}@{data['version']} ({data['kind']}, {data['status']})\n"
        f"    SELECT {columns}\n"
        f"    FROM {source_table}{where_clause}\n"
        f"  )"
    )



def compile_join(root: dict, dep_aliases: dict[str, dict]) -> str:
    join_on = root.get("implementation", {}).get("join_on", [])
    if not join_on:
        raise ValueError(f"{root['id']}: composite recipe requires implementation.join_on to compile a join")
    aliases = list(dep_aliases.keys())
    base = aliases[0]
    lines = [f"  joined AS (\n    SELECT *\n    FROM {base}"]
    for alias in aliases[1:]:
        on_clause = " AND ".join(f"{alias}.{k} = {base}.{k}" for k in join_on)
        lines.append(f"    LEFT JOIN {alias} ON {on_clause}")
    lines.append("  )")
    return "\n".join(lines)


def split_output_expressions(output_expressions: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    aggregates, derived = {}, {}
    for name, expr in output_expressions.items():
        (aggregates if AGGREGATE_FN_RE.search(expr) else derived)[name] = expr
    return aggregates, derived


def compile_composite(root_key: str, root: dict, by_key: dict[str, dict]) -> str:
    impl = root.get("implementation", {})
    output_expressions = impl.get("output_expressions", {})
    group_by = impl.get("group_by", [])

    dep_aliases = {}
    for dep in root.get("uses", []):
        dep_data = by_key[dep["ref"]]
        dep_aliases[dep["as"]] = dep_data

    ctes = [compile_atomic_cte(alias, data) for alias, data in dep_aliases.items() if not data.get("uses")]
    ctes.append(compile_join(root, dep_aliases))

    aggregates, derived = split_output_expressions(output_expressions)

    agg_select_cols = list(group_by) + [f"{expr} AS {name}" for name, expr in aggregates.items()]
    agg_cte = (
        f"  agg AS (\n"
        f"    SELECT {', '.join(agg_select_cols)}\n"
        f"    FROM joined\n"
        + (f"    GROUP BY {', '.join(group_by)}\n" if group_by else "")
        + f"  )"
    )
    ctes.append(agg_cte)

    final_cols = [c.split(" AS ")[-1].split(".")[-1] if group_by and c in group_by else c for c in []]
    # Final SELECT: all group-by output columns (by their bare name) + aggregate columns + derived columns.
    group_by_bare = [g.split(".")[-1] for g in group_by]
    final_select = group_by_bare + list(aggregates.keys()) + [f"{expr} AS {name}" for name, expr in derived.items()]

    body = ",\n".join(ctes)
    return (
        f"-- Compiled from {root_key} (compiler: sql_compiler.py, stub — not executed)\n"
        f"WITH\n{body}\n"
        f"{DETERMINISTIC_ORDER_COMMENT}\n"
        f"SELECT {', '.join(final_select)}\nFROM agg;"
    )


def compile_recipe(root_key: str, recipes_dir: Path = DEFAULT_RECIPES_DIR) -> str:
    findings = validate(recipes_dir)
    errors = [f for f in findings if f.level == "ERROR"]
    if errors:
        raise SystemExit(f"refusing to compile: {len(errors)} validation error(s) in {recipes_dir}")

    by_key = load_all(recipes_dir)
    if root_key not in by_key:
        raise SystemExit(f"recipe not found: {root_key}")
    root = by_key[root_key]

    if not root.get("uses"):
        impl = root.get("implementation", {})
        columns = ", ".join(o["name"] for o in root.get("output", []))
        predicates = atomic_predicates(root)
        if predicates:
            lines = [f"WHERE {predicates[0]}"] + [f"  AND {p}" for p in predicates[1:]]
            where_clause = "\n" + "\n".join(lines)
        else:
            where_clause = ""
        return (
            f"-- Compiled from {root_key} (compiler: sql_compiler.py, stub — not executed)\n"
            f"SELECT {columns}\n"
            f"FROM {impl.get('source_table', '-- TODO: no source_table declared')}{where_clause}\n;"
        )

    return compile_composite(root_key, root, by_key)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe_key", help="id@version, e.g. quote.bind_rate_by_young_driver@1.0")
    parser.add_argument("--recipes-dir", default=str(DEFAULT_RECIPES_DIR))
    args = parser.parse_args()
    print(compile_recipe(args.recipe_key, Path(args.recipes_dir)))


if __name__ == "__main__":
    main()
