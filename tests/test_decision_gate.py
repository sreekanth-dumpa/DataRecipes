"""
Proves the Iteration 1 decision gate (Section 12): "Five Quote/Policy
recipes compose without ambiguous grain or time."

Run: pytest tests/ -v
"""
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from validator.validate import validate, load_recipes  # noqa: E402
from compiler.sql_compiler import compile_recipe  # noqa: E402

RECIPES_DIR = REPO_ROOT / "recipes"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

EXPECTED_RECIPE_KEYS = {
    "quote.eligible_journey@2.1",
    "policy.quote_to_bound_policy@4.0",
    "quote.young_driver_indicator@1.4",
    "quote.product_version_at_quote_time@1.0",
    "quote.bind_rate_by_young_driver@1.0",
}


def test_exactly_five_seed_recipes_present():
    recipes = load_recipes(RECIPES_DIR)
    keys = {r.key for r in recipes}
    assert keys == EXPECTED_RECIPE_KEYS, f"seed recipe set changed: {keys}"


def test_no_validation_errors():
    """The hard gate: zero ERROR-level findings across the seed set."""
    findings = validate(RECIPES_DIR)
    errors = [f for f in findings if f.level == "ERROR"]
    assert errors == [], "\n".join(f"{f.recipe}: {f.message}" for f in errors)


def test_every_recipe_declares_unambiguous_time_semantics_when_time_sensitive():
    """Direct check on the 'ambiguous grain or time' half of the gate: any
    recipe with a cutoff-style parameter must declare as_of_behavior."""
    for path in RECIPES_DIR.rglob("*.yaml"):
        data = yaml.safe_load(path.read_text())
        params = {p["name"] for p in (data.get("parameters") or [])}
        time_sensitive = any("cutoff" in p for p in params)
        if time_sensitive:
            ts = data.get("time_semantics") or {}
            assert ts.get("as_of_behavior"), f"{data['id']}: time-sensitive but as_of_behavior not declared"


def test_composite_recipe_resolves_exact_dependency_set():
    """The 'compose' half of the gate: the composite metric's dependency
    closure is exactly the three atomics the spec (Section 2) names."""
    recipes = {r.key: r for r in load_recipes(RECIPES_DIR)}
    composite = recipes["quote.bind_rate_by_young_driver@1.0"]
    deps = {d["ref"] for d in composite.data["uses"]}
    assert deps == {
        "quote.eligible_journey@2.1",
        "policy.quote_to_bound_policy@4.0",
        "quote.young_driver_indicator@1.4",
    }


def test_every_declared_fixture_exists():
    for path in RECIPES_DIR.rglob("*.yaml"):
        data = yaml.safe_load(path.read_text())
        for fixture_name in data.get("tests", []):
            fixture_path = FIXTURES_DIR / f"{fixture_name}.yaml"
            assert fixture_path.exists(), f"{data['id']}: declared test '{fixture_name}' has no fixture file"


@pytest.mark.parametrize("recipe_key", sorted(EXPECTED_RECIPE_KEYS))
def test_every_seed_recipe_compiles_to_sql(recipe_key):
    """Compilation is a second, independent proof that the DAG is
    well-formed: it requires resolving every join and expression."""
    sql = compile_recipe(recipe_key)
    assert "SELECT" in sql.upper()
    assert "TODO: no source_table declared" not in sql, f"{recipe_key}: missing source_table mapping"
