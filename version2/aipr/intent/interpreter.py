"""Deterministic interpreter: request text or form -> proposed intent.

The deterministic path is the default and the replay path.  An optional,
provider-neutral LLM proposer (``llm.py``) may *propose* fields; its proposal
goes through exactly the same normalisation, recipe hard-match and human
confirmation, and is never trusted to supply authority.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any

from ..recipes.registry import Recipe

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
MONTHS["sept"] = 9
STATES = {"NC", "SC", "VA", "GA", "TN", "CA", "TX", "FL", "NY"}
DIM_WORDS = {"product version": "product_version", "version": "product_version", "channel": "channel",
             "state": "state", "driver age": "driver_age", "household": "household", "agent": "agent_id",
             "territory": "territory"}

# phrases that change meaning relative to the certified defaults
MEANING_CUES = [
    (r"polic(y|ies) issu", "conversion_event", "policy_issuance"),
    (r"latest (product )?version", "version_attribution", "latest_iteration"),
    (r"(net of|excluding|exclude|minus) cancel", "cancelled_binds", "exclude_cancelled"),
    (r"elapsed|\bhours?\b", "day_basis", "elapsed_24h"),
    (r"(every|each|per) (quote )?iteration", "quote_identity", "quote_iteration"),
    (r"censored", "maturity_policy", "censored_cohort_report"),
    (r"(all|every) (of )?(the )?\w+ quotes|include immature", "maturity_policy", "include_immature"),
]
UNSUPPORTED_METRICS = {"retention": "retention_rate", "loss ratio": "loss_ratio", "premium": "written_premium",
                       "claim": "claim_frequency", "renewal": "renewal_rate"}


def _parse_date(text: str, default_year: int) -> date | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return date.fromisoformat(m.group(1))
    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", text)
    if m and m.group(1).lower() in MONTHS:
        return date(int(m.group(3) or default_year), MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


def interpret_text(text: str, recipe: Recipe | None, today: date | None = None) -> tuple[dict[str, Any], list[str]]:
    """Return (intent, assumptions).  Every default applied is recorded as an assumption."""
    today = today or date(2026, 10, 1)
    t = text.strip()
    low = t.lower()
    assumptions: list[str] = []

    metric = "quote_conversion"
    for word, mid in UNSUPPORTED_METRICS.items():
        if word in low:
            metric = mid
    if metric == "quote_conversion" and not re.search(r"conver|bind|quote", low):
        metric = "unknown"

    state = next((s for s in re.findall(r"\b([A-Z]{2})\b", t) if s in STATES), None)
    if state is None:
        state = "NC"
        assumptions.append("state not stated; assumed NC")
    lob = "personal_auto"
    if "home" in low:
        lob = "homeowners"
    elif "auto" not in low:
        assumptions.append("line of business not stated; assumed personal_auto")

    year_m = re.search(r"\b(20\d{2})\b", t)
    year = int(year_m.group(1)) if year_m else today.year
    cohort_start = cohort_end = None
    for name, num in MONTHS.items():
        if re.search(rf"\b(issued|in|during|for|from)\s+{name}\b", low) and len(name) > 2:
            cohort_start = date(year, num, 1)
            cohort_end = date(year, num, calendar.monthrange(year, num)[1])
            break
    if cohort_start is None:
        first = today.replace(day=1)
        prev_end = first - timedelta(days=1)
        cohort_start, cohort_end = prev_end.replace(day=1), prev_end
        assumptions.append(f"cohort not stated; assumed previous calendar month {cohort_start:%B %Y}")

    wm = re.search(r"(\d{1,3})[\s-]*(calendar\s+)?day", low)
    window = int(wm.group(1)) if wm else 30
    if not wm:
        assumptions.append("window not stated; assumed recipe default 30 calendar days")

    cutoff = None
    cm = re.search(r"(known by|as of|as known|cutoff|through)\s+(.+?)(?:[?.,;]|$)", low)
    if cm:
        cutoff = _parse_date(cm.group(2), year)
    if cutoff is None:
        cutoff = today - timedelta(days=1)
        assumptions.append(f"knowledge cutoff not stated; assumed end of {cutoff.isoformat()}")

    dims: list[str] = []
    bm = re.search(r"\bby\s+(.+?)(?:,?\s+(?:as of|as known|known by|for|in|issued|with)\b|[?.;]|$)", low)
    if bm:
        for part in re.split(r",|\band\b|/", bm.group(1)):
            p = part.strip()
            for word, d in DIM_WORDS.items():
                if word in p and d not in dims:
                    dims.append(d)
                    break
    if not dims:
        dims = ["product_version"]
        assumptions.append("no breakdown stated; assumed product_version")

    semantics: dict[str, str] = {}
    for pattern, field_name, value in MEANING_CUES:
        if re.search(pattern, low):
            semantics[field_name] = value
    if recipe is not None:
        for name, opt in recipe.body["options"].items():
            if name not in semantics:
                semantics[name] = opt["supported"][0]
                assumptions.append(f"{name} defaulted to certified meaning '{opt['supported'][0]}'")

    intent = {
        "domains": ["quote", "policy"],
        "metric": metric,
        "population": "first_eligible_issued_personal_auto_quote",
        "scope": {"state": state, "lob": lob},
        "cohort": {"start": cohort_start.isoformat(), "end": cohort_end.isoformat()},
        "window": {"value": window, "unit": "elapsed_24h" if semantics.get("day_basis") == "elapsed_24h" else "calendar_day",
                   "inclusive_end": True},
        "time_basis": "quote_issue_date",
        "knowledge_cutoff": f"{cutoff.isoformat()}T23:59:59Z",
        "maturity_policy": semantics.get("maturity_policy", "exclude_without_complete_window"),
        "dimensions": dims,
        "output_grain": list(dims),
        "output": {"form": "assessment_with_chart", "row_limit": 10000},
        "candidate_recipes": ["quote_conversion"] if metric == "quote_conversion" else [],
        "semantics": semantics,
    }
    return intent, assumptions


def intent_from_form(form: dict[str, Any], recipe: Recipe) -> dict[str, Any]:
    """Structured form path used by the UI and tests."""
    dims = list(form.get("dimensions", ["product_version", "channel"]))
    sem = {name: opt["supported"][0] for name, opt in recipe.body["options"].items()}
    sem.update(form.get("semantics", {}))
    return {
        "domains": ["quote", "policy"],
        "metric": form.get("metric", "quote_conversion"),
        "population": recipe.body["population"],
        "scope": {"state": form.get("state", "NC"), "lob": form.get("lob", "personal_auto")},
        "cohort": {"start": str(form.get("cohort_start", "2026-08-01")), "end": str(form.get("cohort_end", "2026-08-31"))},
        "window": {"value": int(form.get("window_days", 30)), "unit": "calendar_day", "inclusive_end": True},
        "time_basis": "quote_issue_date",
        "knowledge_cutoff": str(form.get("knowledge_cutoff", "2026-09-15T23:59:59Z")),
        "maturity_policy": sem["maturity_policy"],
        "dimensions": dims,
        "output_grain": list(dims),
        "output": {"form": "assessment_with_chart", "row_limit": 10000},
        "candidate_recipes": [recipe.id],
        "semantics": sem,
    }
