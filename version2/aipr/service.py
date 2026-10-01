"""The information compiler service: one façade over every workflow phase (spec section 2).

    interpret -> confirm -> prepare (retrieve, resolve, cache check, profile, plan, estimate)
    -> execute (online runtime) -> validate + cache -> present -> assess -> improve
    and, separately, the recipe builder.

The service is always running: constructing it starts the coordinator and its
warm workers.  The FastAPI app (aipr.api.server) and the Streamlit UI call
these methods; neither is a high-volume processing engine.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import COMPILER_VERSION
from .access.policy import AccessPolicy
from .assess.assessment import assess as run_assessment
from .assess.assessment import render_markdown
from .assess.feedback import FeedbackStore
from .builder import recipe_builder
from .cache.information_cache import InformationCache, rollup, with_rates
from .compiler.dialect import get_dialect
from .compiler.physical import compile_fused, compile_partitioned, compile_staged
from .context.obligations import derive_obligations
from .context.retrieval import EvidenceStore, retrieve
from .control.registry import PlanRegistry
from .control.store import ControlStore
from .core.clock import now_iso
from .core.config import Settings
from .core.hashing import fingerprint
from .core.ids import new_id, trace_id as new_trace_id
from .data import fixture
from .engines.duckdb_engine import DuckDBEngine
from .engines.python_ops import runtime_digest
from .intent import clarify
from .intent.interpreter import intent_from_form, interpret_text
from .intent.llm import get_proposer, merge_proposal
from .intent.model import (bound_parameters, coverage_fingerprint, intent_hash, is_confirmed, new_record,
                           semantic_fingerprint, apply_edit)
from .ir.logical import build_logical_ir, template_fingerprint
from .planner import costing
from .planner.cardinality import join_envelopes, rejected_alternatives
from .present.facts import answer_facts, chart_spec, evidence_categories, narrative
from .recipes.registry import RecipeRegistry
from .resolve.binding import binding_contract
from .resolve.profiling import blocking_findings, probe_scope, run_profile
from .runtime.coordinator import Coordinator, TERMINAL
from .trace.receipt import build_receipt, save_receipt
from .trace.tracer import Tracer
from .verify.oracle import oracle


def _jsonable(params: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in params.items()}


class AIPRService:
    def __init__(self, settings: Settings | None = None, engine: Any = None, start: bool = True,
                 warehouse_rows: dict[str, list[tuple]] | None = None):
        self.settings = settings or Settings()
        s = self.settings
        s.ensure_dirs()
        if engine is None:
            if not s.warehouse_path.exists() or warehouse_rows is not None:
                fixture.build_warehouse(s.warehouse_path, warehouse_rows)
            engine = DuckDBEngine(s.warehouse_path)
        self.engine = engine
        self.dialect = get_dialect(engine.dialect_name)
        self.store = ControlStore(s.control_path)
        self.tracer = Tracer(self.store)
        self.registry = PlanRegistry(self.store, s.artifacts_dir)
        self.recipes = RecipeRegistry(s.recipes_dir)
        self.evidence = EvidenceStore.load(s.context_dir)
        self.access = AccessPolicy(s.context_dir / "access" / "policies.json")
        self.cache = InformationCache(self.store, engine, s.cache_ttl_seconds)
        self.feedback = FeedbackStore(self.store, s.feedback_dir)
        self.proposer = get_proposer(s.llm_provider)
        self.coordinator = Coordinator(self.store, engine, self.tracer, self.registry, s, self._finalize)
        if start:
            self.coordinator.start()

    def close(self) -> None:
        self.coordinator.stop()
        if hasattr(self.engine, "close"):
            self.engine.close()

    # ------------------------------------------------------------------ interpret
    def interpret(self, text: str | None = None, form: dict[str, Any] | None = None, subject: str = "analyst_nc",
                  purpose: str = "pricing_review", conversation_id: str | None = None,
                  parent_request_id: str | None = None, constraints: dict[str, Any] | None = None,
                  use_llm: bool = True) -> dict[str, Any]:
        recipe = self.recipes.latest("quote_conversion")
        if form is not None:
            intent, assumptions = intent_from_form(form, recipe), []
        else:
            intent, assumptions = interpret_text(text or "", recipe)
        requester = {"subject": subject, "tenant": self.access.doc["tenant"], "purpose": purpose}
        rec = new_record(text or json.dumps(form, default=str), requester, intent, conversation_id,
                         parent_request_id, constraints)
        rec["assumptions"] = assumptions
        if use_llm and text:
            proposal, receipt = self.proposer.propose(text, {"recipes": list(self.recipes.recipes)})
            rec["agent_invocations"].append(receipt)
            if proposal:
                rec["intent"], notes = merge_proposal(rec["intent"], proposal)
                rec["assumptions"] += notes
        else:
            rec["agent_invocations"].append({"provider": "deterministic", "status": "interpreted",
                                             "interpreter": "aipr.intent.interpreter"})
        # conversation reuse: a confirmed parent with unchanged meaning carries its confirmation
        if parent_request_id:
            parent = self.store.get_request(parent_request_id)
            if parent and is_confirmed(parent) and parent["intent"] == rec["intent"]:
                rec["confirmation"] = {**parent["confirmation"], "reused_from": parent_request_id}
        (self.settings.artifacts_dir / "requests" / f"{rec['request_id']}.txt").write_text(rec["raw_request"])
        route = self.recipes.resolve(rec["intent"])
        rec["route"] = route.to_dict()
        rec["trace_id"] = new_trace_id()
        self.store.save_request(rec, "interpreted", intent_hash(rec))
        self.tracer.emit(rec["trace_id"], "interpret", route.status,
                         {"intent_hash": intent_hash(rec), "assumptions": rec["assumptions"],
                          "route": route.to_dict(), "agent_invocations": rec["agent_invocations"]},
                         request_id=rec["request_id"])
        return self.describe_request(rec["request_id"])

    def describe_request(self, request_id: str) -> dict[str, Any]:
        rec = self.store.get_request(request_id)
        recipe = self.recipes.get(rec["route"]["recipe_key"]) if rec["route"].get("recipe_key") else None
        return {"request_id": request_id, "record": rec, "intent_hash": intent_hash(rec), "confirmed": is_confirmed(rec),
                "route": rec["route"], "questions": clarify.material_questions(rec["intent"], recipe),
                "maturity": clarify.maturity_analysis(rec["intent"]), "preparation": rec.get("preparation")}

    def edit(self, request_id: str, path: str, value: Any) -> dict[str, Any]:
        rec = apply_edit(self.store.get_request(request_id), path, value)
        rec.pop("preparation", None)
        rec["route"] = self.recipes.resolve(rec["intent"]).to_dict()
        self.store.save_request(rec, "edited", intent_hash(rec))
        self.tracer.emit(rec["trace_id"], "interpret", "edited", {"path": path, "value": value,
                         "confirmation": rec["confirmation"]["status"]}, request_id=request_id)
        return self.describe_request(request_id)

    # ------------------------------------------------------------------ confirm
    def confirm(self, request_id: str, actor: str, answers: dict[str, str] | None = None) -> dict[str, Any]:
        rec = self.store.get_request(request_id)
        rec = clarify.confirm(rec, actor, answers)
        rec.pop("preparation", None)
        rec["route"] = self.recipes.resolve(rec["intent"]).to_dict()
        self.store.save_request(rec, "confirmed", intent_hash(rec))
        self.tracer.emit(rec["trace_id"], "confirm", rec["route"]["status"],
                         {"approved_intent_hash": rec["confirmation"]["approved_intent_hash"], "actor": actor,
                          "answers": answers or {}, "route": rec["route"]}, request_id=request_id)
        return self.describe_request(request_id)

    # ------------------------------------------------------------------ prepare
    def prepare(self, request_id: str, prefer_variant: str | None = None, stats_scale: float | None = None,
                weights: dict[str, float] | None = None, partitions: int | None = None,
                bypass_cache: bool = False) -> dict[str, Any]:
        """Retrieve -> resolve -> cache check -> profile -> plan -> estimate.  No source execution before confirmation."""
        rec = self.store.get_request(request_id)
        tid = rec["trace_id"]
        prep: dict[str, Any] = {"prepared_at": now_iso(), "status": "blocked", "blockers": []}
        if not is_confirmed(rec):
            prep["blockers"].append("meaning not confirmed (or edited after confirmation)")
            return self._save_prep(rec, prep)
        route = self.recipes.resolve(rec["intent"])
        prep["route"] = route.to_dict()
        if route.status != "supported":
            prep["blockers"] += [f"{g['field']}: {g['gap']}" for g in route.gaps] or [route.status]
            self.tracer.emit(tid, "retrieve", "blocked", {"route": route.to_dict()}, request_id=request_id)
            return self._save_prep(rec, prep)
        recipe = self.recipes.get(route.recipe_key)
        intent = rec["intent"]
        prep.update(recipe_key=recipe.key, recipe_hash=recipe.content_hash)
        # access decision (before any source read)
        decision = self.access.decide(rec["requester"]["subject"], rec["requester"]["purpose"], intent["scope"])
        prep["access"] = decision.to_dict()
        if not decision.allowed:
            prep["blockers"].append(f"access denied: {decision.reason}")
            self.tracer.emit(tid, "resolve", "access_denied", {"access": decision.to_dict()}, request_id=request_id)
            return self._save_prep(rec, prep)
        # obligation-directed retrieval
        obligations = derive_obligations(intent)
        manifest = retrieve(self.evidence, intent, obligations)
        prep["evidence"] = manifest.to_dict()
        self.tracer.emit(tid, "retrieve", "complete" if manifest.complete else "missing_obligations",
                         {"evidence_manifest": prep["evidence"]["manifest_hash"],
                          "claims": [e["claim_id"] for e in manifest.entries],
                          "rejected": manifest.rejected, "missing": manifest.missing_obligations},
                         request_id=request_id)
        if not manifest.complete:
            prep["blockers"] += [m["gap"] for m in manifest.missing_obligations]
            return self._save_prep(rec, prep)
        # source cut + binding
        source_cut = self.engine.source_cut()
        scfp = fingerprint(source_cut)
        prep["source_cut"] = source_cut
        prep["source_cut_fingerprint"] = scfp
        schema_fp = self.engine.schema_fingerprint()
        prep["binding"] = binding_contract(intent, recipe, decision.to_dict(), source_cut, schema_fp)
        sem_fp = semantic_fingerprint(intent, recipe.key)
        cov_fp = coverage_fingerprint(intent, recipe.key)
        prep.update(semantic_fingerprint=sem_fp, coverage_fingerprint=cov_fp, access_fingerprint=decision.fingerprint)
        # advisory feedback from approved cases for this recipe/scope
        prep["advisories"] = self.feedback.applicable_advice(recipe.key, intent["scope"])
        # compatible cache *before* expensive discovery
        hit = self.cache.lookup(semantic_fp=sem_fp, coverage_fp=cov_fp, source_cut_fp=scfp, access_allowed=decision.allowed,
                                access_fp=decision.fingerprint, recipe_key=recipe.key, dims=intent["dimensions"])
        prep["cache"] = {"kind": hit["kind"], "rejected": hit["rejected"],
                         "information_set_id": hit.get("information_set", {}).get("information_set_id"),
                         "operation": hit.get("operation")}
        self.tracer.emit(tid, "cache_lookup", hit["kind"], {"cache": prep["cache"]}, request_id=request_id)
        params = bound_parameters(intent)
        prep["params"] = _jsonable(params)
        if hit["kind"] != "miss" and not bypass_cache:
            prep["status"] = "cache_hit"
            prep["estimate"] = {"duration_seconds": {"p50": 0.0, "p90": 0.0, "range": [0.0, 0.0]},
                                "credits_notional": {"p50": 0.0, "range": [0.0, 0.0]}, "label": "cache operation"}
            return self._save_prep(rec, prep)
        # profiling: reuse a compatible profile, else bounded VOI probes
        pkey = fingerprint({"cut": scfp, "scope": probe_scope(intent)})
        cached_prof = self.store.one("SELECT body_json FROM profiles WHERE profile_key=?", (pkey,))
        profile = run_profile(self.engine, self.dialect, intent, params, scfp,
                              json.loads(cached_prof["body_json"]) if cached_prof else None,
                              rec["constraints"]["discovery_budget_credits"], rec["constraints"]["discovery_max_probes"])
        if not cached_prof:
            self.store.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?)", (pkey, json.dumps(profile), now_iso()))
        prep["profile"] = profile
        self.tracer.emit(tid, "resolve", "profiled", {"binding_hash": prep["binding"]["binding_hash"],
                         "probes": profile["probes"], "reused": profile["reused"]}, request_id=request_id)
        blocks = blocking_findings(profile)
        if blocks:
            prep["blockers"] += blocks
            return self._save_prep(rec, prep)
        # logical IR + plan registry
        ir = build_logical_ir(intent, recipe)
        tfp = template_fingerprint(ir)
        mature = profile["stats"].get("mature_cohort_size", {}).get("value", {}).get("mature_rows")
        applicability = {"parameter_schema": ir["parameter_schema"], "schema_fingerprint": schema_fp,
                         "policy_version": self.access.version, "recipe_hash": recipe.content_hash,
                         "scope": {"lob": intent["scope"]["lob"]}, "certification": recipe.body["certification"]["status"],
                         "stats_envelope": {"mature_rows": mature,
                                            "mature_rows_range": [int((mature or 0) / 2), int((mature or 0) * 2) + 1]}}
        plan_id, lver, reused, check = self.registry.save_logical(ir, tfp, applicability)
        prep.update(plan_id=plan_id, logical_version=lver, plan_reused=reused, plan_applicability=check,
                    template_fingerprint=tfp, logical_ir_hash=fingerprint(ir))
        saved = self.registry.find_logical(tfp)
        prep["certificate"] = saved.get("certificate") if saved else None
        # physical candidates
        uncertainty = 1.5 if profile["stats"].get("mature_cohort_size", {}).get("label") == "exact" else 3.0
        envs = join_envelopes(profile, uncertainty)
        if stats_scale:  # simulate stale statistics (demo / tests of checkpoint adaptation)
            for k in ("canonical_cohort", "quote_keys", "bind_outcome", "quote_outcomes"):
                e = envs[k]
                if e["rows_est"] is not None:
                    e.update(rows_est=int(e["rows_est"] * stats_scale), rows_low=int(e["rows_low"] * stats_scale),
                             rows_high=int(e["rows_high"] * stats_scale) + 1)
            prep["stats_override"] = {"scale": stats_scale, "note": "estimates deliberately scaled to simulate stale statistics"}
        est_mature = envs["quote_keys"]["rows_est"] or 0
        n_parts = partitions or max(2, min(self.settings.max_partitions,
                                           -(-est_mature // self.settings.rows_per_partition)))
        cands = [("fused", compile_fused(ir, self.dialect)), ("staged", compile_staged(ir, self.dialect)),
                 (f"partitioned_{n_parts}", compile_partitioned(ir, self.dialect, n_parts))]
        candidates = []
        for label, plan in cands:
            for n in plan["nodes"]:
                if n["node_id"] in envs:
                    n["envelope"] = envs[n["node_id"]]
            plan["physical_fingerprint"] = fingerprint({k: v for k, v in plan.items() if k != "physical_fingerprint"})
            est = costing.estimate_plan(plan, envs, self.engine.name, self.settings.workers,
                                        self.settings.rows_per_partition, uncertainty, envs["raw_join"]["rows_est"])
            candidates.append({"label": label, "plan": plan, "estimate": est})
        choice = costing.choose(candidates, rec["constraints"]["execution_budget_credits"],
                                rec["constraints"]["max_runtime_seconds"], weights, prefer_variant)
        prep["rejected_alternatives"] = rejected_alternatives(envs)
        prep["join_envelopes"] = envs
        prep["candidates"] = []
        for c in candidates:
            pid, existed = self.registry.save_physical(plan_id, lver, c["plan"], origin="compiler")
            c["physical_id"] = pid
            prep["candidates"].append({"label": c["label"], "physical_id": pid, "variant": c["plan"]["variant"],
                                       "partitions": c["plan"]["partitions"], "estimate": c["estimate"],
                                       "feasible": c["feasible"], "infeasible_reasons": c["infeasible_reasons"],
                                       "objective": c["objective"], "on_pareto_frontier": c["on_pareto_frontier"],
                                       "physical_fingerprint": c["plan"]["physical_fingerprint"],
                                       "decomposition_rationale": c["plan"]["decomposition_rationale"],
                                       "reused_physical_version": existed})
        if choice["selected"] is None:
            prep["blockers"].append(choice["reason"])
            return self._save_prep(rec, prep)
        sel = choice["selected"]
        prep["selected"] = {"label": sel["label"], "physical_id": sel["physical_id"], "estimate": sel["estimate"]}
        prep["selected_plan"] = sel["plan"]
        prep["selection_reason"] = choice["reason"]
        prep["estimate"] = sel["estimate"]
        prep["status"] = "ready"
        self.tracer.emit(tid, "physical_planning", "proposed", {
            "input_refs": {"intent_hash": intent_hash(rec), "evidence_manifest": prep["evidence"]["manifest_hash"],
                           "stats_version": pkey},
            "decision": {"selected_plan": sel["label"], "reason": choice["reason"],
                         "rejected": [{"plan": r["plan"], "rule": r["rule"]} for r in prep["rejected_alternatives"]]},
            "output_refs": {"logical_ir_hash": prep["logical_ir_hash"], "physical_dag_hash": sel["plan"]["physical_fingerprint"]},
            "expectations": {"duration_seconds_p50": sel["estimate"]["duration_seconds"]["p50"],
                             "duration_seconds_p90": sel["estimate"]["duration_seconds"]["p90"],
                             "credits_range": sel["estimate"]["credits_notional"]["range"],
                             "output_rows_range": [envs["canonical_cohort"]["rows_low"], envs["canonical_cohort"]["rows_high"]]},
            "actor": {"type": "service", "version": COMPILER_VERSION}}, request_id=request_id)
        return self._save_prep(rec, prep)

    def _save_prep(self, rec: dict[str, Any], prep: dict[str, Any]) -> dict[str, Any]:
        prep["intent_hash"] = intent_hash(rec)
        rec["preparation"] = prep
        self.store.save_request(rec, f"prepared:{prep['status']}", intent_hash(rec))
        return prep

    # ------------------------------------------------------------------ execute
    def execute(self, request_id: str, queue: bool = False, priority: int = 0,
                physical_id: str | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        rec = self.store.get_request(request_id)
        prep = rec.get("preparation")
        if not is_confirmed(rec):
            return {"admitted": False, "reason": "intent not confirmed or changed since confirmation"}
        if not prep or prep.get("intent_hash") != intent_hash(rec):
            return {"admitted": False, "reason": "request not prepared for the confirmed intent"}
        if prep["status"] not in ("ready", "cache_hit"):
            return {"admitted": False, "reason": "; ".join(prep.get("blockers") or [prep["status"]])}
        decision = self.access.decide(rec["requester"]["subject"], rec["requester"]["purpose"], rec["intent"]["scope"])
        if not decision.allowed or decision.fingerprint != prep["access_fingerprint"]:
            return {"admitted": False, "reason": "access changed since preparation; re-prepare"}
        if fingerprint(self.engine.source_cut()) != prep["source_cut_fingerprint"]:
            return {"admitted": False, "reason": "source cut changed since preparation; re-prepare"}
        eid = new_id("exe")
        ctx = {"trace_id": rec["trace_id"], "logical_version": prep.get("logical_version"),
               "recipe_key": prep["recipe_key"]}
        base = dict(execution_id=eid, request_id=request_id, subject=rec["requester"]["subject"],
                    intent_hash=intent_hash(rec), semantic_fingerprint=prep["semantic_fingerprint"],
                    coverage_fingerprint=prep["coverage_fingerprint"], source_cut_fingerprint=prep["source_cut_fingerprint"],
                    access_fingerprint=prep["access_fingerprint"], params_json=json.dumps(prep["params"]),
                    budget_json=json.dumps(rec["constraints"]), priority=priority, created_at=now_iso())
        if prep["status"] == "cache_hit":
            return self._execute_from_cache(rec, prep, base, ctx, t0)
        # shared producer: identical semantic/source/access contract already in flight
        producer = self.store.one("""SELECT execution_id FROM executions WHERE semantic_fingerprint=? AND source_cut_fingerprint=?
                                     AND access_fingerprint=? AND state IN ('queued','running','finalizing') LIMIT 1""",
                                  (prep["semantic_fingerprint"], prep["source_cut_fingerprint"], prep["access_fingerprint"]))
        pid = physical_id or prep["selected"]["physical_id"]
        plan = self.store.physical_plan(pid)
        if producer and physical_id is None:
            ctx["admission_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            self._insert_execution(base, prep, pid, ctx, state="waiting", waits_on=producer["execution_id"])
            self.tracer.emit(rec["trace_id"], "execute", "attached_to_shared_producer",
                             {"producer": producer["execution_id"]}, request_id=request_id, execution_id=eid)
            return {"admitted": True, "execution_id": eid, "state": "waiting", "waits_on": producer["execution_id"],
                    "admission_ms": ctx["admission_ms"]}
        running = self.store.one("SELECT COUNT(*) AS n FROM executions WHERE state='running'")["n"]
        state = "queued" if (queue or running >= self.settings.max_concurrent_executions) else "running"
        if state == "queued" and not rec["constraints"].get("queue_allowed", True):
            return {"admitted": False, "reason": "capacity exhausted and queueing not allowed"}
        ctx["admission_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        with self.store.transaction() as s:
            self._insert_execution(base, prep, pid, ctx, state=state, tx=s)
            for n in plan["nodes"]:
                s.db.execute("INSERT INTO node_runs(execution_id, node_id, physical_id, state, attempts, updated_at) "
                             "VALUES (?,?,?,?,0,?)", (eid, n["node_id"], pid, "pending", now_iso()))
        self.tracer.emit(rec["trace_id"], "execute", "admitted", {"execution_id": eid, "state": state,
                         "physical_id": pid, "admission_ms": ctx["admission_ms"]}, request_id=request_id, execution_id=eid)
        self.coordinator.wake()
        return {"admitted": True, "execution_id": eid, "state": state, "physical_id": pid,
                "admission_ms": ctx["admission_ms"]}

    def _insert_execution(self, base: dict[str, Any], prep: dict[str, Any], pid: str | None, ctx: dict[str, Any],
                          state: str, waits_on: str | None = None, tx: Any = None) -> None:
        row = {**base, "plan_id": prep.get("plan_id"), "initial_physical_id": pid, "current_physical_id": pid,
               "context_json": json.dumps(ctx), "state": state, "waits_on": waits_on,
               "admitted_at": now_iso(), "started_at": now_iso() if state == "running" else None}
        cols = ", ".join(row)
        db = tx.db if tx is not None else self.store
        db.execute(f"INSERT INTO executions({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))

    def _execute_from_cache(self, rec, prep, base, ctx, t0) -> dict[str, Any]:
        hit = self.cache.lookup(semantic_fp=prep["semantic_fingerprint"], coverage_fp=prep["coverage_fingerprint"],
                                source_cut_fp=prep["source_cut_fingerprint"], access_allowed=True,
                                access_fp=prep["access_fingerprint"], recipe_key=prep["recipe_key"],
                                dims=rec["intent"]["dimensions"])
        if hit["kind"] == "miss":
            return {"admitted": False, "reason": "cache entry no longer valid; re-prepare"}
        ctx["admission_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        self._insert_execution(base, prep, None, ctx, state="finalizing")
        eid = base["execution_id"]
        isr = hit["information_set"]
        origin = {"kind": "application_cache_" + hit["kind"], "information_set_id": isr["information_set_id"],
                  "producer_execution_id": isr["producer_execution_id"],
                  "producer_receipt": f"artifacts/receipts/{isr['producer_execution_id']}.json",
                  "operation": hit.get("operation") or {"type": "exact_reuse"}}
        self._complete(eid, rows=hit["rows"], origin=origin, final_relation=None, plan=None)
        return {"admitted": True, "execution_id": eid, "state": "complete", "origin": origin,
                "admission_ms": ctx["admission_ms"]}

    # ------------------------------------------------------------------ finalize
    def _finalize(self, execution_id: str) -> None:
        exe = self.store.get_execution(execution_id)
        if exe["state"] in ("failed", "cancelled"):
            self._write_receipt(exe, None, {"kind": "none"}, None)
            return
        plan = self.store.physical_plan(exe["current_physical_id"])
        runs = self.store.node_runs(execution_id)
        final_rel = runs[plan["final_sql_node"]]["output_relation"]
        dims = plan["dimensions"]
        rows = runs["rate_interval"]["observed"]["result_rows"]
        origin = {"kind": "fresh_execution", "variant": plan["variant"], "partitions": plan["partitions"]}
        self._complete(execution_id, rows=rows, origin=origin, final_relation=final_rel, plan=plan)
        # release waiters attached to this producer
        for w in self.store.all("SELECT execution_id FROM executions WHERE waits_on=? AND state='waiting'", (execution_id,)):
            self.store.set_execution(w["execution_id"], state="finalizing", started_at=now_iso())
            self._complete(w["execution_id"], rows=rows, plan=None, final_relation=None,
                           origin={"kind": "shared_producer", "producer_execution_id": execution_id,
                                   "producer_receipt": f"artifacts/receipts/{execution_id}.json"})

    def _complete(self, execution_id: str, rows: list[dict[str, Any]], origin: dict[str, Any],
                  final_relation: str | None, plan: dict[str, Any] | None) -> None:
        exe = self.store.get_execution(execution_id)
        rec = self.store.get_request(exe["request_id"])
        intent = rec["intent"]
        dims = intent["dimensions"]
        stats_rows = rollup(rows, dims)
        if plan is None or origin["kind"] != "fresh_execution":
            from .engines.python_ops import wilson_interval
            out_rows = wilson_interval(stats_rows)
        else:
            out_rows = rows
        publication = None
        if origin["kind"] == "fresh_execution":
            runs = self.store.node_runs(execution_id)
            all_ok = all(v["passed"] for r in runs.values() for v in r["observed"].get("validations", []) if v["blocking"])
            publication = self.cache.publish(execution={**exe, "execution_id": execution_id}, intent=intent,
                                             recipe_key=exe["context"]["recipe_key"], rows=stats_rows,
                                             final_relation=final_relation, validations_passed=all_ok, complete=True,
                                             exactness="exact", policy_version=self.access.version,
                                             physical_fingerprint=plan["physical_fingerprint"])
        prep = rec.get("preparation") or {}
        facts = answer_facts(out_rows, intent, origin, prep.get("source_cut") or {})
        result = {"rows": out_rows, "facts": facts, "narrative": narrative(facts, out_rows),
                  "chart": chart_spec(out_rows, dims), "origin": origin,
                  "physical_fingerprint": plan["physical_fingerprint"] if plan else None,
                  "variant": plan["variant"] if plan else None,
                  "information_set_id": (publication or {}).get("information_set_id") or origin.get("information_set_id")}
        self.store.set_execution(execution_id, finished_at=now_iso(), result_json=json.dumps(result, default=str),
                                 information_set_id=result["information_set_id"])
        exe = {**self.store.get_execution(execution_id), "state": "complete"}
        receipt = self._write_receipt(exe, out_rows, origin, publication)
        temporal = not any("no_future_knowledge" in f for f in receipt["validations"]["failed"])
        result["evidence_categories"] = evidence_categories(
            confirmed=is_confirmed(rec), recipe_certified=self.recipes.get(prep["recipe_key"]).certified,
            manifest_complete=(prep.get("evidence") or {}).get("complete", False),
            temporal_ok=temporal if origin["kind"] == "fresh_execution" else None,
            structural_ok=(not receipt["validations"]["failed"]) if origin["kind"] == "fresh_execution" else None,
            business_verified="unverified (fixture oracle available via assessment)")
        # the terminal state is written last, so a reader never sees 'complete' without result and receipt
        self.store.set_execution(execution_id, result_json=json.dumps(result, default=str), state="complete")
        self.tracer.emit(rec["trace_id"], "present", "complete",
                         {"origin": origin, "result_digest": receipt["output"]["result_digest"],
                          "information_set_id": result["information_set_id"],
                          "cache_publication": {k: v for k, v in (publication or {}).items() if k != "manifest"}},
                         request_id=exe["request_id"], execution_id=execution_id)

    def _write_receipt(self, exe: dict[str, Any], rows: list[dict[str, Any]] | None, origin: dict[str, Any],
                       publication: dict[str, Any] | None) -> dict[str, Any]:
        rec = self.store.get_request(exe["request_id"])
        prep = rec.get("preparation") or {}
        ini = self.store.physical_plan(exe["initial_physical_id"]) if exe.get("initial_physical_id") else None
        act = self.store.physical_plan(exe["current_physical_id"]) if exe.get("current_physical_id") else None
        adaptations = [json.loads(a["record_json"]) for a in
                       self.store.all("SELECT record_json FROM adaptations WHERE execution_id=? ORDER BY created_at",
                                      (exe["execution_id"],))]
        receipt = build_receipt(execution=exe, record=rec, prep=prep, initial_plan=ini, actual_plan=act,
                                attempts=self.store.attempts(exe["execution_id"]),
                                node_runs=self.store.node_runs(exe["execution_id"]), adaptations=adaptations,
                                result_rows=rows, origin=origin, cache_publication=publication)
        save_receipt(self.settings.artifacts_dir, receipt)
        self.store.set_execution(exe["execution_id"], receipt_json=json.dumps(receipt, default=str))
        return receipt

    # ------------------------------------------------------------------ inspect / control
    def status(self, execution_id: str) -> dict[str, Any]:
        exe = self.store.get_execution(execution_id)
        runs = self.store.node_runs(execution_id)
        nodes = [{"node_id": k, "state": r["state"], "attempts": r["attempts"], "rows": r["observed"].get("rows"),
                  "elapsed_ms": r["observed"].get("elapsed_ms"), "output_relation": r.get("output_relation")}
                 for k, r in runs.items()]
        done = sum(1 for n in nodes if n["state"] == "complete")
        adaptations = self.store.all("SELECT adaptation_id, from_physical_id, to_physical_id, created_at FROM adaptations "
                                     "WHERE execution_id=?", (execution_id,))
        return {"execution_id": execution_id, "state": exe["state"], "error": exe.get("error"),
                "progress": f"{done}/{len(nodes)}" if nodes else None, "nodes": nodes, "adaptations": adaptations,
                "waits_on": exe.get("waits_on"), "physical_id": exe.get("current_physical_id"),
                "created_at": exe["created_at"], "started_at": exe.get("started_at"), "finished_at": exe.get("finished_at")}

    def result(self, execution_id: str) -> dict[str, Any]:
        exe = self.store.get_execution(execution_id)
        return {"execution_id": execution_id, "state": exe["state"], "result": exe.get("result"),
                "receipt": exe.get("receipt"), "error": exe.get("error")}

    def wait(self, execution_id: str, timeout: float = 60.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            exe = self.store.get_execution(execution_id)
            if exe["state"] in TERMINAL and exe.get("receipt_json"):
                return self.result(execution_id)
            self.coordinator.wake()
            time.sleep(0.02)
        raise TimeoutError(execution_id)

    def cancel(self, execution_id: str) -> dict[str, Any]:
        exe = self.store.get_execution(execution_id)
        if exe["state"] in TERMINAL:
            return {"cancelled": False, "reason": f"already {exe['state']}"}
        if exe["state"] == "waiting":
            self.store.set_execution(execution_id, state="cancelled", finished_at=now_iso(), error="consumer cancelled")
            self._write_receipt(self.store.get_execution(execution_id), None, {"kind": "none"}, None)
            return {"cancelled": True, "note": "consumer detached; shared producer keeps running"}
        waiters = self.store.one("SELECT COUNT(*) AS n FROM executions WHERE waits_on=? AND state='waiting'", (execution_id,))["n"]
        if waiters:
            return {"cancelled": False, "reason": f"producer still needed by {waiters} waiting consumer(s); not cancelled"}
        self.store.set_execution(execution_id, cancel_requested=1)
        if exe["state"] == "queued":
            self.store.set_execution(execution_id, state="running")
        self.coordinator.wake()
        return {"cancelled": True, "note": "cancellation requested; running statements are interrupted"}

    def sweep(self, force: bool = False) -> dict[str, Any]:
        """Drop expired or orphaned stage relations (transient is a table type, not a TTL)."""
        now = now_iso()
        dropped = []
        for r in self.store.all("""SELECT s.relation, s.status, s.expires_at, e.state FROM stage_relations s
                                   LEFT JOIN executions e ON e.execution_id = s.execution_id WHERE s.status != 'dropped'"""):
            terminal = r["state"] in TERMINAL
            if (r["status"] in ("orphaned", "rejected") and terminal) or r["expires_at"] < now or (force and terminal):
                self.engine.drop_relation(r["relation"])
                self.store.execute("UPDATE stage_relations SET status='dropped' WHERE relation=?", (r["relation"],))
                dropped.append(r["relation"])
        return {"dropped": dropped}

    # ------------------------------------------------------------------ assess / improve
    def assess(self, execution_id: str, feedback: str = "", rating: str | None = None) -> dict[str, Any]:
        exe = self.store.get_execution(execution_id)
        rec = self.store.get_request(exe["request_id"])
        prep = rec.get("preparation") or {}
        peers = []
        for p in self.store.all("""SELECT execution_id, semantic_fingerprint, result_json, started_at, finished_at, current_physical_id,
                                   initial_physical_id FROM executions WHERE coverage_fingerprint=? AND source_cut_fingerprint=?
                                   AND access_fingerprint=? AND state='complete'""",
                                (exe["coverage_fingerprint"], exe["source_cut_fingerprint"], exe["access_fingerprint"])):
            res = json.loads(p["result_json"]) if p["result_json"] else None
            dur = None
            if p["started_at"] and p["finished_at"] and res and res["origin"]["kind"] == "fresh_execution":
                dur = (datetime.fromisoformat(p["finished_at"]) - datetime.fromisoformat(p["started_at"])).total_seconds()
            if p["semantic_fingerprint"] != exe["semantic_fingerprint"] and res:
                dims = rec["intent"]["dimensions"]
                if res["rows"] and not set(dims) <= set(res["rows"][0]):
                    continue  # a coarser grain cannot be compared at this grain
                res = {**res, "rows": rollup(res["rows"], dims)}
            peers.append({"execution_id": p["execution_id"], "result": res, "duration_s": dur,
                          "semantic_fingerprint": p["semantic_fingerprint"],
                          "physical_fingerprint": (res or {}).get("physical_fingerprint"),
                          "adapted": p["current_physical_id"] != p["initial_physical_id"]})
        oracle_rows = None
        if self.engine.name == "duckdb":
            src = {t: [tuple(r.values()) for r in self.engine.fetch(f"SELECT * FROM {t}")] for t in fixture.SOURCE_TABLES}
            i = rec["intent"]
            oracle_rows = oracle(src, {"state": i["scope"]["state"], "lob": i["scope"]["lob"],
                                       "cohort_start": i["cohort"]["start"], "cohort_end": i["cohort"]["end"],
                                       "window_days": i["window"]["value"], "knowledge_cutoff": i["knowledge_cutoff"],
                                       "dimensions": i["dimensions"]})
        doc = run_assessment(execution=exe, record=rec, prep=prep, receipt=exe.get("receipt") or {},
                             node_runs=self.store.node_runs(execution_id), peers=peers, oracle_rows=oracle_rows,
                             trace_check=self.tracer.verify_chain(), user_feedback=feedback, rating=rating)
        top = doc["recommendations"][0]
        case = {"case_id": doc["case_id"], "execution_id": execution_id, "request_id": exe["request_id"],
                "recipe_key": prep.get("recipe_key"), "scope": rec["intent"]["scope"], "status": "proposed",
                "change_class": top["change_class"], "recommendations": doc["recommendations"],
                "gate_vector": doc["gate_vector"], "assessment_hash": doc["assessment_hash"], "created_at": now_iso(),
                "owner": "recipe owner (unassigned)", "affected_scope": {"recipe": prep.get("recipe_key"),
                                                                         "scope": rec["intent"]["scope"]},
                "regression_cases": [{"intent": rec["intent"], "source_cut_fingerprint": exe["source_cut_fingerprint"],
                                      "expected_rows_ref": "expected_outputs.json"}],
                "history": [{"status": "proposed", "at": now_iso(), "actor": "assessment_service"}]}
        path = self.feedback.save_case(case, render_markdown(doc, rec),
                                       {"trace_id": rec["trace_id"], "execution_id": execution_id,
                                        "receipt": f"artifacts/receipts/{execution_id}.json",
                                        "receipt_hash": (exe.get("receipt") or {}).get("receipt_hash")},
                                       {"oracle_rows": oracle_rows, "observed_rows": (exe.get("result") or {}).get("rows")},
                                       {"type": "advisory", "change_class": top["change_class"],
                                        "proposal": top["recommendation"], "executable": False})
        self.tracer.emit(rec["trace_id"], "assess", "proposed", {"case_id": doc["case_id"], "gate_vector": doc["gate_vector"],
                         "assessment_hash": doc["assessment_hash"]}, request_id=exe["request_id"], execution_id=execution_id)
        return {"assessment": doc, "case_path": path}

    def transition_case(self, case_id: str, to: str, actor: str, note: str = "") -> dict[str, Any]:
        return self.feedback.transition(case_id, to, actor, note)

    # ------------------------------------------------------------------ verification
    def certify_equivalence(self, request_id: str, timeout: float = 60.0) -> dict[str, Any]:
        """Developer validation: run every admitted variant on the fixed cut and compare with the oracle."""
        rec = self.store.get_request(request_id)
        prep = rec.get("preparation") or {}
        if not prep.get("candidates"):  # developer validation plans even when a cached answer exists
            prep = self.prepare(request_id, bypass_cache=True)
            if prep["status"] != "ready":
                return {"certificate": None, "reason": "; ".join(prep.get("blockers") or [prep["status"]])}
        results = {}
        for c in prep["candidates"]:
            r = self.execute(request_id, physical_id=c["physical_id"])
            if not r.get("admitted"):
                results[c["label"]] = {"error": r.get("reason")}
                continue
            out = self.wait(r["execution_id"], timeout)
            results[c["label"]] = {"execution_id": r["execution_id"], "state": out["state"],
                                   "rows": (out.get("result") or {}).get("rows")}
        dims = rec["intent"]["dimensions"]
        key = lambda rows: sorted(tuple([str(x[d]) for d in dims] + [int(x["eligible"]), int(x["converted"]),
                                                                       int(x["immature_excluded"])]) for x in rows or [])
        ref = None
        if self.engine.name == "duckdb":
            src = {t: [tuple(r.values()) for r in self.engine.fetch(f"SELECT * FROM {t}")] for t in fixture.SOURCE_TABLES}
            i = rec["intent"]
            ref = oracle(src, {"state": i["scope"]["state"], "lob": i["scope"]["lob"], "cohort_start": i["cohort"]["start"],
                               "cohort_end": i["cohort"]["end"], "window_days": i["window"]["value"],
                               "knowledge_cutoff": i["knowledge_cutoff"], "dimensions": dims})
        agree = {k: (key(v.get("rows")) == key(ref)) if ref is not None else None for k, v in results.items()}
        mutual = len({json.dumps(key(v.get("rows"))) for v in results.values()}) == 1
        cert = {"evidence_class": "empirically_equivalent" if mutual and all(agree.values()) else "counterexample",
                "scope": "fixture source cut " + prep["source_cut_fingerprint"][:19], "variants": list(results),
                "oracle_agreement": agree, "mutual_agreement": mutual, "rewrite_rules": prep["selected_plan"]["rewrite_rules"],
                "bounded_verifier": {"verdict": "unsupported", "counts_as_verified": False},
                "created_at": now_iso(), "note": "test evidence on a fixed cut; no universal equivalence claim"}
        self.registry.set_certificate(prep["plan_id"], prep["logical_version"], cert)
        return {"certificate": cert, "runs": {k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in results.items()}}

    # ------------------------------------------------------------------ recipe builder
    def builder_questions(self, logic: str = "") -> dict[str, Any]:
        return {"questions": recipe_builder.questions(), "suggested": recipe_builder.suggest_answers(logic)}

    def builder_draft(self, name: str, logic: str, answers: dict[str, str], owner: str) -> dict[str, Any]:
        return recipe_builder.build_draft(name, logic, answers, owner, self.recipes, self.settings.drafts_dir)

    # ------------------------------------------------------------------ listings
    def list_executions(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.all("""SELECT execution_id, request_id, subject, state, plan_id, initial_physical_id,
                                 current_physical_id, revisions, information_set_id, created_at, finished_at, error
                                 FROM executions ORDER BY created_at DESC LIMIT ?""", (limit,))

    def runtime_info(self) -> dict[str, Any]:
        lat = self.coordinator.dispatch_latency_ms
        return {"coordinator": self.coordinator.owner, "workers": self.settings.workers,
                "engine": self.engine.name, "python_runtime_digest": runtime_digest(),
                "dispatch_latency_ms": {"n": len(lat), "max": max(lat) if lat else None,
                                        "p50": sorted(lat)[len(lat) // 2] if lat else None}}
