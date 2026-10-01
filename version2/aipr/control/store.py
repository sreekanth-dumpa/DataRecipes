"""Durable control store (spec sections 3 and 10.2).

A relational store owns plan versions, execution instances, node attempts,
leases, adaptations, information-set manifests, profiles, stage relations and
the trace.  Larger artefacts (plans, receipts, request text) are also written
as immutable files under var/artifacts and referenced by content hash.
SQLite with WAL is the POC stand-in for the production relational store;
leases here are single-host.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from ..core.clock import now_iso
from ..core.hashing import canonical_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  request_id TEXT PRIMARY KEY, conversation_id TEXT, parent_request_id TEXT, subject TEXT,
  record_json TEXT NOT NULL, intent_hash TEXT, status TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS logical_plans (
  plan_id TEXT NOT NULL, version INTEGER NOT NULL, template_fingerprint TEXT NOT NULL,
  recipe_key TEXT, dimensions TEXT, body_json TEXT NOT NULL, body_hash TEXT NOT NULL,
  applicability_json TEXT, certificate_json TEXT, created_at TEXT, PRIMARY KEY (plan_id, version));
CREATE INDEX IF NOT EXISTS ix_lp_template ON logical_plans(template_fingerprint);
CREATE TABLE IF NOT EXISTS physical_plans (
  physical_id TEXT PRIMARY KEY, plan_id TEXT, logical_version INTEGER, physical_version INTEGER,
  variant TEXT, partitions INTEGER, engine TEXT, physical_fingerprint TEXT, body_json TEXT NOT NULL,
  parent_physical_id TEXT, origin TEXT, artifact_ref TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_pp_fp ON physical_plans(plan_id, physical_fingerprint);
CREATE TABLE IF NOT EXISTS executions (
  execution_id TEXT PRIMARY KEY, request_id TEXT, subject TEXT, intent_hash TEXT, semantic_fingerprint TEXT,
  coverage_fingerprint TEXT, source_cut_fingerprint TEXT, access_fingerprint TEXT, plan_id TEXT,
  initial_physical_id TEXT, current_physical_id TEXT, params_json TEXT, budget_json TEXT, context_json TEXT,
  state TEXT, priority INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0, waits_on TEXT,
  information_set_id TEXT, result_json TEXT, receipt_json TEXT, error TEXT, revisions INTEGER DEFAULT 0,
  created_at TEXT, admitted_at TEXT, started_at TEXT, finished_at TEXT);
CREATE INDEX IF NOT EXISTS ix_ex_state ON executions(state);
CREATE TABLE IF NOT EXISTS node_runs (
  execution_id TEXT, node_id TEXT, physical_id TEXT, state TEXT, attempts INTEGER DEFAULT 0,
  output_relation TEXT, observed_json TEXT, updated_at TEXT, PRIMARY KEY (execution_id, node_id));
CREATE TABLE IF NOT EXISTS node_attempts (
  attempt_id TEXT PRIMARY KEY, execution_id TEXT, node_id TEXT, physical_id TEXT, attempt_no INTEGER,
  state TEXT, lease_owner TEXT, lease_expires TEXT, query_id TEXT, output_relation TEXT,
  started_at TEXT, finished_at TEXT, observed_json TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS adaptations (
  adaptation_id TEXT PRIMARY KEY, execution_id TEXT, from_physical_id TEXT, to_physical_id TEXT,
  checkpoint_node TEXT, record_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS information_sets (
  information_set_id TEXT PRIMARY KEY, semantic_fingerprint TEXT, coverage_fingerprint TEXT,
  source_cut_fingerprint TEXT, access_fingerprint TEXT, recipe_key TEXT, dimensions TEXT,
  manifest_json TEXT NOT NULL, rows_json TEXT NOT NULL, relation_ref TEXT, status TEXT,
  producer_execution_id TEXT, created_at TEXT, expires_at TEXT);
CREATE INDEX IF NOT EXISTS ix_is_cov ON information_sets(coverage_fingerprint, source_cut_fingerprint, access_fingerprint);
CREATE TABLE IF NOT EXISTS profiles (
  profile_key TEXT PRIMARY KEY, body_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS stage_relations (
  relation TEXT PRIMARY KEY, execution_id TEXT, node_id TEXT, attempt_no INTEGER, access_fingerprint TEXT,
  created_at TEXT, expires_at TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS trace_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, span_id TEXT, parent_span_id TEXT, request_id TEXT,
  execution_id TEXT, stage TEXT, status TEXT, body_json TEXT, prev_hash TEXT, hash TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_tr_req ON trace_events(request_id);
CREATE TABLE IF NOT EXISTS feedback_cases (
  case_id TEXT PRIMARY KEY, execution_id TEXT, recipe_key TEXT, scope_json TEXT, status TEXT,
  change_class TEXT, path TEXT, created_at TEXT, updated_at TEXT);
"""


class ControlStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    # -- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.db.execute(sql, tuple(params))

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            r = self.db.execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, tuple(params)).fetchall()]

    def transaction(self):
        store = self

        class _Tx:
            def __enter__(self_inner):
                store._lock.acquire()
                store.db.execute("BEGIN IMMEDIATE")
                return store

            def __exit__(self_inner, et, ev, tb):
                try:
                    store.db.execute("ROLLBACK" if et else "COMMIT")
                finally:
                    store._lock.release()
                return False
        return _Tx()

    @staticmethod
    def j(obj: Any) -> str:
        return canonical_json(obj)

    # -- requests ----------------------------------------------------------
    def save_request(self, record: dict[str, Any], status: str, ih: str) -> None:
        self.execute("""INSERT INTO requests(request_id, conversation_id, parent_request_id, subject, record_json,
                        intent_hash, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(request_id) DO UPDATE SET record_json=excluded.record_json,
                        intent_hash=excluded.intent_hash, status=excluded.status, updated_at=excluded.updated_at""",
                     (record["request_id"], record["conversation_id"], record.get("parent_request_id"),
                      record["requester"]["subject"], json.dumps(record, default=str), ih, status,
                      record["created_at"], now_iso()))

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        r = self.one("SELECT record_json, status FROM requests WHERE request_id=?", (request_id,))
        if not r:
            return None
        rec = json.loads(r["record_json"])
        rec["_status"] = r["status"]
        return rec

    # -- executions ----------------------------------------------------------
    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        r = self.one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        if r:
            for k in ("params_json", "budget_json", "context_json", "result_json", "receipt_json"):
                r[k[:-5]] = json.loads(r[k]) if r.get(k) else None
        return r

    def set_execution(self, execution_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE executions SET {cols} WHERE execution_id=?", (*fields.values(), execution_id))

    def physical_plan(self, physical_id: str) -> dict[str, Any]:
        r = self.one("SELECT body_json FROM physical_plans WHERE physical_id=?", (physical_id,))
        if r is None:
            raise KeyError(physical_id)
        return json.loads(r["body_json"])

    def node_runs(self, execution_id: str) -> dict[str, dict[str, Any]]:
        out = {}
        for r in self.all("SELECT * FROM node_runs WHERE execution_id=?", (execution_id,)):
            r["observed"] = json.loads(r["observed_json"]) if r.get("observed_json") else {}
            out[r["node_id"]] = r
        return out

    def attempts(self, execution_id: str) -> list[dict[str, Any]]:
        rows = self.all("SELECT * FROM node_attempts WHERE execution_id=? ORDER BY started_at", (execution_id,))
        for r in rows:
            r["observed"] = json.loads(r["observed_json"]) if r.get("observed_json") else {}
        return rows
