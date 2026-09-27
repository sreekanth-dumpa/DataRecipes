"""
Executes compiled SQL against the synthetic DuckDB warehouse
(data/warehouse.duckdb). This is the "execute_on_preset_compute" step of
the request-time path in Section 5, for the duckdb engine only --
Snowflake/ROSA/application compute adapters (Section 6.10) remain
unimplemented since no live Snowflake connection exists in this POC pass.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "warehouse.duckdb"


def execute(sql: str, params: dict | None = None, db_path: Path = DEFAULT_DB_PATH) -> list[dict]:
    """Runs SQL with named ($param) bindings against the warehouse,
    read-only, and returns rows as a list of dicts (JSON-serializable)."""
    if not db_path.exists():
        raise FileNotFoundError(
            f"{db_path} not found -- run `python data/generate_synthetic_data.py` first"
        )
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        result = conn.execute(sql, params or {})
        columns = [d[0] for d in result.description]
        rows = result.fetchall()
    finally:
        conn.close()
    return [dict(zip(columns, row)) for row in rows]
