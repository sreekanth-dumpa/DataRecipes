#!/usr/bin/env python3
"""
Generates a synthetic Auto + Home quote/policy warehouse into a DuckDB
file, standing in for the Quote and Policy FDPs (Section 1: "effective-
dated, replayable ground truth") for this POC pass.

Schemas/tables (matches implementation.engines.duckdb.source_table in the
five seed recipes):
  quote_fdp.quote_journey   -- both product lines; pre-derived eligibility
  quote_fdp.rated_driver    -- AUTO only, one row per journey, pre-derived
                                young_driver_segment (see docstring below)
  quote_fdp.rated_dwelling  -- HOME only, one row per journey
  policy_fdp.policy         -- both lines; single current-state row per
                                journey, with effective_date set so that a
                                point-in-time knowledge_cutoff filter
                                correctly excludes a bind that happens
                                after the cutoff (see _policy_frame)

Design note on grain: rated_driver and rated_dwelling are one row per
quote_journey, not per individual driver/dwelling detail. This matches
the seed recipes' declared grain (quote_journey) and keeps the compiler's
generated SQL a plain SELECT with no aggregation step. A real FDP would
likely expose driver-detail tables with the segment derived upstream;
that derivation is out of scope here since the point is to prove the
recipe layer, not rebuild driver rating.

Run: python data/generate_synthetic_data.py [--out data/warehouse.duckdb] [--seed 42]
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "data" / "warehouse.duckdb"

# "Extract date": the as-of moment this synthetic snapshot represents.
# Journey dates span the year before it, so a knowledge_cutoff of
# 2026-08-31 (the worked example in Section 11) falls well inside the
# closed part of the window.
EXTRACT_DATE = date(2026, 9, 15)
JOURNEY_WINDOW_DAYS = 365
DECISION_WINDOW_DAYS = 14  # journeys newer than this are still "open"

STATES = ["NC", "SC", "GA", "VA", "TN", "FL", "OH", "TX", "IL", "PA"]
CHANNELS = ["EA", "IA", "DIRECT"]
CHANNEL_WEIGHTS = [0.5, 0.35, 0.15]


def _random_dates(rng: np.random.Generator, n: int) -> np.ndarray:
    offsets = rng.integers(0, JOURNEY_WINDOW_DAYS, size=n)
    start = EXTRACT_DATE - timedelta(days=JOURNEY_WINDOW_DAYS)
    return np.array([start + timedelta(days=int(o)) for o in offsets])


def _quote_journeys(rng: np.random.Generator, product_line: str, n: int) -> pd.DataFrame:
    journey_dates = _random_dates(rng, n)
    ids = np.array([f"Q-{product_line}-{i:06d}" for i in range(n)])

    version_cutover = EXTRACT_DATE - timedelta(days=180)
    product_version = np.where(journey_dates < version_cutover, f"{product_line}-v1", f"{product_line}-v2")

    decision_window_open = np.array(
        [(EXTRACT_DATE - jd).days < DECISION_WINDOW_DAYS for jd in journey_dates]
    )

    # Decision completion date: ~14-21 days after journey creation, capped at extract date.
    completion_lag = rng.integers(DECISION_WINDOW_DAYS, DECISION_WINDOW_DAYS + 7, size=n)
    effective_date = np.array(
        [min(jd + timedelta(days=int(lag)), EXTRACT_DATE) for jd, lag in zip(journey_dates, completion_lag)]
    )

    # A small extra slice of closed-window journeys are ineligible for
    # reasons other than an open decision window (e.g. withdrawn pre-bind).
    other_ineligible = (~decision_window_open) & (rng.random(n) < 0.03)
    is_eligible = (~decision_window_open) & (~other_ineligible)
    exclusion_reason = np.select(
        [decision_window_open, other_ineligible],
        ["decision_window_incomplete", "withdrawn_pre_bind"],
        default=None,
    )

    return pd.DataFrame({
        "quote_journey_id": ids,
        "product_line": product_line,
        "product_version": product_version,
        "journey_date": journey_dates,
        "effective_date": effective_date,
        "decision_window_open": decision_window_open,
        "is_eligible": is_eligible,
        "exclusion_reason": exclusion_reason,
        "state": rng.choice(STATES, size=n),
        "channel": rng.choice(CHANNELS, size=n, p=CHANNEL_WEIGHTS),
    })


def _rated_driver_frame(rng: np.random.Generator, auto_journeys: pd.DataFrame) -> pd.DataFrame:
    n = len(auto_journeys)
    # Age distribution skewed so ~18-20% of journeys are young-driver (<25).
    age = np.round(rng.gamma(shape=2.0, scale=8.0, size=n) + 18).astype(int)
    age = np.clip(age, 16, 85)
    is_young = age < 25
    return pd.DataFrame({
        "quote_journey_id": auto_journeys["quote_journey_id"].values,
        "primary_driver_age": age,
        "is_young_driver": is_young,
        "young_driver_segment": np.where(is_young, "young_driver", "standard"),
    })


def _rated_dwelling_frame(rng: np.random.Generator, home_journeys: pd.DataFrame) -> pd.DataFrame:
    n = len(home_journeys)
    construction = rng.choice(["Frame", "Masonry", "Brick Veneer"], size=n, p=[0.55, 0.25, 0.20])
    year_built = rng.integers(1950, 2024, size=n)
    coverage = np.round(rng.normal(350_000, 120_000, size=n).clip(150_000, 900_000), -3)
    return pd.DataFrame({
        "quote_journey_id": home_journeys["quote_journey_id"].values,
        "construction_type": construction,
        "year_built": year_built,
        "roof_age_years": EXTRACT_DATE.year - year_built,
        "dwelling_coverage_amount": coverage,
    })


def _policy_frame(rng: np.random.Generator, journeys: pd.DataFrame, base_bind_rate: float,
                   young_driver_ids: set[str] | None = None) -> pd.DataFrame:
    n = len(journeys)
    eligible = journeys["is_eligible"].values
    bind_prob = np.full(n, base_bind_rate)
    if young_driver_ids is not None:
        is_young = journeys["quote_journey_id"].isin(young_driver_ids).values
        bind_prob = np.where(is_young, base_bind_rate - 0.15, base_bind_rate)

    binds = eligible & (rng.random(n) < bind_prob)
    bind_lag = rng.integers(0, 6, size=n)
    bind_date = np.array([
        min(ed + timedelta(days=int(lag)), EXTRACT_DATE) if b else pd.NaT
        for ed, lag, b in zip(journeys["effective_date"].values, bind_lag, binds)
    ])

    # effective_date here is when THIS ROW'S STATE became true: bind_date
    # for a bound journey (so a knowledge_cutoff before the bind excludes
    # it -- the late-bind-after-cutoff behavior), else the journey's own
    # effective_date (so "not bound" is true as soon as it's knowable).
    row_effective_date = np.where(binds, bind_date, journeys["effective_date"].values)

    return pd.DataFrame({
        "quote_journey_id": journeys["quote_journey_id"].values,
        "bound_policy_id": np.where(binds, "POL-" + journeys["quote_journey_id"].values, None),
        "is_bound": binds,
        "bind_date": bind_date,
        "effective_date": row_effective_date,
        "product_line": journeys["product_line"].values,
    })


def generate(n_per_line: int, seed: int) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)

    auto = _quote_journeys(rng, "AUTO", n_per_line)
    home = _quote_journeys(rng, "HOME", n_per_line)
    quote_journey = pd.concat([auto, home], ignore_index=True)

    rated_driver = _rated_driver_frame(rng, auto)
    rated_dwelling = _rated_dwelling_frame(rng, home)

    young_ids = set(rated_driver.loc[rated_driver["is_young_driver"], "quote_journey_id"])
    policy_auto = _policy_frame(rng, auto, base_bind_rate=0.62, young_driver_ids=young_ids)
    policy_home = _policy_frame(rng, home, base_bind_rate=0.68)
    policy = pd.concat([policy_auto, policy_home], ignore_index=True)

    return {
        "quote_journey": quote_journey,
        "rated_driver": rated_driver,
        "rated_dwelling": rated_dwelling,
        "policy": policy,
    }


def load_into_duckdb(frames: dict[str, pd.DataFrame], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    conn = duckdb.connect(str(out_path))
    conn.execute("CREATE SCHEMA quote_fdp")
    conn.execute("CREATE SCHEMA policy_fdp")

    quote_journey = frames["quote_journey"]  # noqa: F841 (registered for SQL below)
    rated_driver = frames["rated_driver"]  # noqa: F841
    rated_dwelling = frames["rated_dwelling"]  # noqa: F841
    policy = frames["policy"]  # noqa: F841

    conn.register("quote_journey_df", quote_journey)
    conn.register("rated_driver_df", rated_driver)
    conn.register("rated_dwelling_df", rated_dwelling)
    conn.register("policy_df", policy)

    conn.execute("CREATE TABLE quote_fdp.quote_journey AS SELECT * FROM quote_journey_df")
    conn.execute("CREATE TABLE quote_fdp.rated_driver AS SELECT * FROM rated_driver_df")
    conn.execute("CREATE TABLE quote_fdp.rated_dwelling AS SELECT * FROM rated_dwelling_df")
    conn.execute("CREATE TABLE policy_fdp.policy AS SELECT * FROM policy_df")

    conn.execute("CREATE UNIQUE INDEX idx_quote_journey_id ON quote_fdp.quote_journey (quote_journey_id)")
    conn.execute("CREATE UNIQUE INDEX idx_rated_driver_journey_id ON quote_fdp.rated_driver (quote_journey_id)")
    conn.execute("CREATE UNIQUE INDEX idx_rated_dwelling_journey_id ON quote_fdp.rated_dwelling (quote_journey_id)")
    conn.execute("CREATE UNIQUE INDEX idx_policy_journey_id ON policy_fdp.policy (quote_journey_id)")

    counts = conn.execute("""
        SELECT 'quote_journey' AS t, COUNT(*) FROM quote_fdp.quote_journey
        UNION ALL SELECT 'rated_driver', COUNT(*) FROM quote_fdp.rated_driver
        UNION ALL SELECT 'rated_dwelling', COUNT(*) FROM quote_fdp.rated_dwelling
        UNION ALL SELECT 'policy', COUNT(*) FROM policy_fdp.policy
    """).fetchall()
    conn.close()
    for table, count in counts:
        print(f"  {table}: {count:,} rows")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--n-per-line", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    frames = generate(args.n_per_line, args.seed)
    load_into_duckdb(frames, Path(args.out))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
