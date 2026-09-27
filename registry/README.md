# Registry

Git (`recipes/`) is the authoritative store for recipe specs. Everything in this
folder is a **rebuildable projection** — never a source of truth (Section 6.3).

- `recipe.schema.json` — JSON Schema for a recipe YAML file. Structural contract
  only; semantic checks (cycles, grain/unit compatibility, join keys, as-of
  ambiguity, withdrawn refs) live in `validator/validate.py`.
- `build_index.py` — walks `recipes/**/*.yaml`, validates each against the schema,
  and builds `index.db` (SQLite) with columns: `id, version, kind, status, owner,
  deps (json), digest, path`. This is the "recipe lookup and impact graph" output
  from Section 6.3.
- `index.db` — generated. Do not hand-edit; regenerate with `build_index.py`.

Publish flow (Section 3, step 5): after validation + test + approve, run
`build_index.py` to refresh the projection and write a lockfile (`lockfiles/`)
pinning the exact transitive dependency closure for that version.
