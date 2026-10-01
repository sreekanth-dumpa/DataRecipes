"""Persistent plan registry (spec section 10.2).

Reusable definitions (logical plans, physical plan versions) are kept apart
from executions so retries, replay and adaptation never overwrite the original
plan.  Plans are immutable, content-hashed, and also written as artefact files.
A saved plan carries a *semantic certificate* (invalidated by schema, policy or
recipe changes) and a separate *statistical applicability envelope*
(invalidated by volume/skew drift, which only affects the preferred physical
variant).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.clock import now_iso
from ..core.hashing import fingerprint
from ..core.ids import new_id
from .store import ControlStore


class PlanRegistry:
    def __init__(self, store: ControlStore, artifacts_dir: Path):
        self.store = store
        self.dir = Path(artifacts_dir) / "plans"
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- logical -------------------------------------------------------------
    def find_logical(self, template_fp: str) -> dict[str, Any] | None:
        r = self.store.one("SELECT * FROM logical_plans WHERE template_fingerprint=? ORDER BY version DESC LIMIT 1",
                           (template_fp,))
        if r:
            r["body"] = json.loads(r["body_json"])
            r["applicability"] = json.loads(r["applicability_json"] or "{}")
            r["certificate"] = json.loads(r["certificate_json"] or "null")
        return r

    def check_applicability(self, saved: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        a = saved["applicability"]
        semantic = [k for k in ("schema_fingerprint", "policy_version", "recipe_hash", "parameter_schema")
                    if a.get(k) != current.get(k)]
        stat = []
        lo, hi = a.get("stats_envelope", {}).get("mature_rows_range", [None, None])
        m = current.get("stats_envelope", {}).get("mature_rows")
        if lo is not None and m is not None and not (lo <= m <= hi):
            stat.append(f"mature rows {m} outside saved envelope [{lo}, {hi}]")
        return {"semantic_invalidations": semantic, "statistical_drift": stat,
                "reusable_definition": not semantic, "physical_preference_valid": not stat}

    def save_logical(self, ir: dict[str, Any], template_fp: str, applicability: dict[str, Any]) -> tuple[str, int, bool, dict]:
        saved = self.find_logical(template_fp)
        if saved:
            check = self.check_applicability(saved, applicability)
            if check["reusable_definition"]:
                return saved["plan_id"], saved["version"], True, check
            plan_id, version = saved["plan_id"], saved["version"] + 1
        else:
            plan_id, version, check = new_id("plan"), 1, {"semantic_invalidations": [], "statistical_drift": [],
                                                           "reusable_definition": True, "physical_preference_valid": True}
        body_hash = fingerprint(ir)
        self.store.execute("""INSERT INTO logical_plans(plan_id, version, template_fingerprint, recipe_key, dimensions,
                              body_json, body_hash, applicability_json, certificate_json, created_at)
                              VALUES (?,?,?,?,?,?,?,?,?,?)""",
                           (plan_id, version, template_fp, f"{ir['recipe']['id']}@{ir['recipe']['version']}",
                            json.dumps(ir["dimensions"]), json.dumps(ir), body_hash, json.dumps(applicability),
                            json.dumps(None), now_iso()))
        (self.dir / f"{plan_id}.v{version}.logical.json").write_text(json.dumps(ir, indent=2))
        return plan_id, version, False, check

    def logical_body(self, plan_id: str, version: int) -> dict[str, Any]:
        r = self.store.one("SELECT body_json FROM logical_plans WHERE plan_id=? AND version=?", (plan_id, version))
        return json.loads(r["body_json"])

    def set_certificate(self, plan_id: str, version: int, certificate: dict[str, Any]) -> None:
        self.store.execute("UPDATE logical_plans SET certificate_json=? WHERE plan_id=? AND version=?",
                           (json.dumps(certificate), plan_id, version))

    # -- physical ------------------------------------------------------------
    def save_physical(self, plan_id: str, version: int, plan: dict[str, Any], origin: str,
                      parent: str | None = None) -> tuple[str, bool]:
        fp = plan["physical_fingerprint"]
        existing = self.store.one("SELECT physical_id FROM physical_plans WHERE plan_id=? AND logical_version=? "
                                  "AND physical_fingerprint=?", (plan_id, version, fp))
        if existing:
            return existing["physical_id"], True
        n = self.store.one("SELECT COUNT(*) AS n FROM physical_plans WHERE plan_id=? AND logical_version=?",
                           (plan_id, version))["n"]
        pid = new_id("phys")
        ref = self.dir / f"{pid}.physical.json"
        ref.write_text(json.dumps(plan, indent=2))
        self.store.execute("""INSERT INTO physical_plans(physical_id, plan_id, logical_version, physical_version, variant,
                              partitions, engine, physical_fingerprint, body_json, parent_physical_id, origin,
                              artifact_ref, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (pid, plan_id, version, n + 1, plan["variant"], plan["partitions"], plan["engine"], fp,
                            json.dumps(plan), parent, origin, str(ref.relative_to(self.dir.parent.parent)), now_iso()))
        return pid, False

    def list_plans(self) -> list[dict[str, Any]]:
        rows = self.store.all("""SELECT l.plan_id, l.version, l.recipe_key, l.dimensions, l.template_fingerprint,
                                 l.certificate_json, l.created_at,
                                 (SELECT COUNT(*) FROM physical_plans p WHERE p.plan_id=l.plan_id) AS physical_versions,
                                 (SELECT COUNT(*) FROM executions e WHERE e.plan_id=l.plan_id) AS executions
                                 FROM logical_plans l ORDER BY l.created_at DESC""")
        for r in rows:
            r["certificate"] = json.loads(r.pop("certificate_json") or "null")
        return rows

    def list_physical(self, plan_id: str) -> list[dict[str, Any]]:
        return self.store.all("""SELECT physical_id, logical_version, physical_version, variant, partitions, engine,
                                 physical_fingerprint, parent_physical_id, origin, artifact_ref, created_at
                                 FROM physical_plans WHERE plan_id=? ORDER BY created_at""", (plan_id,))
