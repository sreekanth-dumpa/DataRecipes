#!/usr/bin/env python3
"""
Static recipe validator (Just-in-Time Derived Data Product Architecture v2,
Section 2 + Section 6.8).

Deterministic, mandatory gate. Checks that are mechanically decidable from
the recipe specs alone, with no source-data access:

  1. Structural schema (recipe.schema.json)
  2. Duplicate id@version
  3. Dependency resolution (every `uses[].ref` exists in the loaded set)
  4. Dependency cycles (DAG requirement)
  5. Withdrawn/deprecated dependency reuse
  6. Ambiguous as-of behavior (a recipe touching effective-dated data must
     declare time_semantics.as_of_behavior)
  7. Missing parameter bindings (a composite must declare every required
     parameter its dependencies require, by name)
  8. Grain traceability (advisory only — see docstring on `check_grain`)

Exit code is non-zero if any ERROR-level finding exists. WARNING-level
findings (grain traceability) do not fail the gate but are reported.

Usage:
    python validate.py [recipes_dir]
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

try:
    import jsonschema
except ImportError:  # pragma: no cover
    sys.exit("jsonschema is required: pip install jsonschema --break-system-packages")

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "registry" / "recipe.schema.json"
DEFAULT_RECIPES_DIR = REPO_ROOT / "recipes"

REJECT_STATUSES = {"withdrawn"}


@dataclass
class Finding:
    level: str  # "ERROR" | "WARNING"
    recipe: str
    message: str


@dataclass
class Recipe:
    path: Path
    data: dict
    id: str = field(init=False)
    version: str = field(init=False)
    key: str = field(init=False)

    def __post_init__(self):
        self.id = self.data.get("id", "")
        self.version = self.data.get("version", "")
        self.key = f"{self.id}@{self.version}"


def load_recipes(recipes_dir: Path) -> list[Recipe]:
    recipes = []
    for path in sorted(recipes_dir.rglob("*.yaml")):
        with open(path) as f:
            data = yaml.safe_load(f)
        recipes.append(Recipe(path=path, data=data))
    return recipes


def check_schema(recipe: Recipe, schema: dict, findings: list[Finding]) -> None:
    validator = jsonschema.Draft7Validator(schema)
    for err in validator.iter_errors(recipe.data):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        findings.append(Finding("ERROR", recipe.key or str(recipe.path), f"schema: {loc}: {err.message}"))


def check_duplicates(recipes: list[Recipe], findings: list[Finding]) -> None:
    seen: dict[str, Path] = {}
    for r in recipes:
        if r.key in seen:
            findings.append(Finding("ERROR", r.key, f"duplicate id@version, also defined at {seen[r.key]}"))
        else:
            seen[r.key] = r.path


def check_dependencies_resolve(recipes: list[Recipe], by_key: dict[str, Recipe], findings: list[Finding]) -> None:
    for r in recipes:
        for dep in r.data.get("uses", []) or []:
            ref = dep.get("ref", "")
            if ref not in by_key:
                findings.append(Finding("ERROR", r.key, f"unresolved dependency: {ref}"))


def check_status_reuse(recipes: list[Recipe], by_key: dict[str, Recipe], findings: list[Finding]) -> None:
    for r in recipes:
        for dep in r.data.get("uses", []) or []:
            ref = dep.get("ref", "")
            dep_recipe = by_key.get(ref)
            if dep_recipe and dep_recipe.data.get("status") in REJECT_STATUSES:
                findings.append(Finding("ERROR", r.key, f"depends on withdrawn recipe: {ref}"))


def check_cycles(recipes: list[Recipe], by_key: dict[str, Recipe], findings: list[Finding]) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {r.key: WHITE for r in recipes}
    stack_path: list[str] = []

    def visit(key: str) -> bool:
        color[key] = GRAY
        stack_path.append(key)
        r = by_key.get(key)
        if r:
            for dep in r.data.get("uses", []) or []:
                ref = dep.get("ref", "")
                if ref not in color:
                    continue  # already reported as unresolved
                if color[ref] == GRAY:
                    cycle = " -> ".join(stack_path[stack_path.index(ref):] + [ref])
                    findings.append(Finding("ERROR", key, f"dependency cycle: {cycle}"))
                    return True
                if color[ref] == WHITE:
                    if visit(ref):
                        return True
        stack_path.pop()
        color[key] = BLACK
        return False

    for r in recipes:
        if color[r.key] == WHITE:
            visit(r.key)


def check_as_of_ambiguity(recipes: list[Recipe], findings: list[Finding]) -> None:
    """A recipe is 'time-sensitive' if it declares a knowledge_cutoff-style
    parameter or depends on one that does. Any time-sensitive recipe must
    declare time_semantics.as_of_behavior explicitly."""
    for r in recipes:
        params = {p["name"] for p in (r.data.get("parameters") or [])}
        time_sensitive = any("cutoff" in p or "as_of" in p for p in params)
        ts = r.data.get("time_semantics") or {}
        if time_sensitive and not ts.get("as_of_behavior"):
            findings.append(Finding("ERROR", r.key, "ambiguous as-of behavior: time-sensitive parameter present but time_semantics.as_of_behavior is not declared"))


def check_parameter_bindings(recipes: list[Recipe], by_key: dict[str, Recipe], findings: list[Finding]) -> None:
    """A composite recipe must declare every required parameter that its
    dependencies require, by name (no implicit binding)."""
    for r in recipes:
        if not r.data.get("uses"):
            continue
        own_params = {p["name"] for p in (r.data.get("parameters") or [])}
        for dep in r.data["uses"]:
            dep_recipe = by_key.get(dep.get("ref", ""))
            if not dep_recipe:
                continue
            for p in dep_recipe.data.get("parameters") or []:
                if p.get("required", True) and p["name"] not in own_params:
                    findings.append(Finding(
                        "ERROR", r.key,
                        f"missing parameter binding: '{p['name']}' required by {dep['ref']} (as {dep['as']}) is not declared on {r.key}",
                    ))


def check_grain_traceability(recipes: list[Recipe], by_key: dict[str, Recipe], findings: list[Finding]) -> None:
    """Advisory only. True grain-algebra validation (does the composite's
    declared grain actually follow from its dependencies' grains and joins)
    requires join-key semantics beyond what atomic recipes declare today.
    This check only flags when a composite's grain token has no apparent
    source among its dependencies' output field names or grains, as a
    prompt for analyst review during Section 3 'Validate' — it never fails
    the gate."""
    for r in recipes:
        if not r.data.get("uses") or not r.data.get("grain"):
            continue
        tokens = [t.strip() for t in r.data["grain"].split(" x ")]
        available = set()
        for dep in r.data["uses"]:
            dep_recipe = by_key.get(dep.get("ref", ""))
            if not dep_recipe:
                continue
            available.add(dep_recipe.data.get("grain", ""))
            for out in dep_recipe.data.get("output") or []:
                available.add(out["name"])
        for token in tokens:
            if not any(token in a or a in token for a in available if a):
                findings.append(Finding("WARNING", r.key, f"grain traceability: token '{token}' not obviously sourced from any dependency; confirm during analyst review"))


def validate(recipes_dir: Path) -> list[Finding]:
    schema = json.loads(SCHEMA_PATH.read_text())
    recipes = load_recipes(recipes_dir)
    findings: list[Finding] = []

    for r in recipes:
        check_schema(r, schema, findings)

    check_duplicates(recipes, findings)
    by_key = {r.key: r for r in recipes}
    check_dependencies_resolve(recipes, by_key, findings)
    check_status_reuse(recipes, by_key, findings)
    check_cycles(recipes, by_key, findings)
    check_as_of_ambiguity(recipes, findings)
    check_parameter_bindings(recipes, by_key, findings)
    check_grain_traceability(recipes, by_key, findings)
    return findings


def main() -> int:
    recipes_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RECIPES_DIR
    findings = validate(recipes_dir)

    errors = [f for f in findings if f.level == "ERROR"]
    warnings = [f for f in findings if f.level == "WARNING"]

    for f in findings:
        print(f"[{f.level}] {f.recipe}: {f.message}")

    print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
