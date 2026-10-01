"""Intent record (spec section 4) and the hashes derived from it.

* ``intent_hash``        -- covers the whole confirmed interpretation; any edit
                            invalidates a confirmation bound to the old hash.
* ``semantic_fingerprint`` -- meaning + bound parameters only (recipe binding is
                            added once resolved).  Equal fingerprints must yield
                            equivalent information across admitted physical plans.
* ``coverage_fingerprint`` -- semantic fingerprint without dimensions; used for
                            derived (roll-up) cache reuse.
"""
from __future__ import annotations

import copy
from datetime import date, datetime
from typing import Any

from ..core.clock import now_iso
from ..core.hashing import fingerprint
from ..core.ids import new_id

MEANING_FIELDS = ("domains", "metric", "population", "scope", "cohort", "window", "time_basis",
                  "knowledge_cutoff", "maturity_policy", "dimensions", "output_grain", "semantics")

DEFAULT_CONSTRAINTS = {"discovery_budget_credits": 0.05, "discovery_max_probes": 4,
                       "execution_budget_credits": 2.0, "max_runtime_seconds": 900,
                       "max_plan_revisions": 3, "queue_allowed": True}


def new_record(raw_text: str, requester: dict[str, Any], intent: dict[str, Any],
               conversation_id: str | None = None, parent_request_id: str | None = None,
               constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    rid = new_id("req")
    return {
        "request_id": rid,
        "conversation_id": conversation_id or new_id("conv"),
        "parent_request_id": parent_request_id,
        "raw_request": raw_text,
        "raw_request_ref": f"artifacts/requests/{rid}.txt",
        "requester": requester,
        "intent": intent,
        "constraints": {**DEFAULT_CONSTRAINTS, **(constraints or {})},
        "assumptions": [],
        "clarifications": [],
        "confirmation": {"status": "pending", "approved_intent_hash": None, "actor": None, "at": None},
        "agent_invocations": [],
        "version": 1,
        "created_at": now_iso(),
    }


def intent_hash(record: dict[str, Any]) -> str:
    return fingerprint(record["intent"])


def semantic_view(intent: dict[str, Any], recipe_key: str | None = None,
                  drop: tuple[str, ...] = ()) -> dict[str, Any]:
    view = {k: intent.get(k) for k in MEANING_FIELDS if k not in drop}
    view["recipe"] = recipe_key
    return view


def semantic_fingerprint(intent: dict[str, Any], recipe_key: str | None) -> str:
    return fingerprint(semantic_view(intent, recipe_key))


def coverage_fingerprint(intent: dict[str, Any], recipe_key: str | None) -> str:
    return fingerprint(semantic_view(intent, recipe_key, drop=("dimensions", "output_grain")))


def apply_edit(record: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Edit an intent field (dot path).  Any edit invalidates confirmation."""
    rec = copy.deepcopy(record)
    node = rec["intent"]
    parts = path.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    if path == "dimensions":
        rec["intent"]["output_grain"] = list(value)
    rec["version"] += 1
    if rec["confirmation"]["status"] == "confirmed":
        rec["confirmation"] = {"status": "invalidated", "approved_intent_hash": None,
                               "actor": None, "at": now_iso(), "reason": f"edited {path}"}
    return rec


def is_confirmed(record: dict[str, Any]) -> bool:
    c = record.get("confirmation", {})
    return c.get("status") == "confirmed" and c.get("approved_intent_hash") == intent_hash(record)


def bound_parameters(intent: dict[str, Any]) -> dict[str, Any]:
    """Connector-bind values (never interpolated into SQL text)."""
    cutoff = datetime.fromisoformat(intent["knowledge_cutoff"].replace("Z", "+00:00")).replace(tzinfo=None)
    return {
        "state": intent["scope"]["state"],
        "lob": intent["scope"]["lob"],
        "cohort_start": date.fromisoformat(intent["cohort"]["start"]),
        "cohort_end": date.fromisoformat(intent["cohort"]["end"]),
        "window_days": int(intent["window"]["value"]),
        "cutoff_ts": cutoff,
        "cutoff_date": cutoff.date(),
    }
