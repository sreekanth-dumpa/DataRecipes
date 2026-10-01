"""Stage validation (spec sections 9 and 10.4).

A producer's relation is admitted only after its declared validations pass.
Blocking failures (grain, temporal, conservation) stop publication; envelope
deviations are *performance* observations handed to the checkpoint policy.
"""
from __future__ import annotations

from typing import Any


def _key(cols: list[str]) -> str:
    return ", ".join(cols) if cols else "1"


def run_validations(engine: Any, node: dict[str, Any], relation: str, rels: dict[str, str],
                    params: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for v in node["validations"]:
        t = v["type"]
        res: dict[str, Any] = {"id": v["id"], "type": t, "blocking": v.get("blocking", True)}
        try:
            if t == "unique_key":
                k = _key(v["key"])
                row = engine.fetch(f"SELECT COUNT(*) AS n, COUNT(*) - (SELECT COUNT(*) FROM (SELECT DISTINCT {k} FROM {relation}) d) "
                                   f"AS dup FROM {relation}")[0]
                res.update(passed=row["dup"] == 0, observed={"rows": row["n"], "duplicate_keys": row["dup"]})
            elif t == "max_le_param":
                row = engine.fetch(f"SELECT MAX({v['column']}) AS m FROM {relation}")[0]
                lim = params[v["param"]]
                res.update(passed=row["m"] is None or row["m"] <= lim, observed={"max": str(row["m"]), "limit": str(lim)})
            elif t == "between_params":
                row = engine.fetch(f"SELECT MIN({v['column']}) AS lo, MAX({v['column']}) AS hi FROM {relation}")[0]
                ok = row["lo"] is None or (row["lo"] >= params[v["low"]] and row["hi"] <= params[v["high"]])
                res.update(passed=ok, observed={"min": str(row["lo"]), "max": str(row["hi"])})
            elif t == "subset_of":
                other = rels[v["of"]]
                row = engine.fetch(f"SELECT COUNT(*) AS n FROM {relation} a WHERE NOT EXISTS "
                                   f"(SELECT 1 FROM {other} b WHERE b.{v['key']} = a.{v['key']})")[0]
                res.update(passed=row["n"] == 0, observed={"orphans": row["n"]})
            elif t == "rowcount_equals":
                a = engine.fetch(f"SELECT COUNT(*) AS n FROM {relation}")[0]["n"]
                b = engine.fetch(f"SELECT COUNT(*) AS n FROM {rels[v['of']]}")[0]["n"]
                res.update(passed=a == b, observed={"rows": a, "expected": b})
            elif t == "numerator_le_denominator":
                row = engine.fetch(f"SELECT COUNT(*) AS n FROM {relation} WHERE converted > eligible")[0]
                res.update(passed=row["n"] == 0, observed={"violating_rows": row["n"]})
            elif t == "non_negative":
                cond = " OR ".join(f"{c} < 0" for c in v["columns"])
                row = engine.fetch(f"SELECT COUNT(*) AS n FROM {relation} WHERE {cond}")[0]
                res.update(passed=row["n"] == 0, observed={"violating_rows": row["n"]})
            elif t == "conservation":
                total = engine.fetch(f"SELECT COALESCE(SUM(cohort_quotes),0) AS n, COALESCE(SUM(eligible + immature_excluded),0) AS m FROM {relation}")[0]
                base = engine.fetch(f"SELECT COUNT(*) AS n FROM {rels[v['against']]}")[0]["n"]
                res.update(passed=int(total["n"]) == base and int(total["m"]) == base,
                           observed={"aggregate_cohort_quotes": int(total["n"]), "eligible_plus_immature": int(total["m"]),
                                     "canonical_cohort_rows": base})
            else:
                res.update(passed=False, observed={"error": f"unknown validation type {t}"})
        except Exception as exc:  # a validation that cannot run is not a pass
            res.update(passed=False, observed={"error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        out.append(res)
    return out


def envelope_check(node: dict[str, Any], rows: int) -> dict[str, Any] | None:
    env = node.get("envelope") or {}
    if env.get("rows_est") is None:
        return None
    lo, hi = env.get("rows_low"), env.get("rows_high")
    within = lo is None or (lo <= rows <= hi)
    return {"rows_est": env["rows_est"], "rows_range": [lo, hi], "rows_observed": rows, "within": within,
            "classification": "performance_observation" if not within else "ok"}
