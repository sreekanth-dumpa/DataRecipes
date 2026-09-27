#!/usr/bin/env python3
"""
Compiles a validated recipe closure into parameterized SQL for a target
engine (Section 6.9). This is the "compile_steps" stage of the
request-time path in Section 5.

Engines: duckdb (executable in this POC pass, see builder/db/duckdb_runner.py)
and snowflake (declared, not wired to a live connection yet).

Scope and honesty about what's mechanical vs. templated:
  - Atomic recipes compile to a deterministic CTE: SELECT the declared
    output columns from the engine's declared source_table, with WHERE
    predicates derived from recognized parameter names (knowledge_cutoff,
    cohort bounds). Column names (effective_date, journey_date) are real
    for the synthetic DuckDB warehouse (see context/osi/ for the schema
    they come from) but the mapping is still a POC-level convention, not
    a general column-binding language.
  - Composite recipes join their dependencies' CTEs on `implementation.
    join_on`, then compute `implementation.output_expressions` — split
    into an aggregate layer (expressions containing an aggregate function)
    and a derived layer (plain arithmetic over the aggregate layer's
    columns), so aliases like bind_rate can reference eligible_count and
    bound_count without illegal same-level alias references. These
    expressions are engine-agnostic ANSI SQL.

This module produces SQL text; execution against DuckDB happens in
builder/db/duckdb_runner.py, not here.
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
DEFAULT_ENGINE = "duckdb"
AGGREGATE_FN_RE = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", re.IGNORECASE)

# Deterministic tie-break ordering, per Section 7: "SQL must use
# deterministic ordering when a recipe selects one record among ties."
DETERMINISTIC_ORDER_COMMENT = "-- deterministic ordering: none required (no QUALIFY/ROW_NUMBER tie-break in this recipe)"

# Named-parameter placeholder syntax per engine's Python driver convention.
PLACEHOLDER_STYLE = {
    "duckdb": lambda name: f"${name}",
    "snowflake": lambda name: f"%({name})s",
}


def placeholder(engine: str, name: str) -> str:
    try:
        return PLACEHOLDER_STYLE[engine](name)
    except KeyError:
        raise SystemExit(f"unsupported engine: {engine} (expected one of {list(PLACEHOLDER_STYLE)})")


def source_table_for(data: dict, engine: str) -> str:
    engines = data.get("implementation", {}).get("engines", {})
    binding = engines.get(engine)
    if not binding:
        return f"-- TODO: no {engine} binding declared for {data.get('id')}"
    return binding["source_table"]


def load_all(recipes_dir: Path) -> dict[str, dict]:
    by_key = {}
    for path in sorted(recipes_dir.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        by_key[f"{data['id']}@{data['version']}"] = data
    return by_key


def atomic_predicates(data: dict, engine: str) -> list[str]:
    predicates = []
    param_names = {p["name"] for p in (data.get("parameters") or [])}
    if "knowledge_cutoff" in param_names:
        predicates.append(f"effective_date <= {placeholder(engine, 'knowledge_cutoff')}")
    if {"cohort_start", "cohort_end"} <= param_names:
        predicates.append(
            f"journey_date BETWEEN {placeholder(engine, 'cohort_start')} AND {placeholder(engine, 'cohort_end')}"
        )
    if "product_line" in param_names:
        predicates.append(f"product_line = {placeholder(engine, 'product_line')}")
    return predicates


def _where_clause(predicates: list[str], indent: str) -> str:
    if not predicates:
        return ""
    lines = [f"{indent}WHERE {predicates[0]}"] + [f"{indent}  AND {p}" for p in predicates[1:]]
    return "\n" + "\n".join(lines)


def compile_atomic_cte(alias: str, data: dict, engine: str) -> str:
    source_table = source_table_for(data, engine)
    columns = ", ".join(o["name"] for o in data.get("output", []))
    predicates = atomic_predicates(data, engine)
    where_clause = _where_clause(predicates, "    ")
    return (
        f"  {alias} AS (\n"
        f"    -- {data['id']}@{data['version']} ({data['kind']}, {data['status']})\n"
        f"    SELECT {columns}\n"
        f"    FROM {source_table}{where_clause}\n"
        f"  )"
    )


def compile_from_clause(root: dict, dep_aliases: dict[str, dict]) -> str:
    """Builds the FROM/LEFT JOIN clause referencing dependency aliases
    directly (eligible.is_eligible, segment.young_driver_segment, ...).

    Deliberately NOT flattened through an intermediate 'SELECT * AS joined'
    CTE: DuckDB (and standard SQL) drops table qualifiers once columns pass
    through a bare SELECT *, so a later CTE referencing 'segment.foo' would
    fail with 'table not found'. Keeping the join inline in the aggregation
    step's FROM clause preserves the aliases where they're used.
    """
    join_on = root.get("implementation", {}).get("join_on", [])
    if not join_on:
        raise ValueError(f"{root['id']}: composite recipe requires implementation.join_on to compile a join")
    aliases = list(dep_aliases.keys())
    base = aliases[0]
    lines = [f"    FROM {base}"]
    for alias in aliases[1:]:
        on_clause = " AND ".join(f"{alias}.{k} = {base}.{k}" for k in join_on)
        lines.append(f"    LEFT JOIN {alias} ON {on_clause}")
    return "\n".join(lines)


def split_output_expressions(output_expressions: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    aggregates, derived = {}, {}
    for name, expr in output_expressions.items():
        (aggregates if AGGREGATE_FN_RE.search(expr) else derived)[name] = expr
    return aggregates, derived


def compile_composite(root_key: str, root: dict, by_key: dict[str, dict], engine: str) -> str:
    impl = root.get("implementation", {})
    output_expressions = impl.get("output_expressions", {})
    group_by = impl.get("group_by", [])

    dep_aliases = {}
    for dep in root.get("uses", []):
        dep_data = by_key[dep["ref"]]
        dep_aliases[dep["as"]] = dep_data

    ctes = [compile_atomic_cte(alias, data, engine) for alias, data in dep_aliases.items() if not data.get("uses")]

    aggregates, derived = split_output_expressions(output_expressions)

    agg_select_cols = list(group_by) + [f"{expr} AS {name}" for name, expr in aggregates.items()]
    from_clause = compile_from_clause(root, dep_aliases)
    agg_cte = (
        f"  agg AS (\n"
        f"    SELECT {', '.join(agg_select_cols)}\n"
        f"{from_clause}\n"
        + (f"    GROUP BY {', '.join(group_by)}\n" if group_by else "")
        + f"  )"
    )
    ctes.append(agg_cte)

    group_by_bare = [g.split(".")[-1] for g in group_by]
    final_select = group_by_bare + list(aggregates.keys()) + [f"{expr} AS {name}" for name, expr in derived.items()]

    body = ",\n".join(ctes)
    return (
        f"-- Compiled from {root_key} for engine={engine}\n"
        f"WITH\n{body}\n"
        f"{DETERMINISTIC_ORDER_COMMENT}\n"
        f"SELECT {', '.join(final_select)}\nFROM agg;"
    )


def compile_recipe(root_key: str, recipes_dir: Path = DEFAULT_RECIPES_DIR, engine: str = DEFAULT_ENGINE) -> str:
    findings = validate(recipes_dir)
    errors = [f for f in findings if f.level == "ERROR"]
    if errors:
        raise SystemExit(f"refusing to compile: {len(errors)} validation error(s) in {recipes_dir}")

    by_key = load_all(recipes_dir)
    if root_key not in by_key:
        raise SystemExit(f"recipe not found: {root_key}")
    root = by_key[root_key]

    if not root.get("uses"):
        columns = ", ".join(o["name"] for o in root.get("output", []))
        predicates = atomic_predicates(root, engine)
        where_clause = _where_clause(predicates, "")
        return (
            f"-- Compiled from {root_key} for engine={engine}\n"
            f"SELECT {columns}\n"
            f"FROM {source_table_for(root, engine)}{where_clause}\n;"
        )

    return compile_composite(root_key, root, by_key, engine)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recipe_key", help="id@version, e.g. quote.bind_rate_by_young_driver@1.0")
    parser.add_argument("--recipes-dir", default=str(DEFAULT_RECIPES_DIR))
    parser.add_argument("--engine", default=DEFAULT_ENGINE, choices=list(PLACEHOLDER_STYLE))
    args = parser.parse_args()
    print(compile_recipe(args.recipe_key, Path(args.recipes_dir), args.engine))


if __name__ == "__main__":
    main()
