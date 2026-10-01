"""Material clarification questions and confirmation (spec sections 2 and 4).

A question is *material* when different answers can change the number.  The
maturity question is computed, not templated: when cohort_end + window is
after the cutoff, part of the cohort cannot have a complete window, and
excluding it changes the denominator.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from ..core.clock import now_iso
from ..recipes.registry import Recipe
from .model import intent_hash

QUESTION_TEXT = {
    "conversion_event": "What counts as conversion: a bind event, or policy issuance?",
    "day_basis": "Is the window measured in calendar days or elapsed 24-hour periods?",
    "version_attribution": "Which product version is a quote attributed to: its first eligible issued iteration, or its latest?",
    "cancelled_binds": "How are binds that were later cancelled treated?",
    "quote_identity": "Is the unit a business quote, or each quote iteration?",
    "maturity_policy": "How should quotes without a complete observation window be handled?",
}


def maturity_analysis(intent: dict[str, Any]) -> dict[str, Any]:
    start = date.fromisoformat(intent["cohort"]["start"])
    end = date.fromisoformat(intent["cohort"]["end"])
    window = int(intent["window"]["value"])
    cutoff = date.fromisoformat(intent["knowledge_cutoff"][:10])
    last_mature_issue = cutoff - timedelta(days=window)
    total_days = (end - start).days + 1
    immature_days = max(0, (end - max(start - timedelta(days=1), last_mature_issue)).days)
    immature_days = min(immature_days, total_days)
    return {
        "last_mature_issue_date": last_mature_issue.isoformat(),
        "cohort_days": total_days,
        "immature_cohort_days": immature_days,
        "fully_mature": immature_days == 0,
        "cutoff_needed_for_full_cohort": (end + timedelta(days=window)).isoformat(),
    }


def material_questions(intent: dict[str, Any], recipe: Recipe | None) -> list[dict[str, Any]]:
    qs: list[dict[str, Any]] = []
    if recipe is not None:
        for name, opt in recipe.body["options"].items():
            options = [{"value": v, "supported": True} for v in opt["supported"]]
            options += [{"value": v, "supported": False, "why": why} for v, why in opt.get("rejected", {}).items()]
            qs.append({"id": name, "question": QUESTION_TEXT.get(name, name), "current": intent["semantics"].get(name),
                       "options": options, "kind": "definition"})
    m = maturity_analysis(intent)
    if not m["fully_mature"]:
        qs.append({
            "id": "maturity_tradeoff", "kind": "maturity", "material": True,
            "question": (f"{m['immature_cohort_days']} of {m['cohort_days']} cohort days were issued after "
                         f"{m['last_mature_issue_date']} and cannot have a complete {intent['window']['value']}-day window "
                         f"by the cutoff. Excluding them changes the denominator."),
            "options": [
                {"value": "exclude_without_complete_window", "supported": True,
                 "effect": "answer covers mature quotes only; immature quotes are counted and disclosed"},
                {"value": f"move_cutoff_to:{m['cutoff_needed_for_full_cohort']}", "supported": True,
                 "effect": "a later cutoff makes the full cohort mature (new meaning, new contract)"},
                {"value": "censored_cohort_report", "supported": False,
                 "why": "a censored cohort report is a different approved metric that is not installed"},
            ],
            "analysis": m,
        })
    return qs


def confirm(record: dict[str, Any], actor: str, answers: dict[str, str] | None = None) -> dict[str, Any]:
    """Bind a confirmation to the current intent hash.  Answers are applied first."""
    answers = answers or {}
    for qid, value in answers.items():
        record["clarifications"].append({"question_id": qid, "answer": value, "actor": actor, "at": now_iso()})
        if qid == "maturity_tradeoff":
            if value.startswith("move_cutoff_to:"):
                record["intent"]["knowledge_cutoff"] = value.split(":", 1)[1] + "T23:59:59Z"
            else:
                record["intent"]["maturity_policy"] = value
                record["intent"]["semantics"]["maturity_policy"] = value
        else:
            record["intent"].setdefault("semantics", {})[qid] = value
            if qid == "maturity_policy":
                record["intent"]["maturity_policy"] = value
    record["confirmation"] = {"status": "confirmed", "approved_intent_hash": intent_hash(record),
                              "actor": actor, "at": now_iso()}
    return record
