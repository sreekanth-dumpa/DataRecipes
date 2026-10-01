"""End-to-end walkthrough of every workflow phase on the synthetic fixture.

    python demo.py            (from version2/)
"""
from __future__ import annotations

import json

from aipr.service import AIPRService

Q = ("What was the 30 day quote conversion for NC personal auto quotes issued in August 2026 "
     "by product version and channel, as known by Sept 15 2026?")


def show(title, obj=None):
    print(f"\n=== {title}")
    if obj is not None:
        print(json.dumps(obj, indent=2, default=str)[:1800])


def main() -> None:
    svc = AIPRService()
    try:
        d = svc.interpret(Q)
        show("Interpret", {"route": d["route"]["status"], "intent": d["record"]["intent"],
                           "assumptions": d["record"]["assumptions"]})
        mq = next(q for q in d["questions"] if q["id"] == "maturity_tradeoff")
        show("Material clarification", mq["question"])
        rid = d["request_id"]
        svc.confirm(rid, "analyst_nc", {"maturity_tradeoff": "exclude_without_complete_window"})
        p = svc.prepare(rid)
        show("Prepare (retrieve, resolve, plan, estimate)", {
            "status": p["status"], "claims": [e["claim_id"] for e in p.get("evidence", {}).get("entries", [])],
            "cache": p.get("cache", {}).get("kind"),
            "candidates": [(c["label"], c["estimate"]["duration_seconds"]["p50"], c["on_pareto_frontier"])
                           for c in p.get("candidates", [])],
            "selected": (p.get("selected") or {}).get("label"), "reason": p.get("selection_reason"),
            "rejected": p.get("rejected_alternatives")})
        r = svc.execute(rid)
        out = svc.wait(r["execution_id"])
        show("Execute + present", {"state": out["state"], "narrative": out["result"]["narrative"],
                                   "origin": out["result"]["origin"]})
        d2 = svc.interpret(form={"state": "NC", "cohort_start": "2026-08-01", "cohort_end": "2026-08-31",
                                 "knowledge_cutoff": "2026-09-15T23:59:59Z", "dimensions": ["product_version"]})
        svc.confirm(d2["request_id"], "analyst_nc")
        p2 = svc.prepare(d2["request_id"])
        out2 = svc.wait(svc.execute(d2["request_id"])["execution_id"])
        show("Follow-up served by derived cache roll-up", {"prepare": p2["status"], "origin": out2["result"]["origin"],
                                                          "rows": out2["result"]["rows"]})
        cert = svc.certify_equivalence(rid)
        show("Equivalence across admitted physical routes", cert["certificate"])
        a = svc.assess(r["execution_id"], feedback="")
        show("Assessment gate vector", a["assessment"]["gate_vector"])
        print("case folder:", a["case_path"])
        show("Trace chain", svc.tracer.verify_chain())
    finally:
        svc.close()


if __name__ == "__main__":
    main()
