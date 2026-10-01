"""Online DAG runtime: dispatch, adaptation, cancellation, recovery, shared producers (spec section 10)."""
import json
import time

from aipr.data import fixture
from aipr.engines.duckdb_engine import DuckDBEngine
from aipr.service import AIPRService

from conftest import SlowEngine, form, key, make_service, run


def _prepared(svc, **kw):
    prefer = kw.pop("prefer", None)
    scale = kw.pop("stats_scale", None)
    subject = kw.pop("subject", "analyst_nc")
    d = svc.interpret(form=form(**kw), subject=subject)
    svc.confirm(d["request_id"], "t")
    p = svc.prepare(d["request_id"], prefer_variant=prefer, stats_scale=scale)
    return d["request_id"], p


def test_admission_returns_execution_id_and_saved_plan_versions(svc):
    rid, p = _prepared(svc, window_days=20, prefer="staged")
    r = svc.execute(rid)
    assert r["admitted"] and r["execution_id"].startswith("exe_") and r["admission_ms"] < 1000
    out = svc.wait(r["execution_id"])
    assert out["state"] == "complete"
    phys = svc.registry.list_physical(p["plan_id"])
    assert {x["variant"] for x in phys} >= {"fused", "staged", "partitioned"}
    rec = out["receipt"]
    assert all(a["query_id"] for a in rec["attempts"] if a["node_id"] != "rate_interval")
    assert "left_join_preserves_cohort" in " ".join(rec["validations"]["passed"])


def test_saved_plan_definition_is_reused_with_rebound_parameters(svc):
    _, p1 = _prepared(svc, window_days=19, dimensions=["state", "channel"])
    _, p2 = _prepared(svc, window_days=18, dimensions=["state", "channel"])
    assert p1["plan_id"] == p2["plan_id"] and p2["plan_reused"] is True
    assert p1["semantic_fingerprint"] != p2["semantic_fingerprint"]


def test_checkpoint_adaptation_replans_only_unstarted_suffix(tmp_path):
    s = make_service(tmp_path, rows_per_partition=400)
    try:
        rid, p = _prepared(s, cohort_start="2026-07-01", cohort_end="2026-08-31", knowledge_cutoff="2026-09-30T23:59:59Z",
                           dimensions=["channel"], prefer="partitioned", stats_scale=0.25)
        r = s.execute(rid)
        out = s.wait(r["execution_id"])
        rec = out["receipt"]
        assert out["state"] == "complete"
        assert rec["plan"]["initial"]["partitions"] == 2 and rec["plan"]["actual"]["partitions"] > 2
        a = rec["plan"]["adaptations"][0]
        assert a["action"] == "replan_suffix" and a["expected_payoff"]["relative_saving"] >= 0.15
        assert "canonical_cohort" not in a["changed_nodes"]["removed"]
        assert set(a["equivalence_evidence"]["rules"]) <= {"RW_DISJOINT_HASH_PARTITION_COUNTS", "RW_STAGE_MATERIALIZE"}
        # completed prefix attempts were not re-run
        assert sum(1 for x in rec["attempts"] if x["node_id"] == "canonical_cohort") == 1
        # adapted result still equals the oracle
        asmt = s.assess(r["execution_id"])["assessment"]
        assert asmt["metrics"]["business_result_accuracy"]["value"] == 1.0
        assert len(s.registry.list_physical(p["plan_id"])) >= 4  # new version saved, old preserved
    finally:
        s.close()


def test_no_adaptation_inside_validity_range(svc):
    rid, p = _prepared(svc, window_days=17, prefer="partitioned")
    out = svc.wait(svc.execute(rid)["execution_id"])
    assert out["receipt"]["plan"]["adaptations"] == []
    ev = [e for e in svc.tracer.events(execution_id=out["execution_id"]) if e["stage"] == "checkpoint"]
    assert ev and ev[0]["status"] == "keep"


def test_cancellation_interrupts_and_emits_honest_receipt(tmp_path):
    s = make_service(tmp_path, engine_factory=lambda p: SlowEngine(p, delay=2.0))
    try:
        rid, _ = _prepared(s, prefer="staged")
        eid = s.execute(rid)["execution_id"]
        time.sleep(0.3)
        assert s.cancel(eid)["cancelled"]
        out = s.wait(eid, timeout=15)
        assert out["state"] == "cancelled" and out["result"] is None
        assert out["receipt"]["output"]["completeness"] == "none_published"
        assert any("no information set was published" in c for c in out["receipt"]["caveats"])
        assert s.cache.list() == []
        assert s.sweep()["dropped"] is not None
    finally:
        s.close()


def test_max_runtime_budget_cancels(tmp_path):
    s = make_service(tmp_path, engine_factory=lambda p: SlowEngine(p, delay=1.5))
    try:
        d = s.interpret(form=form(), constraints={"max_runtime_seconds": 1})
        s.confirm(d["request_id"], "t")
        assert s.prepare(d["request_id"], prefer_variant="staged")["status"] == "ready"  # estimate is far below 1s
        out = s.wait(s.execute(d["request_id"])["execution_id"], timeout=20)
        assert out["state"] == "cancelled" and "max_runtime" in out["error"]
    finally:
        s.close()


def test_recovery_reconciles_committed_ctas_without_resubmitting(tmp_path):
    s = make_service(tmp_path)
    rid, p = _prepared(s, prefer="staged")
    s.coordinator.stop()  # simulate a coordinator that dies after the worker's CTAS committed
    r = s.execute(rid)
    eid = r["execution_id"]
    plan = s.store.physical_plan(r["physical_id"])
    node = plan["nodes"][0]
    rel = f"stage.s_{eid.split('_')[-1]}_canonical_cohort_a1"
    sql = node["sql"]
    from aipr.runtime.coordinator import _restore_params
    s.engine.execute(f"CREATE TABLE {rel} AS\n{sql}", _restore_params(s.store.get_execution(eid)["params"]), "q_lost")
    s.store.execute("UPDATE node_runs SET state='submitted', attempts=1, output_relation=? WHERE execution_id=? AND node_id='canonical_cohort'",
                    (rel, eid))
    s.store.execute("INSERT INTO node_attempts(attempt_id, execution_id, node_id, physical_id, attempt_no, state, query_id, "
                    "output_relation, started_at) VALUES ('att_lost', ?, 'canonical_cohort', ?, 1, 'submitted', 'q_lost', ?, '2026-10-01')",
                    (eid, r["physical_id"], rel))
    s.store.set_execution(eid, state="running")
    from aipr.runtime.coordinator import Coordinator
    s.coordinator = Coordinator(s.store, s.engine, s.tracer, s.registry, s.settings, s._finalize)
    s.coordinator.start()
    try:
        out = s.wait(eid)
        assert out["state"] == "complete"
        attempts = [a for a in out["receipt"]["attempts"] if a["node_id"] == "canonical_cohort"]
        assert len(attempts) == 1 and attempts[0]["state"].endswith("reconciled")
        assert attempts[0]["query_id"] == "q_lost"
    finally:
        s.close()


def test_shared_producer_and_consumer_cancellation(tmp_path):
    s = make_service(tmp_path, engine_factory=lambda p: SlowEngine(p, delay=0.4))
    try:
        rid1, _ = _prepared(s, prefer="staged")
        rid2, _ = _prepared(s, prefer="staged")
        rid3, _ = _prepared(s, prefer="staged")
        e1 = s.execute(rid1)["execution_id"]
        w2 = s.execute(rid2)
        w3 = s.execute(rid3)
        assert w2["state"] == "waiting" and w2["waits_on"] == e1
        assert s.cancel(e1)["cancelled"] is False           # producer still needed
        assert s.cancel(w3["execution_id"])["cancelled"]     # consumer detaches
        out1, out2 = s.wait(e1, 30), s.wait(w2["execution_id"], 30)
        assert out1["state"] == out2["state"] == "complete"
        assert out2["result"]["origin"]["kind"] == "shared_producer"
        assert s.result(w3["execution_id"])["state"] == "cancelled"
    finally:
        s.close()


def test_queue_when_requested_then_admitted(svc):
    rid, _ = _prepared(svc, window_days=16, prefer="fused")
    r = svc.execute(rid, queue=True)
    assert r["state"] == "queued"
    out = svc.wait(r["execution_id"])
    assert out["state"] == "complete"
    assert any(e["status"] == "admitted_from_queue" for e in svc.tracer.events(execution_id=r["execution_id"]))


def test_budget_blocks_infeasible_plans(svc):
    d = svc.interpret(form=form(window_days=15), constraints={"execution_budget_credits": 1e-9})
    svc.confirm(d["request_id"], "t")
    p = svc.prepare(d["request_id"])
    assert p["status"] == "blocked" and "no feasible candidate" in p["blockers"][-1]


def test_join_explosion_alternative_rejected_with_rule(svc):
    _, p = _prepared(svc, window_days=14)
    rej = p["rejected_alternatives"][0]
    assert rej["rule"] == "quote_grain_violation" and rej["plan"] == "raw_join"
    raw = p["join_envelopes"]["raw_join"]
    assert raw["rows_est"] >= p["join_envelopes"]["bind_outcome"]["rows_est"]
    assert raw["max_multiplicity"] >= 2 and raw["heavy_hitters"]
