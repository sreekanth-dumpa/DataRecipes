"""Recipe builder (spec section 18).

Turns natural-language logic plus answers to obligation-driven questions into
a *versioned draft*.  A draft never becomes certified by being generated: it
records whether its answers match an installed certified algebra, lists the
adversarial cases a business owner must label, and stays in ``draft`` status.
It never runs user-provided SQL.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..core.clock import now_iso
from ..core.hashing import fingerprint
from ..recipes.registry import RecipeRegistry

QUESTIONS = [
    {"id": "entity_identity", "obligation": "quote_identity",
     "question": "What is one unit in the population: a business quote (all iterations together) or each quote iteration?",
     "options": {"business_quote": "business quote", "quote_iteration": "each iteration"}},
    {"id": "numerator_event", "obligation": "bind_linkage",
     "question": "Which event makes a quote 'converted'?",
     "options": {"bind_event": "a bind event", "policy_issuance": "policy issuance"}},
    {"id": "time_window", "obligation": "observation_window",
     "question": "How is the observation window measured, and what happens to quotes whose window is not complete at the cutoff?",
     "options": {"calendar_day+exclude_without_complete_window": "calendar days; exclude immature quotes",
                 "calendar_day+censored_cohort_report": "calendar days; censored report",
                 "elapsed_24h+exclude_without_complete_window": "elapsed 24h periods; exclude immature"}},
    {"id": "version_attribution", "obligation": "product_version_attribution",
     "question": "Which product version is a quote attributed to when iterations differ?",
     "options": {"first_eligible_issued_iteration": "first eligible issued iteration", "latest_iteration": "latest iteration"}},
    {"id": "duplicates_and_cancellations", "obligation": "cancellation_treatment",
     "question": "If a quote has several binds, or a bind is later cancelled, how is it counted?",
     "options": {"count_bind_regardless_of_later_cancellation": "once; cancellation does not negate",
                 "exclude_cancelled": "not converted if cancelled by the cutoff"}},
    {"id": "aggregation", "obligation": "aggregation_behavior",
     "question": "How should groups roll up, and is any approximation permitted?",
     "options": {"sum_counts_exact": "sum numerators and denominators exactly; recompute the rate",
                 "average_rates": "average group rates", "approximate_ok": "approximate sketches acceptable"}},
]

ALGEBRA_MAP = {  # builder answer -> quote_conversion option name/value
    "entity_identity": ("quote_identity", lambda v: v),
    "numerator_event": ("conversion_event", lambda v: v),
    "version_attribution": ("version_attribution", lambda v: v),
    "duplicates_and_cancellations": ("cancelled_binds", lambda v: v),
}

ADVERSARIAL_CASES = [
    "quote with several iterations where the first is a draft", "quote with several binds inside the window",
    "bind one day after the window end", "bind recorded after the knowledge cutoff (late-arriving)",
    "quote issued too late to complete the window (immature)", "bind on the last day of the window (inclusive end)",
    "bind later cancelled", "unmatched channel code", "bind dated before quote issue",
    "iteration recorded after the cutoff", "orphan bind with no quote",
]


def questions() -> list[dict[str, Any]]:
    return QUESTIONS


def suggest_answers(logic: str) -> dict[str, str]:
    """Deterministic first guess from the natural-language logic (the user still answers)."""
    t = logic.lower()
    s: dict[str, str] = {}
    s["entity_identity"] = "quote_iteration" if re.search(r"each iteration|every iteration", t) else "business_quote"
    s["numerator_event"] = "policy_issuance" if "issu" in t and "polic" in t else "bind_event"
    s["time_window"] = "calendar_day+censored_cohort_report" if "censor" in t else "calendar_day+exclude_without_complete_window"
    s["version_attribution"] = "latest_iteration" if "latest" in t else "first_eligible_issued_iteration"
    s["duplicates_and_cancellations"] = "exclude_cancelled" if "cancel" in t and ("exclude" in t or "net" in t) \
        else "count_bind_regardless_of_later_cancellation"
    s["aggregation"] = "average_rates" if "average" in t else "sum_counts_exact"
    return s


def build_draft(name: str, logic: str, answers: dict[str, str], owner: str, registry: RecipeRegistry,
                drafts_dir: Path, scope: dict[str, Any] | None = None) -> dict[str, Any]:
    missing = [q["id"] for q in QUESTIONS if q["id"] not in answers]
    base = registry.latest("quote_conversion")
    unsupported: list[dict[str, str]] = []
    if base is not None:
        for qid, (opt, conv) in ALGEBRA_MAP.items():
            if qid in answers:
                ok, why = base.option_supported(opt, conv(answers[qid]))
                if not ok:
                    unsupported.append({"question": qid, "answer": answers[qid], "why": why or ""})
        if "time_window" in answers:
            basis, maturity = answers["time_window"].split("+")
            for opt, val in (("day_basis", basis), ("maturity_policy", maturity)):
                ok, why = base.option_supported(opt, val)
                if not ok:
                    unsupported.append({"question": "time_window", "answer": val, "why": why or ""})
        if answers.get("aggregation") != "sum_counts_exact":
            unsupported.append({"question": "aggregation", "answer": answers.get("aggregation", ""),
                                "why": "only exact count algebra is certified; averaging subgroup rates is never admitted"})
    rid = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "draft_recipe"
    d = Path(drafts_dir) / rid
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.glob("*.json"))) + 1
    draft = {
        "id": rid, "version": f"0.{n}.0-draft", "status": "draft", "owner": owner, "created_at": now_iso(),
        "natural_language_logic": logic, "scope": scope or {"lob": "personal_auto"},
        "clarifications": [{"question_id": q["id"], "question": q["question"], "obligation": q["obligation"],
                            "answer": answers.get(q["id"])} for q in QUESTIONS],
        "unanswered": missing,
        "algebra_match": {
            "installed_algebra": base.key if base else None,
            "matches": not unsupported and not missing,
            "unsupported_meanings": unsupported,
            "consequence": ("answers match the installed certified algebra; this draft can bind to it after owner review"
                            if not unsupported and not missing else
                            "meaning not supported by any installed algebra; requires a new certified operator algebra"),
        },
        "tests": {"adversarial_cases": [{"case": c, "expected_outcome": None, "label_status": "pending_business_owner"}
                                        for c in ADVERSARIAL_CASES],
                  "note": "generated cases are diagnostics; independently approved expected outcomes are required for certification"},
        "certification": {"status": "not_certified", "required": ["owner review", "independent expected outputs",
                                                                  "source mapping", "holdout regression", "registry publication"]},
        "activation": "never automatic",
    }
    draft["draft_hash"] = fingerprint(draft)
    (d / f"{draft['version']}.json").write_text(json.dumps(draft, indent=2))
    return draft


def list_drafts(drafts_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted(Path(drafts_dir).glob("*/*.json"))]
