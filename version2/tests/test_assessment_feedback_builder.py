"""Assessment, feedback lifecycle, recipe builder and TLP oracle (spec sections 16-18)."""
import json
from pathlib import Path

import pytest

from aipr.verify.tlp import tlp_check
from aipr.verify.verieql_adapter import check_equivalence

from conftest import form, run


def test_assessment_gate_vector_and_null_with_reason(svc):
    _, _, out = run(svc, window_days=9)
    a = svc.assess(out["execution_id"], feedback="the chart labels were confusing")
    doc = a["assessment"]
    assert doc["gate_vector"]["semantic_admission"] is True
    assert doc["gate_vector"]["result_verification"] == "fixture_oracle_agreement"
    m = doc["metrics"]
    assert m["intent_accuracy"]["value"] is None and m["intent_accuracy"]["reason"]
    assert m["cost_efficiency"]["value"] is None and "billing" in m["cost_efficiency"]["reason"]
    assert any(r["change_class"] == "presentation_change" for r in doc["recommendations"])
    d = Path(a["case_path"])
    for f in ("assessment.md", "case.json", "trace_refs.json", "expected_outputs.json", "proposed_patch.json", "promotion.json"):
        assert (d / f).exists(), f


def test_cached_followup_assessed_separately(svc):
    _, _, fresh = run(svc, window_days=8)
    _, _, cached = run(svc, window_days=8)
    doc = svc.assess(cached["execution_id"])["assessment"]
    assert doc["metrics"]["cache_quality"]["value"] == 0
    assert doc["metrics"]["latency"]["case"].startswith("application_cache")
    assert doc["metrics"]["repeated_answer_consistency"]["value"] == 1.0


def test_approved_feedback_is_advisory_and_retrieved_for_matching_scope(svc):
    _, _, out = run(svc, window_days=7)
    case_id = svc.assess(out["execution_id"], feedback="took too long")["assessment"]["case_id"]
    svc.transition_case(case_id, "approved", "owner", "advisory only")
    d = svc.interpret(form=form(window_days=6))
    svc.confirm(d["request_id"], "t")
    p = svc.prepare(d["request_id"])
    assert any(a["case_id"] == case_id and a["binding"] == "advisory_only" for a in p["advisories"])
    with pytest.raises(PermissionError):
        svc.transition_case(case_id, "active", "owner")


def test_feedback_not_retrieved_for_other_scope(svc):
    d = svc.interpret(form=form(state="VA"), subject="analyst_all")
    svc.confirm(d["request_id"], "t")
    p = svc.prepare(d["request_id"])
    assert p["advisories"] == []


def test_recipe_builder_matching_and_unsupported_drafts(svc):
    q = svc.builder_questions("count quotes that bind within 30 days; ignore cancellations")
    assert len(q["questions"]) == 6
    good = svc.builder_draft("Quote conversion clone", "first quote, bind within 30 days", q["suggested"], "owner@x")
    assert good["status"] == "draft" and good["algebra_match"]["matches"]
    assert good["certification"]["status"] == "not_certified"
    assert all(c["expected_outcome"] is None for c in good["tests"]["adversarial_cases"])
    bad_answers = {**q["suggested"], "numerator_event": "policy_issuance", "aggregation": "average_rates"}
    bad = svc.builder_draft("Issuance conversion", "policy issued", bad_answers, "owner@x")
    assert not bad["algebra_match"]["matches"]
    whys = " ".join(u["why"] for u in bad["algebra_match"]["unsupported_meanings"])
    assert "policy issuance" in whys and "averaging" in whys
    assert bad["version"].endswith("-draft")


def test_tlp_metamorphic_oracle(svc):
    base = "SELECT quote_id, channel_code, eligible FROM src_quote.quote_iteration"
    r = tlp_check(svc.engine, base, "eligible AND channel_code = 'A'")
    assert r["verdict"] == "no_discrepancy_observed" and r["base_rows"] == r["partitioned_rows"]
    nul = tlp_check(svc.engine, "SELECT bind_event_id, NULLIF(event_type, 'CANCEL') AS t FROM src_policy.bind_event",
                    "NULLIF(event_type, 'CANCEL') = 'BIND'")
    assert nul["verdict"] == "no_discrepancy_observed"
    agg = tlp_check(svc.engine, "SELECT COUNT(*) FROM src_quote.quote_iteration", "eligible")
    assert agg["verdict"] == "unsupported"


def test_verieql_adapter_never_claims_verification():
    r = check_equivalence("SELECT 1", "SELECT 1", ["quote_id unique"])
    assert r["verdict"] in ("unsupported", "unknown") and r["counts_as_verified"] is False


def test_assessment_ignores_coarser_peers_and_certification_bypasses_cache(svc):
    _, _, coarse = run(svc, window_days=5, dimensions=["product_version"])
    rid, p, fine = run(svc, window_days=5)
    assert p["status"] == "ready"  # finer grain cannot come from the coarser cache
    doc = svc.assess(fine["execution_id"])["assessment"]
    assert doc["gate_vector"]["structural_validity"] is True
    rid2, p2, _ = run(svc, window_days=5)
    assert p2["status"] == "cache_hit"
    cert = svc.certify_equivalence(rid2)["certificate"]
    assert cert["evidence_class"] == "empirically_equivalent"
