"""
Streamlit Builder UI (Section 6.2) + agentic test window (Section 6.4/6.7).

Two tabs:
  - Builder: browse the registry, inspect a recipe, draft a new one and
    validate it against the live dependency graph via the FastAPI backend.
  - Agentic test window: run a recipe's declared fixtures via
    /recipes/test, and a placeholder for LLM-assisted authoring
    (Portkey -> Claude), which is not wired to a live key in this POC pass.

Run: streamlit run ui/app.py
Requires the backend running at BUILDER_API_URL (default http://localhost:8000).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import requests
import streamlit as st
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from registry.build_index import DEFAULT_DB_PATH  # noqa: E402

API_URL = os.environ.get("BUILDER_API_URL", "http://localhost:8000")
RECIPES_DIR = REPO_ROOT / "recipes"

st.set_page_config(page_title="DataRecipes Builder", layout="wide")
st.title("DataRecipes Builder — POC")
st.caption("Just-in-Time Derived Data Product Architecture v2 · Iteration 1")

tab_builder, tab_test = st.tabs(["Builder", "Agentic test window"])


def load_registry_rows() -> list[tuple]:
    if not DEFAULT_DB_PATH.exists():
        return []
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    rows = conn.execute("SELECT id, version, kind, status, owner, grain FROM recipes ORDER BY id").fetchall()
    conn.close()
    return rows


with tab_builder:
    st.subheader("Registry")
    rows = load_registry_rows()
    if not rows:
        st.warning("No index found. Run `python registry/build_index.py` first.")
    else:
        st.dataframe(
            [{"id": r[0], "version": r[1], "kind": r[2], "status": r[3], "owner": r[4], "grain": r[5]} for r in rows],
            width="stretch",
        )

    st.divider()
    st.subheader("Inspect a recipe")
    yaml_paths = sorted(RECIPES_DIR.rglob("*.yaml"))
    chosen = st.selectbox("Recipe file", options=yaml_paths, format_func=lambda p: str(p.relative_to(RECIPES_DIR)))
    if chosen:
        st.code(chosen.read_text(), language="yaml")

    st.divider()
    st.subheader("Draft a new recipe")
    st.caption("Section 3, step 1 (Draft): business intent, grain, time semantics, output contract.")
    draft_yaml = st.text_area(
        "Recipe YAML",
        height=280,
        value=(
            "id: quote.example_recipe\n"
            "version: \"1.0\"\n"
            "kind: attribute\n"
            "status: draft\n"
            "owner: \n"
            "description: \n"
            "grain: quote_journey\n"
            "output:\n"
            "  - name: value\n"
            "    type: string\n"
        ),
    )
    if st.button("Validate draft"):
        try:
            draft = yaml.safe_load(draft_yaml)
        except yaml.YAMLError as e:
            st.error(f"Invalid YAML: {e}")
        else:
            try:
                resp = requests.post(f"{API_URL}/recipes/validate", json={"recipe": draft}, timeout=10)
                resp.raise_for_status()
                result = resp.json()
                if result["errors"]:
                    st.error("\n".join(result["errors"]))
                else:
                    st.success("No validation errors.")
                if result["warnings"]:
                    st.warning("\n".join(result["warnings"]))
                st.json(result)
            except requests.RequestException as e:
                st.error(f"Could not reach builder API at {API_URL}: {e}")

with tab_test:
    st.subheader("Run declared fixtures")
    st.caption("Section 6.4: analyst test workbench. POC status: resolves fixtures via /recipes/test; does not execute against live Snowflake yet.")
    recipe_key = st.text_input("Recipe key (id@version)", value="quote.bind_rate_by_young_driver@1.0")
    if st.button("Run test suite"):
        try:
            resp = requests.post(f"{API_URL}/recipes/test", json={"recipe_key": recipe_key}, timeout=10)
            resp.raise_for_status()
            st.json(resp.json())
        except requests.RequestException as e:
            st.error(f"Could not reach builder API at {API_URL}: {e}")

    st.divider()
    st.subheader("Agentic authoring assistant")
    st.caption("LLM-assisted candidate definitions and source bindings (Section 3), routed through Portkey to Claude. Not wired to a live key in this POC pass -- prompt is captured for later wiring.")
    st.text_area("Ask the agent to draft or explain a recipe", placeholder="e.g. Draft a recipe for average premium by product version...")
    st.button("Ask agent", disabled=True, help="Portkey/Anthropic key not configured in this session")
