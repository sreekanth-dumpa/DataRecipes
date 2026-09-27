# DataRecipes

Just-in-Time Derived Data Product Architecture — Iteration 1 (Recipe schema,
nested references, Git registry, lockfile, static validator), now with a
real synthetic warehouse and real execution.

Source spec: *Just in Time Derived Data Product Architecture v2* (26 Sep 2026).
POC scope and resolved decisions are in Section 13 of that document.

## Iteration 1 scope

- Recipe kinds: Attribute/resolution, Population, Metric/KPI (SQL-expressible)
- FDPs: Quote, Policy, covering Auto and Home product lines
- Compute: **DuckDB** (executable now) + Snowflake declared for later cutover, per recipe
- Decision gate: **five Quote/Policy recipes compose without ambiguous grain or time** — proved by `tests/test_decision_gate.py`
- Execution proof: compiled recipe SQL runs for real against a synthetic warehouse and reconciles against an independently-written query — proved by `tests/test_execution.py`
- Out of scope for this pass: Feature/model and Process recipe kinds, Python/Lambda execution, Okta-integrated review roles, retention policy, a live Snowflake connection

## Layout

```
recipes/                canonical Git-stored recipe specs (source of truth)
registry/                rebuildable projection: JSON Schema, SQLite index builder, lockfile writer
validator/                static, deterministic validation (cycles, grain/time ambiguity, param bindings)
compiler/                recipe closure -> parameterized SQL, per engine (duckdb executable, snowflake declared)
builder/api/              FastAPI backend: /recipes/validate, /recipes/test (real execution when params given)
builder/db/               DuckDB execution against data/warehouse.duckdb
builder/context_adapter/  Section 6.1 source descriptors: OSIContextAdapter (real, OSI-backed) + StubContextAdapter (fallback)
context/osi/              OSI (Open Semantic Interchange / Apache Ossie) context for the warehouse -- schema-validated
data/                     synthetic Auto+Home warehouse generator + the generated .duckdb file
ui/                       Streamlit Builder UI + test window (runs real queries against the warehouse)
tests/                    decision-gate proof, execution/reconciliation proof, fixtures for declared tests
lockfiles/                resolved dependency closures, written at publish time
```

## The synthetic warehouse

`data/generate_synthetic_data.py` builds a DuckDB file with realistic Auto
and Home data (~20,000 quote journeys per line, reproducible with a fixed
seed):

- `quote_fdp.quote_journey` — both lines; pre-derived eligibility based on
  decision-window closure as of `effective_date`
- `quote_fdp.rated_driver` — Auto only, pre-derived young-driver segment
  (~19% young-driver rate, calibrated)
- `quote_fdp.rated_dwelling` — Home only, construction/year/coverage
- `policy_fdp.policy` — both lines; single current-state row per journey,
  with `effective_date` set so a `knowledge_cutoff` before a bind correctly
  excludes it (proved in `tests/test_execution.py::test_late_bind_after_cutoff_is_really_excluded`)

`data/warehouse.duckdb` is committed (~7MB) so a fresh checkout runs
immediately; `tests/conftest.py` regenerates it automatically if missing.

## The OSI context

`context/osi/quote_policy_auto_home.yaml` is a genuine [Open Semantic
Interchange](https://github.com/open-semantic-interchange/OSI) (Apache
Ossie Core Metadata Spec) document describing the four warehouse tables,
their relationships, and four cross-dataset metrics — schema-validated
against OSI's own published JSON Schema. `builder/context_adapter/osi_adapter.py`
reads it to produce the Section 6.1 source descriptors, replacing the
earlier hardcoded stub for these tables.

## Running it

```bash
pip install -r requirements.txt --break-system-packages

# 1. Generate the synthetic warehouse (or skip -- data/warehouse.duckdb is committed)
python data/generate_synthetic_data.py

# 2. Validate the recipe set
python validator/validate.py

# 3. Build the registry projection (refuses if validation fails)
python registry/build_index.py

# 4. Pin a lockfile for a published version
python registry/write_lockfile.py quote.bind_rate_by_young_driver@1.0

# 5. Compile a recipe to SQL for a given engine
python compiler/sql_compiler.py quote.bind_rate_by_young_driver@1.0 --engine duckdb

# 6. Prove the decision gate + real execution/reconciliation
pytest tests/ -v

# 7. Run the backend + UI
uvicorn builder.api.main:app --reload --port 8000
BUILDER_API_URL=http://localhost:8000 streamlit run ui/app.py
```

Example: run a recipe for real via the API --

```bash
curl -s localhost:8000/recipes/test -X POST -H 'content-type: application/json' -d '{
  "recipe_key": "quote.bind_rate_by_young_driver@1.0",
  "params": {"cohort_start": "2026-07-01", "cohort_end": "2026-07-31", "knowledge_cutoff": "2026-08-31", "product_line": "AUTO"}
}'
```

## Known gaps (by design, for this pass)

- **No live Snowflake connection.** The `snowflake` engine binding is declared
  on every recipe and the compiler emits valid Snowflake-dialect SQL for it,
  but nothing executes against it. DuckDB is the only executable engine.
- **`/recipes/test` fixtures aren't staged into the warehouse.** Without
  `params`, it resolves declared fixtures (`tests/fixtures/*.yaml`) but
  reports `not_executed` — their small fabricated IDs aren't loaded into
  `data/warehouse.duckdb`. With `params`, it runs the compiled recipe for
  real against the actual warehouse.
- **No entitlement/access model.** Runs under the creator's own permissions per
  the POC decision (Section 13).
- **Product-line leakage, found and fixed during this pass:** `quote.eligible_journey`
  originally had no `product_line` filter, so a Home journey with no
  `rated_driver` row leaked into `quote.bind_rate_by_young_driver`'s grain as
  a null-segment bucket once run against real multi-line data. Both recipes
  now require a `product_line` parameter.
