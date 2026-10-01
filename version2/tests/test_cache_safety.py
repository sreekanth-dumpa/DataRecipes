"""Coverage-aware cache: exact reuse, derived roll-up, and zero unsafe hits (spec section 13)."""
import duckdb

from conftest import key, make_service, run


def test_fresh_then_exact_hit_with_producer_link(svc):
    _, p1, out1 = run(svc, window_days=28)
    assert out1["result"]["origin"]["kind"] == "fresh_execution"
    rid2, p2, out2 = run(svc, window_days=28)
    assert p2["status"] == "cache_hit" and p2["cache"]["kind"] == "exact"
    o = out2["result"]["origin"]
    assert o["kind"] == "application_cache_exact" and o["producer_execution_id"] == out1["execution_id"]
    assert key(out1["result"]["rows"], ["product_version", "channel"]) == key(out2["result"]["rows"], ["product_version", "channel"])
    assert out2["receipt"]["reused_artifacts"]["result_origin"]["producer_receipt"].endswith(f"{out1['execution_id']}.json")


def test_derived_rollup_sums_counts_and_recomputes_rate(svc):
    _, _, fine = run(svc, window_days=27)
    _, p, coarse = run(svc, window_days=27, dimensions=["product_version"])
    assert p["cache"]["kind"] == "derived"
    assert coarse["result"]["origin"]["operation"]["type"] == "rollup_sum_sufficient_statistics"
    by_pv = {}
    for r in fine["result"]["rows"]:
        a = by_pv.setdefault(r["product_version"], [0, 0])
        a[0] += r["eligible"]
        a[1] += r["converted"]
    for r in coarse["result"]["rows"]:
        e, c = by_pv[r["product_version"]]
        assert (r["eligible"], r["converted"]) == (e, c)
        assert abs(r["conversion_rate"] - c / e) < 1e-12  # never an average of subgroup rates


def test_rate_rollup_never_averages_subgroups():
    from aipr.cache.information_cache import rollup, with_rates
    rows = [{"g": "x", "eligible": 90, "converted": 9, "immature_excluded": 0, "cohort_quotes": 90},
            {"g": "x", "eligible": 10, "converted": 9, "immature_excluded": 0, "cohort_quotes": 10}]
    assert with_rates(rollup(rows, ["g"]))[0]["conversion_rate"] == 0.18


def test_incompatible_requests_never_hit(svc):
    run(svc, window_days=26)
    cases = {
        "later cutoff": dict(window_days=26, knowledge_cutoff="2026-09-20T23:59:59Z"),
        "finer cohort": dict(window_days=26, cohort_start="2026-08-05"),
        "other state": dict(window_days=26, state="SC"),
        "new dimension": dict(window_days=26, dimensions=["product_version", "channel", "state"]),
    }
    for name, kw in cases.items():
        subject = "analyst_all" if kw.get("state") == "SC" else "analyst_nc"
        _, p, out = run(svc, subject=subject, **kw)
        assert p["status"] == "ready", (name, p.get("cache"))
        assert out["result"]["origin"]["kind"] == "fresh_execution", name


def test_access_fingerprint_not_role_name(svc):
    run(svc, window_days=25)
    _, p_same, _ = run(svc, subject="analyst_nc_2", window_days=25)   # identical entitlements
    assert p_same["status"] == "cache_hit"
    _, p_other, out = run(svc, subject="analyst_all", window_days=25)  # broader entitlements -> different identity
    assert p_other["status"] == "ready"
    assert any("access fingerprint differs" in r for x in p_other["cache"]["rejected"] for r in x["reasons"])


def test_source_change_rejects_old_cache(tmp_path):
    from aipr.engines.duckdb_engine import DuckDBEngine
    from aipr.service import AIPRService

    s = make_service(tmp_path)
    _, _, first = run(s)
    assert first["result"]["origin"]["kind"] == "fresh_execution"
    s.close()
    con = duckdb.connect(str(s.settings.warehouse_path))  # a late-arriving correction lands in the source
    con.execute("INSERT INTO src_policy.bind_event VALUES ('LATE1', 'Q000001', 'BIND', DATE '2026-08-20', "
                "TIMESTAMP '2026-09-01 10:00:00')")
    con.close()
    s2 = AIPRService(s.settings, engine=DuckDBEngine(s.settings.warehouse_path))
    try:
        _, p, out = run(s2)
        assert p["status"] == "ready"
        assert any("source cut differs" in r for x in p["cache"]["rejected"] for r in x["reasons"])
        assert out["result"]["origin"]["kind"] == "fresh_execution"
        # the historical receipt is not rewritten
        assert s2.result(first["execution_id"])["receipt"]["receipt_hash"] == first["receipt"]["receipt_hash"]
    finally:
        s2.close()


def test_partial_or_failed_results_are_not_cached(svc):
    pub = svc.cache.publish(execution={"execution_id": "x"}, intent={}, recipe_key="k", rows=[], final_relation=None,
                            validations_passed=False, complete=True, exactness="exact", policy_version="v",
                            physical_fingerprint="p")
    assert pub["published"] is False
