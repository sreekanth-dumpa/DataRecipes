"""Shared pytest fixtures: ensures the synthetic warehouse exists before
execution-dependent tests run, generating it with the fixed default seed
if missing (keeps a fresh checkout runnable without a manual step, while
the committed data/warehouse.duckdb is the fast path)."""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from data.generate_synthetic_data import generate, load_into_duckdb, DEFAULT_OUT  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def ensure_warehouse():
    if not DEFAULT_OUT.exists():
        frames = generate(n_per_line=20_000, seed=42)
        load_into_duckdb(frames, DEFAULT_OUT)
    yield DEFAULT_OUT
