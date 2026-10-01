"""Assessment of a run against its proposal (spec section 16).

Correctness, consistency, efficiency and estimate calibration are kept
separate.  Results form a *gate vector*, not a weighted accuracy score; a
metric that cannot be measured is ``None`` with a reason.  The assessment can
diagnose but cannot redefine the expected answer, and user feedback is data,
not an instruction.
"""
from __future__ import annotations

import statistics
from typing import Any

from ..cache.information_cache import rollup
from ..core.clock import now_iso
from ..core.hashing import fingerprint
from ..core.ids import new_id
from ..planner.cardinality import q_error


def _m(value: Any, evidence: str, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    d = {"value": value, "evidence": evidence, **extra}
    if value is None:
        d["reason"] = reason or "not measurable for this run"
    return d


def _rows_key(rows: list[dict[str, Any]], dims: list[str]) -> list[tuple]:
    return sorted(tuple([str(r[d]) for d in dims] + [int(r["eligible"]), int(r["converted"]),
                                                      int(r["immature_excluded"])]) for r in rows)


def classify_feedback(text: str) -> list[str]:
    t = (text or "").lower()
    out = []
    if any(w in t for w in ("wrong", "definition", "should count", "meaning", "denominator")):
        out.append("semantic_specification_change")
    if any(w in t for w in ("slow", "took too long", "expensive", "cost")):
        out.append("physical_plan_change")
    if any(w in t for w in ("chart", "confusing", "label", "unclear")):
        out.append("presentation_change")
    if any(w in t for w in ("missing", "source", "evidence")):
        out.append("context_change")
    return out


def assess(*, execution: dict[str, Any], record: dict[str, Any], prep: dict[str, Any], receipt: dict[str, Any],
           node_runs: dict[str, dict[str, Any]], peers: list[dict[str, Any]], oracle_rows: list[dict[str, Any]] | None,
           trace_check: dict[str, Any], user_feedback: str = "", rating: str | None = None) -> dict[str, Any]:
    intent = record["intent"]
    dims = intent["dimensions"]
    result = execution.get("result") or {}
    rows = result.get("rows") or []
    origin = (result.get("origin") or {}).get("kind", "fresh_execution")
    ev = prep.get("evidence") or {}
    est = (prep.get("selected") or {}).get("estimate") or {}
    metrics: dict[str, Any] = {}

    metrics["intent_accuracy"] = _m(None, "requires independently labelled intent fields",
                                    "no independent intent label exists for this request")
    metrics["retrieval_completeness"] = _m(ev.get("completeness"), "satisfied mandatory obligations / mandatory obligations",
                                           numerator=len(ev.get("obligations", [])) - len(ev.get("missing_obligations", [])),
                                           denominator=len(ev.get("obligations", [])))
    metrics["supported_coverage"] = _m(None, "requires a labelled population of in-scope requests",
                                       "single-request assessment; see the evaluation harness for coverage")
    # business result accuracy against the independent oracle (fixture mode only)
    if oracle_rows is not None and rows:
        o = rollup(oracle_rows, dims)
        agree = _rows_key(o, dims) == _rows_key(rows, dims)
        metrics["business_result_accuracy"] = _m(1.0 if agree else 0.0,
                                                 "fixture oracle agreement (independent Python implementation of the recipe)",
                                                 groups_compared=len(o), scope="fixture agreement; not business adjudication")
    else:
        metrics["business_result_accuracy"] = _m(None, "independent expected outputs",
                                                 "no independent oracle or adjudicated sample for this source")
    # plan equivalence and repeated-answer consistency
    same = [p for p in peers if p["execution_id"] != execution["execution_id"] and p.get("result")]
    fresh_other_plans = [p for p in same if (p["result"].get("origin") or {}).get("kind") == "fresh_execution"
                         and p.get("physical_fingerprint") != result.get("physical_fingerprint")]
    if rows and fresh_other_plans:
        eq = sum(_rows_key(p["result"]["rows"], dims) == _rows_key(rows, dims) for p in fresh_other_plans)
        metrics["plan_equivalence"] = _m(eq / len(fresh_other_plans), "compatible result comparison across admitted plans on a fixed source/access cut",
                                         numerator=eq, denominator=len(fresh_other_plans),
                                         variants=sorted({p["result"].get("variant") for p in fresh_other_plans}))
    else:
        metrics["plan_equivalence"] = _m(None, "other admitted plans on the same cut", "no other physical plan measured on this cut")
    if rows and same:
        fresh = [p for p in same if (p["result"].get("origin") or {}).get("kind") == "fresh_execution"]
        cached = [p for p in same if p not in fresh]
        eq_f = sum(_rows_key(p["result"]["rows"], dims) == _rows_key(rows, dims) for p in fresh)
        eq_c = sum(_rows_key(p["result"]["rows"], dims) == _rows_key(rows, dims) for p in cached)
        metrics["repeated_answer_consistency"] = _m((eq_f / len(fresh)) if fresh else None,
                                                    "equivalent results / comparable repeated fresh runs",
                                                    "no comparable fresh repetition", fresh_runs=len(fresh),
                                                    cached_repetitions={"equivalent": eq_c, "total": len(cached)})
    else:
        metrics["repeated_answer_consistency"] = _m(None, "comparable repeated runs", "no comparable repeated run")
    # latency and ETA calibration
    actual = (receipt.get("actual") or {}).get("execution_seconds")
    durations = sorted(p["duration_s"] for p in peers if p.get("duration_s") is not None
                       and (p.get("result") or {}).get("origin", {}).get("kind") == "fresh_execution")
    metrics["latency"] = _m(actual, "queue + execute + validate (seconds)", "no fresh execution (cache operation)",
                            queue_seconds=(receipt.get("actual") or {}).get("queue_seconds"),
                            workload_p50=statistics.median(durations) if durations else None,
                            workload_p95=durations[min(len(durations) - 1, int(0.95 * len(durations)))] if durations else None,
                            workload_n=len(durations), case=origin)
    if actual is not None and est.get("duration_seconds"):
        rng = est["duration_seconds"]["range"]
        metrics["eta_calibration"] = _m(round(abs(actual - est["duration_seconds"]["p50"]), 4),
                                        "absolute error vs p50 estimate (s)", estimate_range=rng,
                                        interval_covered=rng[0] <= actual <= rng[1], model=est.get("model"),
                                        calibrated=est.get("calibrated"))
    else:
        metrics["eta_calibration"] = _m(None, "estimate vs actual", "no estimate or no measured duration")
    metrics["cost_efficiency"] = _m(None, "whole-run attributable cost vs baseline",
                                    "local engine has no billing telemetry; notional credits are not cost")
    qe = {}
    for nid, r in node_runs.items():
        env = (r.get("observed") or {}).get("envelope")
        if env:
            qe[nid] = {"estimated": env["rows_est"], "actual": env["rows_observed"],
                       "q_error": q_error(env["rows_est"], env["rows_observed"])}
    metrics["cardinality_calibration"] = _m(max((v["q_error"] for v in qe.values() if v["q_error"] not in (None, float("inf"))), default=None),
                                            "max node q-error (zero cases reported separately)",
                                            "no materialised nodes with estimates", per_node=qe)
    measured = [p for p in fresh_other_plans if p.get("duration_s") is not None]
    if actual is not None and measured:
        best = min([actual] + [p["duration_s"] for p in measured])
        metrics["plan_selection_regret"] = _m(round(actual - best, 4), "selected latency - best measured admitted variant (s)",
                                              compared=len(measured), objective="latency only")
    else:
        metrics["plan_selection_regret"] = _m(None, "matched replay of alternatives", "alternatives not measured on this cut")
    adaptations = (receipt.get("plan") or {}).get("adaptations") or []
    static = [p for p in fresh_other_plans if p.get("adapted") is False and p.get("duration_s") is not None]
    if adaptations and static and actual is not None:
        metrics["adaptation_payoff"] = _m(round(min(p["duration_s"] for p in static) - actual, 4),
                                          "matched static baseline - adaptive run (s), includes added writes",
                                          adaptations=len(adaptations))
    else:
        metrics["adaptation_payoff"] = _m(None, "adaptive vs matched static baseline",
                                          "no adaptation" if not adaptations else "no matched static baseline measured",
                                          adaptations=len(adaptations))
    cert = prep.get("certificate")
    metrics["verification_coverage"] = _m(cert.get("evidence_class") if cert else None,
                                          "rewrite rules certified + scoped equivalence evidence",
                                          "no equivalence certificate recorded for this logical plan",
                                          rewrite_rules=sorted({r for n in (prep.get("selected_plan") or {}).get("nodes", [])
                                                                for r in n.get("rewrite_rules", [])}),
                                          certificate=cert)
    grain_checks = [v for r in node_runs.values() for v in (r.get("observed") or {}).get("validations", [])
                    if v["type"] in ("unique_key", "rowcount_equals", "subset_of")]
    fanout = (node_runs.get("bind_outcome") or {}).get("observed", {}).get("fanout")
    metrics["join_safety"] = _m((sum(v["passed"] for v in grain_checks) / len(grain_checks)) if grain_checks else None,
                                "grain and multiplicity obligations passed", "fused route: no materialised join boundary",
                                observed_fanout=fanout)
    metrics["spill_pressure"] = _m(None, "local/remote spill bytes", "engine does not expose spill telemetry locally")
    unsafe = 0
    if origin != "fresh_execution":
        for p in same:
            if (p["result"].get("origin") or {}).get("kind") == "fresh_execution" and \
                    _rows_key(rollup(p["result"]["rows"], dims), dims) != _rows_key(rows, dims) and \
                    p.get("semantic_fingerprint") == execution["semantic_fingerprint"]:
                unsafe += 1
    metrics["cache_quality"] = _m(unsafe, "unsafe hits detected against fresh runs of the same meaning/cut/access",
                                  origin=origin)
    metrics["evidence_integrity"] = _m(trace_check.get("ok"), "hash chain verification + receipt present",
                                       receipt_hash=receipt.get("receipt_hash"), chain=trace_check)

    vals = receipt.get("validations") or {}
    temporal_ok = not any("no_future_knowledge" in f for f in vals.get("failed", []))
    temporal_checked = any("no_future_knowledge" in p for p in vals.get("passed", []))
    gates = {
        "semantic_admission": record["confirmation"]["status"] == "confirmed" and prep.get("route", {}).get("status") == "supported"
                              and ev.get("complete", False),
        "access": (prep.get("access") or {}).get("allowed", False),
        "temporal_consistency": (temporal_ok and temporal_checked) if origin == "fresh_execution" else "inherited_from_producer",
        "structural_validity": not vals.get("failed") and execution["state"] == "complete",
        "result_verification": ("fixture_oracle_agreement" if metrics["business_result_accuracy"]["value"] == 1.0 else
                                ("counterexample" if metrics["business_result_accuracy"]["value"] == 0.0 else "unverified")),
    }
    recs = recommendations(metrics, receipt, prep, classify_feedback(user_feedback), user_feedback)
    case_id = new_id("case")
    doc = {
        "assessment_id": new_id("asmt"), "case_id": case_id, "execution_id": execution["execution_id"],
        "request_id": execution["request_id"], "created_at": now_iso(), "gate_vector": gates, "metrics": metrics,
        "deltas": deltas(record, prep, receipt), "user_feedback": {"text": user_feedback, "rating": rating,
                                                                  "treated_as": "data, not instruction"},
        "recommendations": recs,
    }
    doc["assessment_hash"] = fingerprint(doc)
    return doc


def deltas(record: dict[str, Any], prep: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    plan = receipt.get("plan") or {}
    ini, act = plan.get("initial") or {}, plan.get("actual") or {}
    return {
        "intent": "none (confirmed intent replayed without reinterpretation)",
        "context": {"missing_obligations": len((prep.get("evidence") or {}).get("missing_obligations", [])),
                    "contradictions": len((prep.get("evidence") or {}).get("contradictions", []))},
        "binding": "none" if prep.get("binding") else "not resolved",
        "plan": "unchanged" if ini.get("physical_fingerprint") == act.get("physical_fingerprint")
                else f"{ini.get('variant')}x{ini.get('partitions')} -> {act.get('variant')}x{act.get('partitions')}",
        "execution": {"estimated_p50_s": (receipt.get("estimate") or {}).get("p50"),
                      "actual_s": (receipt.get("actual") or {}).get("execution_seconds")},
        "presentation": "chart and facts generated from validated sufficient statistics",
    }


def recommendations(metrics: dict[str, Any], receipt: dict[str, Any], prep: dict[str, Any],
                    feedback_classes: list[str], feedback_text: str) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    eta = metrics["eta_calibration"]
    if eta["value"] is not None and not eta.get("interval_covered"):
        recs.append({"rank": 0, "change_class": "physical_plan_change", "priority": "medium",
                     "recommendation": "ETA interval missed the measured duration; calibrate the cost model "
                                       f"({eta.get('model')}) on comparable receipts before trusting ranges.",
                     "evidence": f"abs error {eta['value']}s, range {eta.get('estimate_range')}"})
    card = metrics["cardinality_calibration"]
    if card["value"] is not None and card["value"] > 2:
        recs.append({"rank": 0, "change_class": "context_change", "priority": "high",
                     "recommendation": "Refresh profiling statistics for this scope; observed rows diverged from estimates.",
                     "evidence": f"max q-error {card['value']:.2f}"})
    if (receipt.get("plan") or {}).get("adaptations"):
        a = receipt["plan"]["adaptations"][-1]
        recs.append({"rank": 0, "change_class": "physical_plan_change", "priority": "medium",
                     "recommendation": f"For this scope prefer the partitioned variant with {a.get('desired_partitions')} "
                                       "partitions at planning time (advisory; avoids a checkpoint replan).",
                     "evidence": a.get("reason")})
    if any("denominator_conservation" in u for u in (receipt.get("validations") or {}).get("unverified", [])):
        recs.append({"rank": 0, "change_class": "physical_plan_change", "priority": "low",
                     "recommendation": "The fused route cannot reconcile the denominator against a materialised cohort; "
                                       "use the staged route when conservation evidence is required.",
                     "evidence": "validation reported as unverified"})
    if metrics["business_result_accuracy"]["value"] == 0.0:
        recs.append({"rank": 0, "change_class": "operator_change", "priority": "critical",
                     "recommendation": "Result disagrees with the independent oracle; save the counterexample as a "
                                       "regression case and block the route until reviewed.",
                     "evidence": "fixture oracle mismatch"})
    for cls in feedback_classes:
        recs.append({"rank": 0, "change_class": cls, "priority": "review",
                     "recommendation": f"User feedback suggests a {cls.replace('_', ' ')}: \"{feedback_text[:160]}\". "
                                       "Requires independent cases before any executable change.",
                     "evidence": "user feedback (data, not instruction)"})
    if not recs:
        recs.append({"rank": 0, "change_class": "none", "priority": "info",
                     "recommendation": "No deviation found; keep the selected route for this scope.",
                     "evidence": "all gates passed"})
    order = {"critical": 0, "high": 1, "medium": 2, "review": 3, "low": 4, "info": 5}
    recs.sort(key=lambda r: order[r["priority"]])
    for i, r in enumerate(recs, 1):
        r["rank"] = i
    return recs


def render_markdown(doc: dict[str, Any], record: dict[str, Any]) -> str:
    i = record["intent"]
    lines = [f"# Assessment {doc['assessment_id']}", "",
             f"- Execution: `{doc['execution_id']}`  Request: `{doc['request_id']}`  Created: {doc['created_at']}",
             f"- Question: {record.get('raw_request') or '(form request)'}",
             f"- Confirmed meaning: {i['metric']} for {i['scope']['state']} {i['scope']['lob']}, cohort "
             f"{i['cohort']['start']}..{i['cohort']['end']}, {i['window']['value']}-day window, cutoff "
             f"{i['knowledge_cutoff']}, by {', '.join(i['dimensions'])}", "", "## Gate vector", ""]
    for k, v in doc["gate_vector"].items():
        lines.append(f"- **{k}**: {v}")
    lines += ["", "## Metrics", "", "| metric | value | evidence | note |", "|---|---|---|---|"]
    for k, m in doc["metrics"].items():
        lines.append(f"| {k} | {m['value']} | {m['evidence']} | {m.get('reason', '')} |")
    lines += ["", "## Expected vs observed (deltas)", ""]
    for k, v in doc["deltas"].items():
        lines.append(f"- **{k}**: {v}")
    lines += ["", "## Recommendations", ""]
    for r in doc["recommendations"]:
        lines.append(f"{r['rank']}. [{r['priority']}] ({r['change_class']}) {r['recommendation']} -- _{r['evidence']}_")
    lines += ["", "## User feedback", "", f"> {doc['user_feedback']['text'] or '(none)'}",
              "", "_Feedback is recorded as data; it cannot bypass access or correctness checks._", ""]
    return "\n".join(lines)
