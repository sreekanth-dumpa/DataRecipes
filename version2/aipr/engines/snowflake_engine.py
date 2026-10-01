"""Optional live Snowflake adapter (spec sections 10.3, 12, 20).

Not exercised by the test-suite: it needs ``snowflake-connector-python`` and a
configured account.  Behaviour:

* one connection (session) per worker; statements submitted with
  ``execute_async`` and the query id persisted before waiting
* base-table reads pinned with ``AT(TIMESTAMP => source_cut_ts)`` (rendered by
  the Snowflake dialect); retention/support must be validated per deployment
* stage relations are TRANSIENT tables in a governed schema; transient is a
  table type, not a TTL, so the control store's sweeper still owns expiry
* GET_QUERY_OPERATOR_STATS is collected after completion; failures are
  recorded with a reason, never as zeros
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from ..core.hashing import fingerprint
from .base import StatementResult


class SnowflakeEngine:
    name = "snowflake"
    dialect_name = "snowflake"

    def __init__(self, source_cut_ts: str | None = None, volume_estimates: dict[str, int] | None = None):
        import snowflake.connector  # optional dependency

        self._connect = lambda: snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"], user=os.environ["SNOWFLAKE_USER"],
            authenticator=os.environ.get("SNOWFLAKE_AUTHENTICATOR", "snowflake"),
            password=os.environ.get("SNOWFLAKE_PASSWORD"), role=os.environ.get("SNOWFLAKE_ROLE"),
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"), database=os.environ.get("SNOWFLAKE_DATABASE"))
        self.source_cut_ts = source_cut_ts or os.environ["AIPR_SF_SOURCE_CUT_TS"]
        self.volume_estimates = volume_estimates or {}
        self._local = threading.local()
        self._running: dict[str, Any] = {}

    def _con(self):
        if not hasattr(self._local, "con"):
            self._local.con = self._connect()
        return self._local.con

    def fetch(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        cur = self._con().cursor()
        cur.execute(sql, {**(params or {}), "source_cut_ts": self.source_cut_ts})
        cols = [d[0].lower() for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def execute(self, sql: str, params: dict[str, Any], query_id: str) -> StatementResult:
        con = self._con()
        cur = con.cursor()
        t0 = time.perf_counter()
        cur.execute_async(sql, {**params, "source_cut_ts": self.source_cut_ts})
        sfqid = cur.sfqid
        self._running[query_id] = sfqid
        while con.is_still_running(con.get_query_status(sfqid)):
            time.sleep(0.25)
        con.get_query_status_throw_if_error(sfqid)
        self._running.pop(query_id, None)
        tele: dict[str, Any] = {"engine_query_id": sfqid}
        try:
            ops = self.fetch("SELECT operator_type, operator_statistics, execution_time_breakdown "
                             "FROM TABLE(GET_QUERY_OPERATOR_STATS(%(qid)s))", {"qid": sfqid})
            tele["operator_stats"] = ops
        except Exception as exc:
            tele["operator_stats"] = None
            tele["unavailable_reason"] = f"GET_QUERY_OPERATOR_STATS failed: {type(exc).__name__}"
        return StatementResult(query_id, (time.perf_counter() - t0) * 1000, tele)

    def interrupt(self, query_id: str) -> bool:
        sfqid = self._running.get(query_id)
        if not sfqid:
            return False
        self._con().cursor().execute("SELECT SYSTEM$CANCEL_QUERY(%(q)s)", {"q": sfqid})
        return True

    def query_status(self, query_id: str) -> str:
        sfqid = self._running.get(query_id)
        if not sfqid:
            return "unknown"
        return str(self._con().get_query_status(sfqid))

    def relation_exists(self, name: str) -> bool:
        schema, table = name.split(".", 1)
        return bool(self.fetch("SELECT 1 FROM information_schema.tables WHERE table_schema ILIKE %(s)s "
                               "AND table_name ILIKE %(t)s", {"s": schema, "t": table}))

    def drop_relation(self, name: str) -> None:
        self._con().cursor().execute(f"DROP TABLE IF EXISTS {name}")

    def source_cut(self) -> dict[str, Any]:
        return {"engine": "snowflake", "consistency_policy": "time_travel_timestamp_per_base_table",
                "relations": {t: {"mode": "time_travel", "at_timestamp": self.source_cut_ts}
                              for t in ("src_quote.quote_iteration", "src_policy.bind_event", "ref.channel_crosswalk")},
                "caveat": "a shared timestamp does not establish cross-engine transactional consistency"}

    def schema_fingerprint(self) -> str:
        rows = self.fetch("SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
                          "WHERE table_schema IN ('SRC_QUOTE','SRC_POLICY','REF') ORDER BY 1,2,3")
        return fingerprint(rows)
