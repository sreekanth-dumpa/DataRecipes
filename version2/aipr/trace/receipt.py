"""Execution receipt (spec section 14).  A receipt documents evidence; it is not
a proof of business truth.  Missing observations carry a reason, never a
fabricated zero."""
from __future__ import annotations

import json
from typing import Any

from .. import COMPILER_VERSION
from ..core.clock import now_iso
from ..core.hashing import fingerprint


def build_receipt(*, execution: dict[str, Any], record: dict[str, Any], prep: dict[str, Any],
                  initial_plan: dict[str, Any] | None, actual_plan: dict[str, Any] | None,
                  attempts: list[dict[str, Any]], node_runs: dict[str, dict[str, Any]],
                  adaptations: list[dict[str, Any]], result_rows: list[dict[str, Any]] | None,
                  origin: dict[str, Any], cache_publication: dict[str, Any] | None,
                  verification: dict[str, Any] | None = None) -> dict[str, Any]:
    vals_passed, vals_failed = [], []
    for nid, r in node_runs.items():
        for v in r.get("observed", {}).get("validations", []):
            (vals_passed if v["passed"] else vals_failed).append(f"{nid}:{v['id']}")
    unverified = []
    if actual_plan and actual_plan["variant"] == "fused":
        unverified.append("denominator_conservation (fused route has no materialised cohort to reconcile against)")
    est = (prep.get("selected") or {}).get("estimate", {})
    started, finished, created = execution.get("started_at"), execution.get("finished_at"), execution.get("created_at")

    def secs(a, b):
        from datetime import datetime
        if not a or not b:
            return None
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds(), 4)

    duration = secs(started, finished)
    immature = sum(int(r.get("immature_excluded", 0)) for r in (result_rows or []))
    receipt = {
        "receipt_version": "1",
        "execution_id": execution["execution_id"], "request_id": execution["request_id"],
        "trace_id": (execution.get("context") or {}).get("trace_id"),
        "status": execution["state"], "error": execution.get("error"), "issued_at": now_iso(),
        "confirmed_intent": {"intent_hash": execution["intent_hash"], "confirmation": record["confirmation"],
                             "semantic_fingerprint": execution["semantic_fingerprint"], "intent": record["intent"]},
        "versions": {
            "recipe": prep.get("recipe_key"), "recipe_hash": prep.get("recipe_hash"),
            "evidence_manifest_hash": (prep.get("evidence") or {}).get("manifest_hash"),
            "evidence_claims": [e["claim_id"] for e in (prep.get("evidence") or {}).get("entries", [])],
            "source_cut": prep.get("source_cut"), "source_cut_fingerprint": execution["source_cut_fingerprint"],
            "access": {"fingerprint": execution["access_fingerprint"],
                       "policy_version": (prep.get("access") or {}).get("policy_version"),
                       "subject": execution.get("subject")},
            "compiler_version": COMPILER_VERSION, "binding_hash": (prep.get("binding") or {}).get("binding_hash"),
        },
        "plan": {
            "plan_id": execution.get("plan_id"), "logical_version": (execution.get("context") or {}).get("logical_version"),
            "plan_definition_reused": prep.get("plan_reused"),
            "initial": _plan_ref(execution.get("initial_physical_id"), initial_plan),
            "actual": _plan_ref(execution.get("current_physical_id"), actual_plan),
            "adaptations": adaptations, "rejected_alternatives": prep.get("rejected_alternatives", []),
            "selection_reason": prep.get("selection_reason"),
        },
        "attempts": [{"node_id": a["node_id"], "attempt_no": a["attempt_no"], "state": a["state"],
                      "query_id": a.get("query_id"), "output_relation": a.get("output_relation"),
                      "elapsed_ms": a["observed"].get("elapsed_ms"), "rows": a["observed"].get("rows"),
                      "error": a.get("error")} for a in attempts],
        "reused_artifacts": {"result_origin": origin, "profile_reused": (prep.get("profile") or {}).get("reused"),
                             "plan_definition_reused": prep.get("plan_reused")},
        "validations": {"passed": sorted(vals_passed), "failed": sorted(vals_failed), "unverified": unverified},
        "estimate": est.get("duration_seconds"), "estimate_credits_notional": est.get("credits_notional"),
        "estimate_model": est.get("model"),
        "actual": {"queue_seconds": secs(created, started), "execution_seconds": duration,
                   "control_plane_admission_ms": (execution.get("context") or {}).get("admission_ms")},
        "cost": {"status": "unavailable",
                 "reason": "local engine has no billing telemetry; Snowflake attribution arrives later and is reconciled separately"},
        "output": {"grain": record["intent"]["dimensions"], "rows": len(result_rows) if result_rows is not None else None,
                   "result_digest": fingerprint(result_rows) if result_rows is not None else None,
                   "exactness": "exact" if result_rows is not None else None,
                   "completeness": "complete" if execution["state"] == "complete" else "none_published",
                   "censoring": {"immature_excluded": immature, "policy": record["intent"]["maturity_policy"]}},
        "cache_publication": cache_publication,
        "verification_scope": verification or {"class": "not_independently_verified",
                                                "note": "structural and temporal checks passed; business truth not adjudicated"},
        "telemetry": {"spill": "unavailable: engine does not expose per-query spill locally",
                      "operator_stats": "unavailable: local engine", "insights": "not_collected"},
        "caveats": _caveats(record, actual_plan, immature, execution),
    }
    receipt["receipt_hash"] = fingerprint(receipt)
    return receipt


def _plan_ref(pid: str | None, plan: dict[str, Any] | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {"physical_id": pid, "variant": plan["variant"], "partitions": plan["partitions"],
            "physical_fingerprint": plan["physical_fingerprint"], "nodes": [n["node_id"] for n in plan["nodes"]]}


def _caveats(record: dict[str, Any], plan: dict[str, Any] | None, immature: int, execution: dict[str, Any]) -> list[str]:
    c = []
    if immature:
        c.append(f"{immature} immature quotes excluded from numerator and denominator (incomplete observation window)")
    c.append("differences between groups are observed associations, not causal effects")
    c.append("cancelled binds count as conversions under recipe v1.0.0")
    if execution["state"] != "complete":
        c.append(f"run ended '{execution['state']}': no information set was published")
    return c


def save_receipt(artifacts_dir, receipt: dict[str, Any]) -> str:
    p = artifacts_dir / "receipts" / f"{receipt['execution_id']}.json"
    p.write_text(json.dumps(receipt, indent=2, default=str))
    return str(p)
