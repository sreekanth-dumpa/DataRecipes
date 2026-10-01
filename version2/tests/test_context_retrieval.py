"""Obligation-directed retrieval and evidence manifest (spec section 5)."""
from aipr.context.obligations import derive_obligations
from aipr.context.retrieval import retrieve

from conftest import form


def _intent(svc, **kw):
    d = svc.interpret(form=form(**kw))
    return d["record"]["intent"]


def test_manifest_covers_all_mandatory_obligations_with_certified_claims(svc):
    intent = _intent(svc)
    m = retrieve(svc.evidence, intent, derive_obligations(intent)).to_dict()
    assert m["complete"] and m["completeness"] == 1.0
    assert {e["claim_id"] for e in m["entries"]} == {"EV-QID-001", "EV-BIND-002", "EV-WIN-003", "EV-AUTH-004", "EV-AGG-005"}
    for e in m["entries"]:
        assert e["authority_class"] == "certified" and e["source_hash"].startswith("sha256:")
        assert e["retrieval_reason"] and e["obligation_ids"]


def test_similarity_never_supplies_authority(svc):
    intent = _intent(svc)
    m = retrieve(svc.evidence, intent, derive_obligations(intent)).to_dict()
    rej = next(r for r in m["rejected"] if r["claim_id"] == "EV-DOC-901")
    assert "cannot discharge" in rej["reason"]
    assert any(c["claim_id"] == "EV-DOC-901" and c["obligation_id"] == "bind_linkage" for c in m["contradictions"])


def test_missing_obligation_blocks_with_specific_gap(svc):
    intent = _intent(svc, cohort_start="2025-06-01", cohort_end="2025-06-30", knowledge_cutoff="2025-08-15T23:59:59Z")
    m = retrieve(svc.evidence, intent, derive_obligations(intent))
    assert not m.complete
    assert any("as of 2025-08-15" in g["gap"] for g in m.missing_obligations)
    d = svc.interpret(form=form(cohort_start="2025-06-01", cohort_end="2025-06-30", knowledge_cutoff="2025-08-15T23:59:59Z"))
    svc.confirm(d["request_id"], "t")
    p = svc.prepare(d["request_id"])
    assert p["status"] == "blocked" and any("No certified, applicable claim" in b for b in p["blockers"])


def test_packet_catalog_has_twenty_families(svc):
    assert [p["id"] for p in svc.evidence.packets] == [f"P{i:02d}" for i in range(1, 21)]
