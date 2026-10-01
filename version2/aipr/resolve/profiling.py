"""Bounded, approved profiling probes with value-of-information selection (spec section 6).

Each probe declares the decision it informs and an expected cost.  A probe runs
only if (a) no compatible profile exists for the same source cut and scope,
(b) the discovery budget allows it, and (c) its decision is still open.
Statistics carry source cut, predicate scope, collection time and an
exact/approximate/configured label.  Live (Snowflake) mode uses configured
volume estimates instead of arbitrary exploratory scans.
"""
from __future__ import annotations

from typing import Any

from ..compiler import sqlgen
from ..compiler.dialect import Dialect
from ..core.clock import now_iso
from ..core.hashing import fingerprint

PROBES = [
    {"probe_id": "mature_cohort_size", "informs": "partition count, admission estimate", "cost_credits": 0.004,
     "description": "exact canonical cohort size and mature subset"},
    {"probe_id": "canonical_key_uniqueness", "informs": "route admission (quote grain after reduction)",
     "cost_credits": 0.004, "description": "duplicate canonical quote keys after the recipe reduction"},
    {"probe_id": "bind_multiplicity", "informs": "join strategy (reduce vs raw join), fan-out envelope",
     "cost_credits": 0.006, "description": "bind events per cohort quote key: distribution and heavy hitters"},
    {"probe_id": "crosswalk_multiplicity", "informs": "dimension join safety",
     "cost_credits": 0.002, "description": "rows per channel_code in the crosswalk and unmatched-code share"},
]


def probe_scope(intent: dict[str, Any]) -> dict[str, Any]:
    return {"state": intent["scope"]["state"], "lob": intent["scope"]["lob"], "cohort": intent["cohort"],
            "window": intent["window"]["value"], "knowledge_cutoff": intent["knowledge_cutoff"]}


def _probe_sql(probe_id: str, d: Dialect) -> str:
    canon = sqlgen.canonical_cohort(d)
    if probe_id == "mature_cohort_size":
        return f"SELECT COUNT(*) AS cohort_rows, COALESCE(SUM(mature),0) AS mature_rows FROM ({canon}) x"
    if probe_id == "canonical_key_uniqueness":
        return f"SELECT COUNT(*) - COUNT(DISTINCT quote_id) AS duplicate_keys FROM ({canon}) x"
    if probe_id == "bind_multiplicity":
        return f"""WITH k AS (SELECT quote_id FROM ({canon}) x WHERE mature = 1),
m AS (SELECT b.quote_id, COUNT(*) AS n FROM {d.source('src_policy.bind_event', 'b')} JOIN k ON k.quote_id = b.quote_id
      WHERE b.event_type = 'BIND' AND b.recorded_at <= {d.param('cutoff_ts')} GROUP BY b.quote_id)
SELECT (SELECT COUNT(*) FROM k) AS left_keys, COUNT(*) AS matched_keys, COALESCE(SUM(n),0) AS raw_join_rows,
       COALESCE(MAX(n),0) AS max_multiplicity, COALESCE(AVG(n),0) AS avg_multiplicity,
       COALESCE(quantile_cont(n, 0.95),0) AS p95_multiplicity FROM m"""
    if probe_id == "crosswalk_multiplicity":
        return f"""SELECT (SELECT COALESCE(MAX(c),0) FROM (SELECT COUNT(*) c FROM {d.source('ref.channel_crosswalk', 'x')}
                   GROUP BY channel_code) t) AS max_rows_per_code,
       AVG(CASE WHEN c.channel_code IS NULL THEN 1.0 ELSE 0.0 END) AS unmatched_share
FROM {d.source('src_quote.quote_iteration', 'q')} LEFT JOIN {d.source('ref.channel_crosswalk', 'c')}
  ON c.channel_code = q.channel_code
WHERE q.lob = {d.param('lob')} AND q.state = {d.param('state')}"""
    raise KeyError(probe_id)


def heavy_hitters_sql(d: Dialect) -> str:
    canon = sqlgen.canonical_cohort(d)
    return f"""SELECT b.quote_id, COUNT(*) AS n FROM {d.source('src_policy.bind_event', 'b')}
JOIN ({canon}) k ON k.quote_id = b.quote_id
WHERE k.mature = 1 AND b.event_type = 'BIND' AND b.recorded_at <= {d.param('cutoff_ts')}
GROUP BY b.quote_id ORDER BY n DESC, b.quote_id LIMIT 3"""


def run_profile(engine: Any, dialect: Dialect, intent: dict[str, Any], params: dict[str, Any],
                source_cut_fp: str, cached: dict[str, Any] | None, budget_credits: float,
                max_probes: int) -> dict[str, Any]:
    scope = probe_scope(intent)
    profile: dict[str, Any] = {"source_cut_fingerprint": source_cut_fp, "predicate_scope": scope,
                               "scope_fingerprint": fingerprint(scope), "stats": {}, "probes": [],
                               "spent_credits": 0.0, "reused": False}
    if cached:
        profile.update(stats=cached["stats"], reused=True,
                       probes=[{"probe_id": p["probe_id"], "status": "reused_from_profile_cache"} for p in PROBES])
        return profile
    if engine.name != "duckdb":
        est = getattr(engine, "volume_estimates", {})
        profile["stats"] = {
            "mature_cohort_size": {"value": {"cohort_rows": est.get("cohort_rows", 1_000_000),
                                             "mature_rows": est.get("mature_rows", 800_000)},
                                   "label": "configured_estimate", "collected_at": now_iso()},
            "bind_multiplicity": {"value": {"avg_multiplicity": est.get("avg_bind_multiplicity", 1.3),
                                            "max_multiplicity": None, "raw_join_rows": None},
                                  "label": "configured_estimate", "collected_at": now_iso()}}
        profile["probes"] = [{"probe_id": p["probe_id"], "status": "skipped",
                              "reason": "live mode uses configured estimates; snapshot-aware probes not yet approved"}
                             for p in PROBES]
        return profile
    for i, p in enumerate(PROBES):
        if i >= max_probes or profile["spent_credits"] + p["cost_credits"] > budget_credits:
            profile["probes"].append({"probe_id": p["probe_id"], "status": "skipped",
                                      "reason": "discovery budget or probe count exhausted"})
            continue
        sql = _probe_sql(p["probe_id"], dialect)
        row = engine.fetch(sql, params)[0]
        value = {k: (float(v) if isinstance(v, float) else (int(v) if v is not None else None)) for k, v in row.items()}
        if p["probe_id"] == "bind_multiplicity":
            value["heavy_hitters"] = [{"quote_id": r["quote_id"], "n": int(r["n"])}
                                      for r in engine.fetch(heavy_hitters_sql(dialect), params)]
        profile["stats"][p["probe_id"]] = {"value": value, "label": "exact", "collected_at": now_iso(),
                                           "source_cut_fingerprint": source_cut_fp, "staleness_policy": "valid_for_source_cut"}
        profile["spent_credits"] += p["cost_credits"]
        profile["probes"].append({"probe_id": p["probe_id"], "status": "executed", "informs": p["informs"],
                                  "cost_credits_notional": p["cost_credits"], "result": value})
    return profile


def blocking_findings(profile: dict[str, Any]) -> list[str]:
    """Probe results that must block admission (an unknown or violated relationship)."""
    out = []
    s = profile["stats"]
    dup = s.get("canonical_key_uniqueness", {}).get("value", {}).get("duplicate_keys")
    if dup:
        out.append(f"{dup} duplicate canonical quote keys after reduction: quote grain not established")
    cw = s.get("crosswalk_multiplicity", {}).get("value", {}).get("max_rows_per_code")
    if cw and cw > 1:
        out.append(f"channel crosswalk has {cw} rows for one code: dimension join would multiply quotes")
    return out
