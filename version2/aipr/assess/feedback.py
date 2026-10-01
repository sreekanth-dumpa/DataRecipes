"""Feedback context lifecycle (spec section 17).

Cases live in their own folders (assessment.md, case.json, trace_refs.json,
expected_outputs.json, proposed_patch.json, promotion.json), separate from the
main specifications.  States: proposed -> evaluated -> approved -> active, or
superseded / rejected.  Approval makes a recommendation *advisory context*
for matching later requests; it never modifies SQL, prompts or certified
semantics.  Activation of executable changes is refused in the POC.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.clock import now_iso

TRANSITIONS = {"proposed": {"evaluated", "approved", "rejected"}, "evaluated": {"approved", "rejected"},
               "approved": {"active", "superseded", "rejected"}, "active": {"superseded"},
               "superseded": set(), "rejected": set()}
EXECUTABLE_CLASSES = {"semantic_specification_change", "operator_change", "physical_plan_change", "prompt_change"}


class FeedbackStore:
    def __init__(self, store, root: Path):
        self.store = store
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, case_id: str) -> Path:
        return self.root / case_id

    def save_case(self, case: dict[str, Any], markdown: str, trace_refs: dict[str, Any],
                  expected_outputs: Any, patch: dict[str, Any]) -> str:
        d = self.path(case["case_id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "case.json").write_text(json.dumps(case, indent=2, default=str))
        (d / "assessment.md").write_text(markdown)
        (d / "trace_refs.json").write_text(json.dumps(trace_refs, indent=2, default=str))
        (d / "expected_outputs.json").write_text(json.dumps(expected_outputs, indent=2, default=str))
        (d / "proposed_patch.json").write_text(json.dumps(patch, indent=2, default=str))
        (d / "promotion.json").write_text(json.dumps({"status": case["status"], "history": [
            {"status": case["status"], "at": now_iso(), "actor": "assessment_service"}]}, indent=2))
        self.store.execute("""INSERT OR REPLACE INTO feedback_cases VALUES (?,?,?,?,?,?,?,?,?)""",
                           (case["case_id"], case["execution_id"], case["recipe_key"], json.dumps(case["scope"]),
                            case["status"], case["change_class"], str(d), now_iso(), now_iso()))
        return str(d)

    def load(self, case_id: str) -> dict[str, Any]:
        return json.loads((self.path(case_id) / "case.json").read_text())

    def transition(self, case_id: str, to: str, actor: str, note: str = "") -> dict[str, Any]:
        case = self.load(case_id)
        cur = case["status"]
        if to not in TRANSITIONS[cur]:
            raise ValueError(f"cannot move case from {cur} to {to}")
        if to == "active" and case["change_class"] in EXECUTABLE_CLASSES:
            raise PermissionError("activating executable logic requires independent cases, holdout regression, "
                                  "review and a versioned release; the POC only admits advisory context")
        case["status"] = to
        case.setdefault("history", []).append({"status": to, "actor": actor, "at": now_iso(), "note": note})
        (self.path(case_id) / "case.json").write_text(json.dumps(case, indent=2, default=str))
        prom = json.loads((self.path(case_id) / "promotion.json").read_text())
        prom["status"] = to
        prom["history"].append({"status": to, "at": now_iso(), "actor": actor, "note": note})
        (self.path(case_id) / "promotion.json").write_text(json.dumps(prom, indent=2))
        self.store.execute("UPDATE feedback_cases SET status=?, updated_at=? WHERE case_id=?", (to, now_iso(), case_id))
        return case

    def applicable_advice(self, recipe_key: str, scope: dict[str, Any]) -> list[dict[str, Any]]:
        """Partitioned retrieval: same recipe, matching scope, approved/active only."""
        out = []
        for r in self.store.all("SELECT case_id, scope_json FROM feedback_cases WHERE recipe_key=? AND status IN ('approved','active')",
                                (recipe_key,)):
            s = json.loads(r["scope_json"])
            if s.get("lob") == scope.get("lob") and s.get("state") in (scope.get("state"), "*"):
                case = self.load(r["case_id"])
                out.append({"case_id": r["case_id"], "status": case["status"], "change_class": case["change_class"],
                            "advice": [x["recommendation"] for x in case["recommendations"][:3]],
                            "applies_to": s, "binding": "advisory_only"})
        return out

    def list(self) -> list[dict[str, Any]]:
        return self.store.all("SELECT * FROM feedback_cases ORDER BY created_at DESC")
