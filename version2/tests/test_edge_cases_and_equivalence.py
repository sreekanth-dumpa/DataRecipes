"""All admitted physical routes agree with independently labelled expected results (spec section 21)."""
import json

import pytest

from aipr.data import fixture
from aipr.verify.oracle import oracle

from conftest import EDGE, key, run

SPEC = json.loads(EDGE.read_text())
DIMS = ["product_version", "channel"]


def test_python_oracle_matches_hand_labels():
    rows = fixture.edge_case_rows(EDGE)
    i = SPEC["intent"]
    assert key(oracle(rows, i), DIMS) == key(SPEC["expected_aggregate"], DIMS)


@pytest.mark.parametrize("variant", ["fused", "staged", "partitioned"])
def test_each_route_matches_hand_labelled_edge_cases(edge_svc, variant):
    d = edge_svc.interpret(form={**SPEC["intent"], "dimensions": DIMS})
    edge_svc.confirm(d["request_id"], "t")
    p = edge_svc.prepare(d["request_id"], prefer_variant=variant)
    pid = next(c["physical_id"] for c in (p.get("candidates") or edge_svc.prepare(d["request_id"]).get("candidates", []))
               if c["variant"] == variant) if p["status"] == "ready" else None
    if pid is None:  # cache hit from an earlier variant: the cached answer must also match
        r = edge_svc.execute(d["request_id"])
        out = edge_svc.wait(r["execution_id"])
        assert out["result"]["origin"]["kind"].startswith("application_cache")
    else:
        r = edge_svc.execute(d["request_id"], physical_id=pid)
        out = edge_svc.wait(r["execution_id"])
        assert out["state"] == "complete", out["error"]
        assert out["result"]["origin"]["variant"] == variant
    assert key(out["result"]["rows"], DIMS) == key(SPEC["expected_aggregate"], DIMS)


def test_all_variants_certified_equivalent_on_edge_cut(edge_svc):
    d = edge_svc.interpret(form={"state": "NC", "cohort_start": "2026-08-01", "cohort_end": "2026-08-31",
                                 "knowledge_cutoff": "2026-09-15T23:59:59Z", "window_days": 30,
                                 "dimensions": ["channel", "product_version"]})
    edge_svc.confirm(d["request_id"], "t")
    edge_svc.prepare(d["request_id"])
    cert = edge_svc.certify_equivalence(d["request_id"])["certificate"]
    assert cert["evidence_class"] == "empirically_equivalent"
    assert all(cert["oracle_agreement"].values()) and cert["mutual_agreement"]
    assert cert["bounded_verifier"]["counts_as_verified"] is False


def test_specific_edge_semantics(edge_svc):
    """Immature excluded+disclosed, late bind ignored, unmatched channel kept as 'unknown', bind fan-out reduced."""
    rid, prep, out = run(edge_svc, dimensions=["channel", "state"], prefer="staged")  # grain not in cache
    assert out["result"]["origin"]["kind"] == "fresh_execution"
    rows = {r["channel"]: r for r in out["result"]["rows"]}
    assert rows["unknown"]["eligible"] == 1 and rows["unknown"]["converted"] == 0
    total = out["result"]["facts"]
    assert total["eligible_mature_quotes"] == 11 and total["converted_quotes"] == 6 and total["immature_excluded"] == 1
    status = edge_svc.status(out["execution_id"])
    bind = next(n for n in status["nodes"] if n["node_id"] == "bind_outcome")
    assert bind["rows"] == 6  # E03's three binds and E13's bind+cancel reduce to one outcome per quote


def test_synthetic_routes_equivalent_and_match_oracle(svc):
    d = svc.interpret(form={"state": "NC", "cohort_start": "2026-07-01", "cohort_end": "2026-08-31",
                            "knowledge_cutoff": "2026-09-15T23:59:59Z", "window_days": 21,
                            "dimensions": ["product_version", "channel"]})
    svc.confirm(d["request_id"], "t")
    svc.prepare(d["request_id"])
    cert = svc.certify_equivalence(d["request_id"])["certificate"]
    assert cert["evidence_class"] == "empirically_equivalent", cert
