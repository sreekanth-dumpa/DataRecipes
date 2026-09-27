"""
FastAPI backend for the recipe builder (Section 9, Iteration 1 subset).

Iteration 1 exposes only /recipes/validate and /recipes/test. The
request-time endpoints (/patterns/search, /requests/resolve,
/plans/execute, /results/{id}) belong to iterations 2-4 and are not
implemented here.

Run: uvicorn builder.api.main:app --reload --port 8000
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from validator.validate import validate, load_recipes  # noqa: E402
from registry.build_index import DEFAULT_DB_PATH  # noqa: E402

RECIPES_DIR = REPO_ROOT / "recipes"

app = FastAPI(
    title="DataRecipes Builder API",
    description="Iteration 1: recipe validation and test-run endpoints per Section 9.",
    version="0.1.0-poc",
)


# ---------------------------------------------------------------------------
# POST /recipes/validate
# ---------------------------------------------------------------------------

class ValidateRequest(BaseModel):
    recipe: dict = Field(..., description="Draft recipe, as it would appear in a recipes/*.yaml file.")


class ValidateResponse(BaseModel):
    errors: list[str]
    warnings: list[str]
    resolved_grain: str | None
    impacted_assets: list[str] = Field(
        default_factory=list,
        description="Recipes in the registry that would be affected if this recipe's dependencies change (from impact_graph). Empty until index.db exists.",
    )


@app.post("/recipes/validate", response_model=ValidateResponse)
def validate_recipe(req: ValidateRequest) -> ValidateResponse:
    """Validates a draft recipe against the current committed recipe set,
    without writing it to recipes/. Writes the draft to a temp copy of the
    recipes directory so cross-recipe checks (cycles, dependency
    resolution) run against the real dependency graph."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for path in RECIPES_DIR.rglob("*.yaml"):
            dest = tmp_dir / path.relative_to(RECIPES_DIR)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(path.read_text())

        draft_id = req.recipe.get("id", "draft")
        draft_path = tmp_dir / "_draft" / f"{draft_id.replace('.', '_')}.yaml"
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(yaml.safe_dump(req.recipe, sort_keys=False))

        findings = validate(tmp_dir)

    draft_key_prefix = f"{draft_id}@"
    own_findings = [f for f in findings if f.recipe.startswith(draft_key_prefix) or f.recipe == draft_id]

    impacted_assets: list[str] = []
    if DEFAULT_DB_PATH.exists():
        conn = sqlite3.connect(DEFAULT_DB_PATH)
        rows = conn.execute(
            "SELECT DISTINCT dependent_id || '@' || dependent_version FROM impact_graph WHERE dep_id = ?",
            (draft_id,),
        ).fetchall()
        conn.close()
        impacted_assets = [r[0] for r in rows]

    return ValidateResponse(
        errors=[f.message for f in own_findings if f.level == "ERROR"],
        warnings=[f.message for f in own_findings if f.level == "WARNING"],
        resolved_grain=req.recipe.get("grain"),
        impacted_assets=impacted_assets,
    )


# ---------------------------------------------------------------------------
# POST /recipes/test
# ---------------------------------------------------------------------------

class TestRequest(BaseModel):
    recipe_key: str = Field(..., description="id@version, e.g. quote.bind_rate_by_young_driver@1.0")
    test_suite: list[str] | None = Field(None, description="Fixture names to run; defaults to all fixtures declared on the recipe.")
    input_cutoff: str | None = Field(None, description="Knowledge cutoff date (ISO 8601) to run fixtures against.")


class TestCaseResult(BaseModel):
    fixture: str
    status: str
    diff: str | None = None


class TestResponse(BaseModel):
    run_id: str
    cases: list[TestCaseResult]
    controls: list[str]


@app.post("/recipes/test", response_model=TestResponse)
def test_recipe(req: TestRequest) -> TestResponse:
    """Section 6.4: analyst test workbench. POC status: this endpoint
    resolves which fixtures a recipe declares and returns a run record,
    but does not execute them against live Snowflake data -- no compute
    connection is wired up yet. Status is reported honestly as
    'not_executed' rather than faking a pass/fail."""
    recipes = load_recipes(RECIPES_DIR)
    match = next((r for r in recipes if r.key == req.recipe_key), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"recipe not found: {req.recipe_key}")

    declared_tests = match.data.get("tests", [])
    suite = req.test_suite if req.test_suite is not None else declared_tests
    unknown = [t for t in suite if t not in declared_tests]

    cases = [
        TestCaseResult(
            fixture=name,
            status="unknown_fixture" if name in unknown else "not_executed",
            diff=None,
        )
        for name in suite
    ]

    return TestResponse(
        run_id=f"run_{uuid4().hex[:12]}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        cases=cases,
        controls=["no live Snowflake connection configured -- fixtures are resolved but not executed"],
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
