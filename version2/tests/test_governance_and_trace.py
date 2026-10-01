"""Access enforcement and hash-linked trace integrity (spec sections 7, 13, 14)."""
from conftest import form, run


def test_row_policy_denies_out_of_scope_state(svc):
    rid, p, out = run(svc, state="SC")  # analyst_nc is entitled to NC only
    assert p["status"] == "blocked" and "row policy denies state SC" in p["blockers"][0]
    assert out is None


def test_subject_without_read_capability_denied(svc):
    rid, p, _ = run(svc, subject="viewer")
    assert p["status"] == "blocked" and "access denied" in p["blockers"][0]


def test_unknown_purpose_denied(svc):
    d = svc.interpret(form=form(), purpose="marketing")
    svc.confirm(d["request_id"], "t")
    assert "purpose" in svc.prepare(d["request_id"])["blockers"][0]


def test_cache_revoked_after_policy_change(svc):
    run(svc, window_days=12)
    n = svc.cache.invalidate_policy("access-2026.10.0")
    assert n >= 1
    assert all(c["status"] != "valid" for c in svc.cache.list())


def test_trace_chain_verifies_and_detects_tampering(fresh_svc):
    run(fresh_svc)
    chk = fresh_svc.tracer.verify_chain()
    assert chk["ok"] and chk["events"] > 5
    assert fresh_svc.tracer.verify_chain(anchor=chk["head"])["ok"]
    seq = fresh_svc.store.one("SELECT seq FROM trace_events ORDER BY seq LIMIT 1 OFFSET 2")["seq"]
    fresh_svc.store.execute("UPDATE trace_events SET body_json = replace(body_json, 'proposed', 'tampered') WHERE seq >= ?", (seq,))
    bad = fresh_svc.tracer.verify_chain()
    assert not bad["ok"]


def test_receipt_records_versions_cut_access_and_unavailable_telemetry(svc):
    _, _, out = run(svc, window_days=11, prefer="fused")
    r = out["receipt"]
    assert r["versions"]["recipe"] == "quote_conversion@1.0.0"
    assert r["versions"]["source_cut_fingerprint"].startswith("sha256:")
    assert r["versions"]["access"]["policy_version"]
    assert r["cost"]["status"] == "unavailable" and r["cost"]["reason"]
    assert r["telemetry"]["spill"].startswith("unavailable")
    assert any("denominator_conservation" in u for u in r["validations"]["unverified"])  # fused cannot reconcile
    assert r["receipt_hash"].startswith("sha256:")


def test_no_future_knowledge_validation_runs_on_cohort(svc):
    _, _, out = run(svc, window_days=10, prefer="staged")
    assert "canonical_cohort:no_future_knowledge" in out["receipt"]["validations"]["passed"]
