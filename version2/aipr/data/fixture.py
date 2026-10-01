"""Synthetic, immutable source fixture standing in for the pinned source products.

Three relations model the quote and policy foundational data products:

* ``src_quote.quote_iteration`` -- several iterations per quote, drafts,
  ineligible iterations, late-recorded iterations (knowledge time != effective time)
* ``src_policy.bind_event``     -- 0..n bind events per quote, cancellations,
  late-arriving binds, binds outside the window, orphan binds
* ``ref.channel_crosswalk``     -- code crosswalk (P06) with one deliberately unmapped code

The generator is seeded, so a fixture build is reproducible and its content
fingerprint is a valid source-cut identity for the demo.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import duckdb

SOURCE_TABLES = ("src_quote.quote_iteration", "src_policy.bind_event", "ref.channel_crosswalk")

DDL = """
CREATE SCHEMA IF NOT EXISTS src_quote;
CREATE SCHEMA IF NOT EXISTS src_policy;
CREATE SCHEMA IF NOT EXISTS ref;
CREATE SCHEMA IF NOT EXISTS stage;
CREATE SCHEMA IF NOT EXISTS cache;
CREATE OR REPLACE TABLE src_quote.quote_iteration (
    quote_id VARCHAR NOT NULL,
    iteration_no INTEGER NOT NULL,
    state VARCHAR NOT NULL,
    lob VARCHAR NOT NULL,
    channel_code VARCHAR NOT NULL,
    product_version VARCHAR NOT NULL,
    issue_date DATE NOT NULL,
    status VARCHAR NOT NULL,
    eligible BOOLEAN NOT NULL,
    recorded_at TIMESTAMP NOT NULL
);
CREATE OR REPLACE TABLE src_policy.bind_event (
    bind_event_id VARCHAR NOT NULL,
    quote_id VARCHAR NOT NULL,
    event_type VARCHAR NOT NULL,
    bind_date DATE NOT NULL,
    recorded_at TIMESTAMP NOT NULL
);
CREATE OR REPLACE TABLE ref.channel_crosswalk (
    channel_code VARCHAR NOT NULL,
    channel VARCHAR NOT NULL
);
"""

CROSSWALK = [("A", "agent"), ("D", "direct"), ("W", "web")]


def _ts(d: date, hour: int = 12) -> datetime:
    return datetime.combine(d, time(hour, 0, 0))


def generate(seed: int = 20261001, n_quotes: int = 8000) -> dict[str, list[tuple]]:
    rng = random.Random(seed)
    iterations: list[tuple] = []
    binds: list[tuple] = []
    start = date(2026, 7, 1)
    span_days = 92  # Jul 1 .. Sep 30
    v32_launch = date(2026, 8, 10)
    bind_seq = 0
    for i in range(n_quotes):
        qid = f"Q{i:06d}"
        state = rng.choices(["NC", "SC", "VA"], weights=[55, 25, 20])[0]
        lob = "personal_auto" if rng.random() < 0.9 else "homeowners"
        channel = rng.choices(["A", "D", "W", "X"], weights=[45, 25, 29, 1])[0]
        first = start + timedelta(days=rng.randrange(span_days))
        n_iter = rng.choices([1, 2, 3, 4], weights=[50, 30, 15, 5])[0]
        d = first
        for it in range(1, n_iter + 1):
            status = "DRAFT" if (it == 1 and n_iter > 1 and rng.random() < 0.15) else "ISSUED"
            eligible = rng.random() > 0.05
            pv = "v3.2" if d >= v32_launch else "v3.1"
            recorded = _ts(d) + timedelta(hours=rng.choice([0, 2, 20, 30]))
            if rng.random() < 0.01:  # late-recorded iteration (knowledge lag)
                recorded += timedelta(days=rng.randrange(20, 45))
            iterations.append((qid, it, state, lob, channel, pv, d, status, eligible, recorded))
            d = d + timedelta(days=rng.randrange(1, 6))
        # conversion propensity: v3.2 and agent channel convert more often
        p = 0.22 + (0.06 if first >= v32_launch else 0.0) + (0.08 if channel == "A" else 0.0)
        if rng.random() < p:
            n_binds = rng.choices([1, 2, 3, 6], weights=[80, 14, 5, 1])[0]
            bd = first + timedelta(days=min(int(rng.gammavariate(2.0, 7.0)), 60))
            for _ in range(n_binds):
                bind_seq += 1
                rec = _ts(bd, 15)
                if rng.random() < 0.03:  # late-arriving bind
                    rec += timedelta(days=rng.randrange(15, 40))
                binds.append((f"B{bind_seq:07d}", qid, "BIND", bd, rec))
                bd = bd + timedelta(days=rng.randrange(0, 4))
            if rng.random() < 0.08:
                bind_seq += 1
                cd = bd + timedelta(days=rng.randrange(1, 20))
                binds.append((f"B{bind_seq:07d}", qid, "CANCEL", cd, _ts(cd, 16)))
    for j in range(25):  # orphan binds: no quote iteration exists
        bind_seq += 1
        bd = start + timedelta(days=rng.randrange(span_days))
        binds.append((f"B{bind_seq:07d}", f"QX{j:04d}", "BIND", bd, _ts(bd, 15)))
    return {"src_quote.quote_iteration": iterations, "src_policy.bind_event": binds,
            "ref.channel_crosswalk": list(CROSSWALK)}


def load_rows(con: duckdb.DuckDBPyConnection, rows: dict[str, list[tuple]]) -> None:
    import pandas as pd

    con.execute(DDL)
    for table, data in rows.items():
        if not data:
            continue
        cols = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema || '.' || table_name = ? "
            "ORDER BY ordinal_position", [table]).fetchall()]
        frame = pd.DataFrame(data, columns=cols)
        con.register("_load_frame", frame)
        con.execute(f"INSERT INTO {table} SELECT * FROM _load_frame")
        con.unregister("_load_frame")


def edge_case_rows(path: Path) -> dict[str, list[tuple]]:
    """Hand-labelled adversarial fixture (fixtures/edge_cases.json)."""
    spec = json.loads(Path(path).read_text())
    its = [(r["quote_id"], r["iteration_no"], r["state"], r["lob"], r["channel_code"],
            r["product_version"], date.fromisoformat(r["issue_date"]), r["status"], r["eligible"],
            datetime.fromisoformat(r["recorded_at"])) for r in spec["quote_iteration"]]
    bs = [(r["bind_event_id"], r["quote_id"], r["event_type"], date.fromisoformat(r["bind_date"]),
           datetime.fromisoformat(r["recorded_at"])) for r in spec["bind_event"]]
    return {"src_quote.quote_iteration": its, "src_policy.bind_event": bs,
            "ref.channel_crosswalk": list(CROSSWALK)}


def build_warehouse(path: Path, rows: dict[str, list[tuple]] | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    try:
        load_rows(con, rows if rows is not None else generate())
    finally:
        con.close()
    return path


def table_fingerprint(con: Any, table: str) -> dict[str, Any]:
    """Content fingerprint of an immutable fixture table (the demo's source cut)."""
    n, digest = con.execute(
        f"SELECT COUNT(*), md5(COALESCE(string_agg(x, '|' ORDER BY x), '')) "
        f"FROM (SELECT CAST(t AS VARCHAR) AS x FROM {table} t)").fetchone()
    return {"mode": "fixture_content_hash", "rows": int(n), "content_md5": digest}


def schema_fingerprint(con: Any) -> list[tuple]:
    return [tuple(r) for r in con.execute(
        "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema IN ('src_quote','src_policy','ref') ORDER BY 1,2,3").fetchall()]
