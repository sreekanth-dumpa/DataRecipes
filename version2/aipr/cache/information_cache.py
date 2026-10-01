"""Coverage-aware information-set cache (spec section 13).

Identity is semantic + source cut + access -- never the physical plan, which is
provenance only.  Two reuse levels:

* exact   -- same semantic fingerprint, source-cut fingerprint and access fingerprint
* derived -- same coverage (meaning minus dimensions), same source cut and
             access, requested dimensions a subset of cached dimensions; served
             by summing sufficient statistics and recomputing the ratio
             (never by averaging subgroup rates)

Every read is re-authorised.  Only complete, validated, exact results are admitted.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from ..core.clock import now_iso, utcnow
from ..compiler.ast_validator import validate_sql
from ..core.ids import new_id

STATS = ("eligible", "converted", "immature_excluded", "cohort_quotes")


def rollup(rows: list[dict[str, Any]], dims: list[str]) -> list[dict[str, Any]]:
    acc: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        k = tuple(r[d] for d in dims)
        a = acc.setdefault(k, {**{d: r[d] for d in dims}, **{s: 0 for s in STATS}})
        for s in STATS:
            a[s] += int(r[s])
    return [acc[k] for k in sorted(acc, key=lambda t: tuple(str(x) for x in t))]


def with_rates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "conversion_rate": (r["converted"] / r["eligible"]) if r["eligible"] else None} for r in rows]


class InformationCache:
    def __init__(self, store, engine, ttl_seconds: int):
        self.store, self.engine, self.ttl = store, engine, ttl_seconds

    def publish(self, *, execution: dict[str, Any], intent: dict[str, Any], recipe_key: str, rows: list[dict[str, Any]],
                final_relation: str | None, validations_passed: bool, complete: bool, exactness: str,
                policy_version: str, physical_fingerprint: str) -> dict[str, Any]:
        if not (complete and validations_passed and exactness == "exact"):
            return {"published": False, "reason": "partial, failed, or non-exact results are not admitted to the cache"}
        isid = new_id("is")
        relation_ref = None
        if final_relation:
            relation_ref = f"cache.{isid}"
            cols = ", ".join(list(intent["dimensions"]) + list(STATS))
            stmt = f"CREATE TABLE {relation_ref} AS SELECT {cols} FROM {final_relation}"
            check = validate_sql(stmt, "duckdb" if self.engine.dialect_name == "duckdb" else "snowflake",
                                 {final_relation}, set(), allow_ctas_schemas=("cache",))
            if not check.ok:
                return {"published": False, "reason": "cache publication failed AST validation: " + "; ".join(check.errors)}
            self.engine.execute(stmt, {}, new_id("q"))
        expires = (utcnow() + timedelta(seconds=self.ttl)).isoformat()
        suff = [{**{d: r[d] for d in intent["dimensions"]}, **{s: int(r[s]) for s in STATS}} for r in rows]
        manifest = {
            "information_set_id": isid,
            "semantic_fingerprint": execution["semantic_fingerprint"],
            "coverage_fingerprint": execution["coverage_fingerprint"],
            "source_cut_fingerprint": execution["source_cut_fingerprint"],
            "access_fingerprint": execution["access_fingerprint"],
            "policy_version": policy_version,
            "recipe": recipe_key,
            "coverage": {"state": intent["scope"]["state"], "lob": intent["scope"]["lob"],
                         "cohort": intent["cohort"], "window_days": intent["window"]["value"],
                         "knowledge_cutoff": intent["knowledge_cutoff"], "dimensions": intent["dimensions"],
                         "maturity_policy": intent["maturity_policy"]},
            "grain": intent["dimensions"],
            "metric_state": {"eligible": "count", "converted": "count", "immature_excluded": "count",
                             "cohort_quotes": "count",
                             "conversion_rate": "derived_as_sum_converted_over_sum_eligible"},
            "complete": True, "exactness": exactness, "validation": "passed",
            "producer_run_id": execution["execution_id"], "producer_request_id": execution["request_id"],
            "producer_physical_fingerprint": physical_fingerprint,
            "relation_ref": relation_ref, "created_at": now_iso(), "expires_at": expires,
        }
        self.store.execute("""INSERT INTO information_sets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (isid, manifest["semantic_fingerprint"], manifest["coverage_fingerprint"],
                            manifest["source_cut_fingerprint"], manifest["access_fingerprint"], recipe_key,
                            json.dumps(intent["dimensions"]), json.dumps(manifest), json.dumps(suff), relation_ref,
                            "valid", execution["execution_id"], manifest["created_at"], expires))
        return {"published": True, "information_set_id": isid, "manifest": manifest}

    def _load(self, r: dict[str, Any]) -> dict[str, Any]:
        r["manifest"] = json.loads(r["manifest_json"])
        r["rows"] = json.loads(r["rows_json"])
        r["dims"] = json.loads(r["dimensions"])
        return r

    def lookup(self, *, semantic_fp: str, coverage_fp: str, source_cut_fp: str, access_allowed: bool,
               access_fp: str, recipe_key: str, dims: list[str]) -> dict[str, Any]:
        rejected: list[dict[str, Any]] = []
        now = now_iso()
        cands = [self._load(r) for r in self.store.all(
            "SELECT * FROM information_sets WHERE recipe_key=? ORDER BY created_at DESC", (recipe_key,))]
        best_derived = None
        for c in cands:
            reasons = []
            if c["status"] != "valid":
                reasons.append(f"status {c['status']}")
            if c["expires_at"] < now:
                reasons.append("expired")
            if c["coverage_fingerprint"] != coverage_fp:
                reasons.append("coverage differs (scope, cohort, window, cutoff, recipe or definitions)")
            if c["source_cut_fingerprint"] != source_cut_fp:
                reasons.append("source cut differs (data changed or different snapshot)")
            if c["access_fingerprint"] != access_fp:
                reasons.append("access fingerprint differs (entitlements or policy version)")
            if not access_allowed:
                reasons.append("re-authorisation denied")
            if not set(dims) <= set(c["dims"]):
                reasons.append(f"requested dimensions {sorted(set(dims) - set(c['dims']))} not in cached grain")
            if reasons:
                rejected.append({"information_set_id": c["information_set_id"], "reasons": reasons})
                continue
            if c["semantic_fingerprint"] == semantic_fp:
                return {"kind": "exact", "information_set": c, "rows": c["rows"], "rejected": rejected}
            if best_derived is None or len(c["dims"]) < len(best_derived["dims"]):
                best_derived = c
        if best_derived is not None:
            return {"kind": "derived", "information_set": best_derived, "rows": rollup(best_derived["rows"], dims),
                    "operation": {"type": "rollup_sum_sufficient_statistics", "from_dims": best_derived["dims"],
                                  "to_dims": dims, "rate": "recomputed_from_sums"},
                    "rejected": rejected}
        return {"kind": "miss", "rejected": rejected}

    def invalidate_policy(self, current_policy_version: str) -> int:
        n = 0
        for r in self.store.all("SELECT information_set_id, manifest_json FROM information_sets WHERE status='valid'"):
            if json.loads(r["manifest_json"])["policy_version"] != current_policy_version:
                self.store.execute("UPDATE information_sets SET status='revoked_policy_change' WHERE information_set_id=?",
                                   (r["information_set_id"],))
                n += 1
        return n

    def list(self) -> list[dict[str, Any]]:
        return [self._load(r) for r in self.store.all("SELECT * FROM information_sets ORDER BY created_at DESC")]
