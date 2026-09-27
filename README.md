# DataRecipes

Just-in-Time Derived Data Product Architecture — Iteration 1 (Recipe schema,
nested references, Git registry, lockfile, static validator).

Source spec: *Just in Time Derived Data Product Architecture v2* (26 Sep 2026).
POC scope and resolved decisions are in Section 13 of that document.

## Iteration 1 scope

- Recipe kinds: Attribute/resolution, Population, Metric/KPI (SQL-expressible on Snowflake only)
- FDPs: Quote, Policy
- Decision gate: **five Quote/Policy recipes compose without ambiguous grain or time** — proved by `tests/test_decision_gate.py`
- Out of scope for this pass: Feature/model and Process recipe kinds, Python/Lambda execution, Okta-integrated review roles, retention policy, live Snowflake execution, real Kairos context folder (context adapter is stubbed — see `builder/context_adapter/adapter.py`)

## Layout

```
recipes/            canonical Git-stored recipe specs (source of truth)
registry/           rebuildable projection: JSON Schema, SQLite index builder, lockfile writer
validator/          static, deterministic validation (cycles, grain/time ambiguity, param bindings)
compiler/           recipe closure -> parameterized Snowflake SQL (text only, not executed)
builder/api/        FastAPI backend: /recipes/validate, /recipes/test
builder/context_adapter/   stubbed Section 6.1 source descriptors
ui/                 Streamlit Builder UI + agentic test window
tests/              pytest decision-gate proof + fixtures for each recipe's declared tests
lockfiles/          resolved dependency closures, written at publish time
```

## Running it

```bash
pip install -r requirements.txt --break-system-packages

# 1. Validate the recipe set
python validator/validate.py

# 2. Build the registry projection (refuses if validation fails)
python registry/build_index.py

# 3. Pin a lockfile for a published version
python registry/write_lockfile.py quote.bind_rate_by_young_driver@1.0

# 4. Compile a recipe to SQL (text only -- no Snowflake connection wired yet)
python compiler/sql_compiler.py quote.bind_rate_by_young_driver@1.0

# 5. Prove the decision gate
pytest tests/ -v

# 6. Run the backend + UI
uvicorn builder.api.main:app --reload --port 8000
BUILDER_API_URL=http://localhost:8000 streamlit run ui/app.py
```

## Known gaps (by design, for this pass)

- **Context adapter is stubbed.** `builder/context_adapter/adapter.py` returns
  hardcoded source descriptors for the tables the five seed recipes use.
  Swap in a real implementation once the Kairos context folder path is available.
- **Compiler emits SQL text only.** No Snowflake connection is configured;
  `/recipes/test` resolves fixtures but reports `not_executed` rather than a
  fabricated pass/fail.
- **No entitlement/access model.** Runs under the creator's own permissions per
  the POC decision (Section 13).
