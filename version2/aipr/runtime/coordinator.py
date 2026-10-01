"""Always-running online DAG coordinator (spec section 10.3 / 10.4).

* Accepts compiled graphs *as data* (persisted physical plan versions); no DAG
  files, cron triggers or batch windows.
* Durable state (node_runs / node_attempts in the control store) is the
  recovery authority; the in-memory event queue only reduces dispatch latency.
* Ready nodes are leased and handed to a bounded pool of warm workers
  immediately; workers persist the query id *before* awaiting completion.
* Completion events wake the coordinator, which validates outputs, admits the
  relation, considers suffix adaptation at a checkpoint, then dispatches
  newly-ready dependents.  A reconciliation poll runs as a fallback.
* Retries use new attempt ids; after an interruption, a recorded attempt whose
  CTAS committed is reconciled rather than resubmitted.
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..compiler.ast_validator import validate_sql
from ..compiler.dialect import SOURCE_ALLOWLIST, get_dialect
from ..compiler.physical import topo_order
from ..control.registry import PlanRegistry
from ..control.store import ControlStore
from ..core.clock import now_iso, utcnow
from ..core.config import Settings
from ..core.ids import new_id
from ..engines.python_ops import run_operator
from ..trace.tracer import Tracer
from . import adaptation
from .validation import envelope_check, run_validations

TERMINAL = {"complete", "failed", "cancelled"}
ACTIVE_NODE = {"reserved", "submitted", "running", "validating"}
MAX_ATTEMPTS = 2


def _restore_params(raw: dict[str, Any]) -> dict[str, Any]:
    from datetime import date
    out = dict(raw)
    for k in ("cohort_start", "cohort_end", "cutoff_date"):
        if isinstance(out.get(k), str):
            out[k] = date.fromisoformat(out[k])
    if isinstance(out.get("cutoff_ts"), str):
        out["cutoff_ts"] = datetime.fromisoformat(out["cutoff_ts"])
    return out


class Coordinator:
    def __init__(self, store: ControlStore, engine: Any, tracer: Tracer, registry: PlanRegistry,
                 settings: Settings, finalizer: Callable[[str], None]):
        self.store, self.engine, self.tracer, self.registry = store, engine, tracer, registry
        self.settings = settings
        self.finalizer = finalizer
        self.dialect = get_dialect(engine.dialect_name)
        self.owner = new_id("coord")
        self.events: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self.pool = ThreadPoolExecutor(max_workers=settings.workers, thread_name_prefix="aipr-worker")
        self.inflight: dict[tuple[str, str], str] = {}
        self._inflight_lock = threading.Lock()
        self.dispatch_latency_ms: list[float] = []

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self.recover()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="aipr-coordinator", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self._running = False
        self._wake.set()
        if self._thread and wait:
            self._thread.join(timeout=5)
        self.pool.shutdown(wait=wait, cancel_futures=True)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while self._running:
            self._wake.wait(timeout=0.25)  # reconciliation poll fallback
            self._wake.clear()
            try:
                self.tick()
            except Exception:  # the coordinator must keep running; failures are recorded per execution
                traceback.print_exc()

    # -- recovery --------------------------------------------------------------
    def recover(self) -> list[dict[str, Any]]:
        """Reconcile attempts left in flight by a previous coordinator."""
        actions = []
        for exe in self.store.all("SELECT execution_id, current_physical_id FROM executions WHERE state IN ('running','finalizing')"):
            if exe.get("current_physical_id") is None:
                continue
            for nr in self.store.all("SELECT * FROM node_runs WHERE execution_id=? AND state IN ('reserved','submitted','running','validating')",
                                     (exe["execution_id"],)):
                att = self.store.one("SELECT * FROM node_attempts WHERE execution_id=? AND node_id=? ORDER BY attempt_no DESC LIMIT 1",
                                     (exe["execution_id"], nr["node_id"]))
                rel = att and att.get("output_relation")
                if att and att.get("query_id") and rel and self.engine.relation_exists(rel):
                    self.store.execute("UPDATE node_attempts SET state='reconciled' WHERE attempt_id=?", (att["attempt_id"],))
                    self.store.execute("UPDATE node_runs SET state='running', output_relation=? WHERE execution_id=? AND node_id=?",
                                       (rel, exe["execution_id"], nr["node_id"]))
                    self.events.put(("done", exe["execution_id"], nr["node_id"], att["attempt_id"], rel,
                                     {"elapsed_ms": None, "reconciled": True, "query_id": att["query_id"]}))
                    actions.append({"execution_id": exe["execution_id"], "node_id": nr["node_id"], "action": "reconciled_committed_output",
                                    "query_id": att["query_id"]})
                else:
                    if att:
                        self.store.execute("UPDATE node_attempts SET state='orphaned', finished_at=? WHERE attempt_id=?",
                                           (now_iso(), att["attempt_id"]))
                        if rel:
                            self.store.execute("UPDATE stage_relations SET status='orphaned' WHERE relation=?", (rel,))
                    self.store.execute("UPDATE node_runs SET state='pending' WHERE execution_id=? AND node_id=?",
                                       (exe["execution_id"], nr["node_id"]))
                    actions.append({"execution_id": exe["execution_id"], "node_id": nr["node_id"], "action": "requeued_new_attempt"})
            if exe.get("execution_id"):
                self.tracer.emit(self._trace_id(exe["execution_id"]), "recovery", "reconciled",
                                 {"actions": [a for a in actions if a["execution_id"] == exe["execution_id"]],
                                  "coordinator": self.owner}, execution_id=exe["execution_id"])
        return actions

    # -- main tick -------------------------------------------------------------
    def tick(self) -> None:
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(ev)
        self._admit_queued()
        for exe in self.store.all("SELECT execution_id FROM executions WHERE state='running'"):
            self._advance(exe["execution_id"])

    def _admit_queued(self) -> None:
        running = self.store.one("SELECT COUNT(*) AS n FROM executions WHERE state='running'")["n"]
        free = self.settings.max_concurrent_executions - running
        if free <= 0:
            return
        for exe in self.store.all("SELECT execution_id FROM executions WHERE state='queued' ORDER BY priority DESC, created_at LIMIT ?",
                                  (free,)):
            self.store.set_execution(exe["execution_id"], state="running", started_at=now_iso())
            self.tracer.emit(self._trace_id(exe["execution_id"]), "execute", "admitted_from_queue", {},
                             execution_id=exe["execution_id"])

    def _trace_id(self, execution_id: str) -> str:
        r = self.store.one("SELECT context_json FROM executions WHERE execution_id=?", (execution_id,))
        return json.loads(r["context_json"])["trace_id"] if r and r.get("context_json") else execution_id

    def _advance(self, execution_id: str) -> None:
        exe = self.store.get_execution(execution_id)
        if exe is None or exe["state"] != "running":
            return
        if exe["cancel_requested"]:
            self._cancel(exe, "cancel requested")
            return
        budget = exe["budget"] or {}
        if exe["started_at"]:
            started = datetime.fromisoformat(exe["started_at"])
            if utcnow() - started > timedelta(seconds=budget.get("max_runtime_seconds", 900)):
                self._cancel(exe, "max_runtime_seconds exceeded")
                return
        plan = self.store.physical_plan(exe["current_physical_id"])
        runs = self.store.node_runs(execution_id)
        if any(r["state"] == "failed" for r in runs.values()):
            self._fail(exe, "a node failed: " + ", ".join(k for k, r in runs.items() if r["state"] == "failed"))
            return
        if all(runs.get(n["node_id"], {}).get("state") == "complete" for n in plan["nodes"]):
            self.store.set_execution(execution_id, state="finalizing")
            try:
                self.finalizer(execution_id)
            except Exception as exc:
                traceback.print_exc()
                self._fail(self.store.get_execution(execution_id), f"finalization failed: {type(exc).__name__}: {exc}")
            return
        order = {nid: i for i, nid in enumerate(topo_order(plan))}
        ready = [n for n in plan["nodes"]
                 if runs.get(n["node_id"], {}).get("state") == "pending"
                 and all(runs.get(d, {}).get("state") == "complete" for d in n["depends_on"])]
        ready.sort(key=lambda n: order[n["node_id"]])
        for node in ready:
            with self._inflight_lock:
                if len(self.inflight) >= self.settings.workers:
                    break
            self._dispatch(exe, plan, node, runs)

    # -- dispatch --------------------------------------------------------------
    def _dispatch(self, exe: dict[str, Any], plan: dict[str, Any], node: dict[str, Any],
                  runs: dict[str, dict[str, Any]]) -> None:
        eid, nid = exe["execution_id"], node["node_id"]
        attempt_no = runs[nid]["attempts"] + 1
        attempt_id = new_id("att")
        relation = f"stage.s_{eid.split('_')[-1]}_{nid}_a{attempt_no}" if node["kind"] == "sql" else None
        lease_exp = (utcnow() + timedelta(seconds=self.settings.lease_seconds)).isoformat()
        with self.store.transaction() as s:
            cur = s.db.execute("UPDATE node_runs SET state='reserved', attempts=?, updated_at=? WHERE execution_id=? "
                               "AND node_id=? AND state='pending'", (attempt_no, now_iso(), eid, nid))
            if cur.rowcount != 1:
                return  # someone else holds it
            s.db.execute("""INSERT INTO node_attempts(attempt_id, execution_id, node_id, physical_id, attempt_no, state,
                            lease_owner, lease_expires, output_relation, started_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (attempt_id, eid, nid, exe["current_physical_id"], attempt_no, "reserved", self.owner, lease_exp,
                          relation, now_iso()))
        rels = {k: r["output_relation"] for k, r in runs.items() if r.get("output_relation")}
        params = _restore_params(exe["params"])
        with self._inflight_lock:
            self.inflight[(eid, nid)] = ""
        self.pool.submit(self._work, eid, node, attempt_id, relation, rels, params, exe["access_fingerprint"],
                         time.perf_counter())

    def _work(self, eid: str, node: dict[str, Any], attempt_id: str, relation: str | None,
              rels: dict[str, str], params: dict[str, Any], access_fp: str, t_dispatch: float) -> None:
        nid = node["node_id"]
        self.dispatch_latency_ms.append((time.perf_counter() - t_dispatch) * 1000)
        try:
            if node["kind"] == "python":
                dep = node["depends_on"][0]
                rows = self.engine.fetch(f"SELECT * FROM {rels[dep]}")
                out, meta = run_operator(node["python"], rows)
                self.events.put(("done", eid, nid, attempt_id, None,
                                 {"rows_in": len(rows), "rows": out, "operator": meta, "elapsed_ms": None}))
                return
            sql = node["sql"]
            for dep_id, rel in rels.items():
                sql = sql.replace("{{rel:" + dep_id + "}}", rel)
            if "{{rel:" in sql:
                missing = re.findall(r"\{\{rel:(\w+)\}\}", sql)
                raise RuntimeError(f"unresolved relation tokens in {nid}: {missing}")
            stmt = self.dialect.ctas(relation, sql)
            allowed = set(SOURCE_ALLOWLIST) | set(rels.values())
            declared = set(node.get("params", [])) | ({"source_cut_ts"} if self.dialect.name == "snowflake" else set())
            check = validate_sql(stmt, self.dialect.sqlglot, allowed, declared)
            if not check.ok:
                raise PermissionError("AST validation failed: " + "; ".join(check.errors))
            query_id = new_id("q")
            expires = (utcnow() + timedelta(seconds=self.settings.stage_ttl_seconds)).isoformat()
            # persist the query id and the governed stage relation *before* awaiting completion
            self.store.execute("UPDATE node_attempts SET query_id=?, state='submitted' WHERE attempt_id=?", (query_id, attempt_id))
            self.store.execute("""INSERT OR REPLACE INTO stage_relations(relation, execution_id, node_id, attempt_no,
                                  access_fingerprint, created_at, expires_at, status) VALUES (?,?,?,?,?,?,?,?)""",
                               (relation, eid, nid, int(relation.rsplit("_a", 1)[1]), access_fp, now_iso(), expires, "creating"))
            self.store.execute("UPDATE node_runs SET state='running', output_relation=? WHERE execution_id=? AND node_id=?",
                               (relation, eid, nid))
            with self._inflight_lock:
                self.inflight[(eid, nid)] = query_id
            self.store.execute("UPDATE node_attempts SET state='running' WHERE attempt_id=?", (attempt_id,))
            res = self.engine.execute(stmt, params, query_id)
            self.events.put(("done", eid, nid, attempt_id, relation,
                             {"elapsed_ms": round(res.elapsed_ms, 2), "query_id": query_id, "telemetry": res.telemetry,
                              "ast_check": {"tables": check.tables, "placeholders": check.placeholders}}))
        except Exception as exc:
            self.events.put(("error", eid, nid, attempt_id, relation, {"error": f"{type(exc).__name__}: {str(exc)[:300]}",
                                                                       "permanent": isinstance(exc, PermissionError)}))
        finally:
            with self._inflight_lock:
                self.inflight.pop((eid, nid), None)
            self.wake()

    # -- completion ------------------------------------------------------------
    def _handle_event(self, ev: tuple) -> None:
        kind, eid, nid, attempt_id, relation, info = ev
        exe = self.store.get_execution(eid)
        if exe is None or exe["state"] in TERMINAL:
            return
        plan = self.store.physical_plan(exe["current_physical_id"])
        node = next((n for n in plan["nodes"] if n["node_id"] == nid), None)
        trace_id = exe["context"]["trace_id"]
        if node is None:
            return
        if kind == "error":
            self._on_error(exe, node, attempt_id, relation, info)
            return
        self.store.execute("UPDATE node_runs SET state='validating' WHERE execution_id=? AND node_id=?", (eid, nid))
        params = _restore_params(exe["params"])
        runs = self.store.node_runs(eid)
        rels = {k: r["output_relation"] for k, r in runs.items() if r.get("output_relation")}
        observed: dict[str, Any] = {k: v for k, v in info.items() if k != "rows"}
        if node["kind"] == "python":
            rows = info["rows"]
            validations = [{"id": "row_preservation", "type": "python_rows_preserved", "blocking": True,
                            "passed": len(rows) == info["rows_in"], "observed": {"in": info["rows_in"], "out": len(rows)}}]
            observed["rows_out"] = len(rows)
            observed["result_rows"] = rows
        else:
            n_rows = self.engine.fetch(f"SELECT COUNT(*) AS n FROM {relation}")[0]["n"]
            observed["rows"] = int(n_rows)
            if nid == "canonical_cohort":
                observed["mature_rows"] = int(self.engine.fetch(f"SELECT COALESCE(SUM(mature),0) AS m FROM {relation}")[0]["m"])
            if nid == "bind_outcome":
                fo = self.engine.fetch(f"SELECT COALESCE(SUM(bind_events_in_window),0) AS matched_events, "
                                       f"COALESCE(MAX(bind_events_in_window),0) AS max_fanout, "
                                       f"COALESCE(AVG(bind_events_in_window),0) AS avg_fanout FROM {relation}")[0]
                observed["fanout"] = {k: (float(v) if k == "avg_fanout" else int(v)) for k, v in fo.items()}
            validations = run_validations(self.engine, node, relation, rels, params)
            env = envelope_check(node, observed["rows"])
            if env:
                observed["envelope"] = env
        observed["validations"] = validations
        failed = [v for v in validations if v["blocking"] and not v["passed"]]
        state = "failed" if failed else "complete"
        self.store.execute("UPDATE node_runs SET state=?, observed_json=?, output_relation=COALESCE(?, output_relation), "
                           "updated_at=? WHERE execution_id=? AND node_id=?",
                           (state, json.dumps(observed, default=str), relation, now_iso(), eid, nid))
        self.store.execute("UPDATE node_attempts SET state=?, finished_at=?, observed_json=?, error=? WHERE attempt_id=?",
                           (state if not info.get("reconciled") else f"{state}_reconciled", now_iso(),
                            json.dumps({k: v for k, v in observed.items() if k != "result_rows"}, default=str),
                            "; ".join(v["id"] for v in failed) or None, attempt_id))
        if relation:
            self.store.execute("UPDATE stage_relations SET status=? WHERE relation=?",
                               ("admitted" if not failed else "rejected", relation))
        self.tracer.emit(trace_id, f"node:{nid}", state,
                         {"attempt_id": attempt_id, "query_id": info.get("query_id"), "output_relation": relation,
                          "expectations": node.get("envelope"),
                          "observations": {k: v for k, v in observed.items() if k not in ("result_rows", "validations")},
                          "validations": [{"id": v["id"], "passed": v["passed"]} for v in validations],
                          "reconciled": bool(info.get("reconciled"))},
                         execution_id=eid, request_id=exe["request_id"])
        if failed:
            self._fail(exe, f"validation failed at {nid}: " + ", ".join(v["id"] for v in failed))
            return
        if nid == "canonical_cohort":
            self._checkpoint(exe, plan, observed)
        self.wake()

    def _on_error(self, exe: dict[str, Any], node: dict[str, Any], attempt_id: str, relation: str | None,
                  info: dict[str, Any]) -> None:
        eid, nid = exe["execution_id"], node["node_id"]
        cancelled = bool(self.store.get_execution(eid)["cancel_requested"])
        self.store.execute("UPDATE node_attempts SET state=?, finished_at=?, error=? WHERE attempt_id=?",
                           ("cancelled" if cancelled else "failed", now_iso(), info["error"], attempt_id))
        if relation:
            self.store.execute("UPDATE stage_relations SET status='orphaned' WHERE relation=?", (relation,))
        runs = self.store.node_runs(eid)
        retry = not cancelled and not info.get("permanent") and runs[nid]["attempts"] < MAX_ATTEMPTS
        self.store.execute("UPDATE node_runs SET state=? WHERE execution_id=? AND node_id=?",
                           ("cancelled" if cancelled else ("pending" if retry else "failed"), eid, nid))
        self.tracer.emit(exe["context"]["trace_id"], f"node:{nid}", "retrying" if retry else "error",
                         {"attempt_id": attempt_id, "error": info["error"]}, execution_id=eid, request_id=exe["request_id"])
        self.wake()

    def _checkpoint(self, exe: dict[str, Any], plan: dict[str, Any], observed: dict[str, Any]) -> None:
        ctx = exe["context"]
        ir = self.registry.logical_body(exe["plan_id"], ctx["logical_version"])
        runs = self.store.node_runs(exe["execution_id"])
        node = next(n for n in plan["nodes"] if n["node_id"] == "canonical_cohort")
        obs = {"rows": observed["rows"], "mature_rows": observed["mature_rows"],
               "estimated": (node.get("envelope") or {}).get("rows_est"), "envelope": node.get("envelope") or {}}
        decision = adaptation.consider(plan, ir, self.dialect, obs, {k: r["state"] for k, r in runs.items()},
                                       exe["revisions"] or 0, exe["budget"].get("max_plan_revisions", 3),
                                       self.settings, self.settings.workers)
        new_plan = decision.pop("new_plan", None)
        if decision["action"] == "replan_suffix" and new_plan is not None:
            for n in new_plan["nodes"]:  # carry envelopes forward for the unchanged prefix
                old = next((o for o in plan["nodes"] if o["node_id"] == n["node_id"]), None)
                if old:
                    n["envelope"] = old.get("envelope", {})
            new_pid, _ = self.registry.save_physical(exe["plan_id"], ctx["logical_version"], new_plan,
                                                     origin="checkpoint_adaptation", parent=exe["current_physical_id"])
            aid = new_id("adapt")
            record = {**decision, "from_physical_id": exe["current_physical_id"], "to_physical_id": new_pid}
            with self.store.transaction() as s:
                s.db.execute("INSERT INTO adaptations VALUES (?,?,?,?,?,?,?)",
                             (aid, exe["execution_id"], exe["current_physical_id"], new_pid, "canonical_cohort",
                              json.dumps(record, default=str), now_iso()))
                for nid in decision["changed_nodes"]["removed"]:
                    s.db.execute("DELETE FROM node_runs WHERE execution_id=? AND node_id=? AND state='pending'",
                                 (exe["execution_id"], nid))
                for nid in decision["changed_nodes"]["added"]:
                    s.db.execute("INSERT OR REPLACE INTO node_runs(execution_id, node_id, physical_id, state, attempts, updated_at) "
                                 "VALUES (?,?,?,?,0,?)", (exe["execution_id"], nid, new_pid, "pending", now_iso()))
                s.db.execute("UPDATE executions SET current_physical_id=?, revisions=COALESCE(revisions,0)+1 WHERE execution_id=?",
                             (new_pid, exe["execution_id"]))
            decision["adaptation_id"] = aid
            decision["to_physical_id"] = new_pid
        self.tracer.emit(ctx["trace_id"], "checkpoint", decision["action"], {"decision": decision},
                         execution_id=exe["execution_id"], request_id=exe["request_id"])

    # -- terminal transitions --------------------------------------------------
    def _cancel(self, exe: dict[str, Any], reason: str) -> None:
        eid = exe["execution_id"]
        with self._inflight_lock:
            qids = [q for (e, _), q in self.inflight.items() if e == eid and q]
        for q in qids:
            self.engine.interrupt(q)
        self.store.execute("UPDATE node_runs SET state='cancelled' WHERE execution_id=? AND state NOT IN ('complete','failed')", (eid,))
        self.store.execute("UPDATE stage_relations SET status='orphaned' WHERE execution_id=? AND status!='admitted'", (eid,))
        self.store.set_execution(eid, state="cancelled", finished_at=now_iso(), error=reason)
        self.tracer.emit(exe["context"]["trace_id"], "execute", "cancelled", {"reason": reason, "interrupted_queries": qids},
                         execution_id=eid, request_id=exe["request_id"])
        self.finalizer(eid)  # emits an honest receipt for the cancelled run

    def _fail(self, exe: dict[str, Any], reason: str) -> None:
        eid = exe["execution_id"]
        self.store.execute("UPDATE node_runs SET state='cancelled' WHERE execution_id=? AND state='pending'", (eid,))
        self.store.set_execution(eid, state="failed", finished_at=now_iso(), error=reason)
        self.tracer.emit(exe["context"]["trace_id"], "execute", "failed", {"reason": reason},
                         execution_id=eid, request_id=exe["request_id"])
        self.finalizer(eid)

    def wait(self, execution_id: str, timeout: float = 60.0) -> dict[str, Any]:
        """Convenience for tests/CLI: block until the execution is terminal."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            exe = self.store.get_execution(execution_id)
            if exe and exe["state"] in TERMINAL:
                return exe
            time.sleep(0.02)
            self.wake()
        raise TimeoutError(execution_id)
