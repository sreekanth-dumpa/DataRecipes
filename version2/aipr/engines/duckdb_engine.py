"""Local DuckDB engine over the immutable fixture (the POC's Snowflake stand-in).

Each worker uses its own cursor (an independent session onto the same
database).  Stage relations are ordinary tables in the ``stage`` schema with
unique per-execution/attempt names; their lifecycle is tracked by the control
store and a sweeper, because a table type is not a TTL.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import duckdb

from ..core.hashing import fingerprint
from ..data.fixture import SOURCE_TABLES, schema_fingerprint, table_fingerprint
from .base import StatementResult


def _bind(sql: str, params: dict[str, Any] | None) -> dict[str, Any]:
    names = set(re.findall(r"\$(\w+)", sql))
    return {k: v for k, v in (params or {}).items() if k in names}


class DuckDBEngine:
    name = "duckdb"
    dialect_name = "duckdb"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.con = duckdb.connect(str(self.path))
        self.con.execute("CREATE SCHEMA IF NOT EXISTS stage; CREATE SCHEMA IF NOT EXISTS cache;")
        self._running: dict[str, Any] = {}
        self._done: dict[str, str] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self.con.close()

    def fetch(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        cur = self.con.cursor()
        try:
            rel = cur.execute(sql, _bind(sql, params))
            cols = [d[0] for d in rel.description]
            return [dict(zip(cols, row)) for row in rel.fetchall()]
        finally:
            cur.close()

    def execute(self, sql: str, params: dict[str, Any], query_id: str) -> StatementResult:
        cur = self.con.cursor()
        with self._lock:
            self._running[query_id] = cur
        t0 = time.perf_counter()
        try:
            cur.execute(sql, _bind(sql, params))
            with self._lock:
                self._done[query_id] = "succeeded"
        except Exception:
            with self._lock:
                self._done[query_id] = "failed"
            raise
        finally:
            with self._lock:
                self._running.pop(query_id, None)
            cur.close()
        elapsed = (time.perf_counter() - t0) * 1000
        return StatementResult(query_id, elapsed, telemetry={
            "bytes_scanned": None, "spill_local_bytes": None, "spill_remote_bytes": None,
            "unavailable_reason": "local DuckDB engine does not expose per-query scan/spill telemetry"})

    def interrupt(self, query_id: str) -> bool:
        with self._lock:
            cur = self._running.get(query_id)
        if cur is None:
            return False
        try:
            cur.interrupt()
        except Exception:
            return False
        return True

    def query_status(self, query_id: str) -> str:
        with self._lock:
            if query_id in self._running:
                return "running"
            return self._done.get(query_id, "unknown")

    def relation_exists(self, name: str) -> bool:
        schema, table = name.split(".", 1)
        return bool(self.fetch("SELECT 1 FROM information_schema.tables WHERE table_schema = $s AND table_name = $t",
                               {"s": schema, "t": table}))

    def drop_relation(self, name: str) -> None:
        schema, table = name.split(".", 1)
        if schema not in ("stage", "cache") or not re.fullmatch(r"[a-z0-9_]+", table):
            raise ValueError(f"refusing to drop {name}")
        cur = self.con.cursor()
        try:
            cur.execute(f"DROP TABLE IF EXISTS {schema}.{table}")
        finally:
            cur.close()

    def source_cut(self) -> dict[str, Any]:
        cur = self.con.cursor()
        try:
            vector = {t: table_fingerprint(cur, t) for t in SOURCE_TABLES}
        finally:
            cur.close()
        return {"engine": "duckdb", "consistency_policy": "single_immutable_fixture_file",
                "file": self.path.name, "relations": vector}

    def schema_fingerprint(self) -> str:
        cur = self.con.cursor()
        try:
            return fingerprint(schema_fingerprint(cur))
        finally:
            cur.close()
