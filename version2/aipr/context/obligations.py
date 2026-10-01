"""Semantic obligations derived from a confirmed intent (spec section 5).

Retrieval answers these questions; it does not load all metadata for a domain.
"""
from __future__ import annotations

from typing import Any

OBLIGATION_TEXT = {
    "quote_identity": "define quote identity and the canonical quote iteration",
    "bind_linkage": "resolve linkage from bind events to quotes and the conversion event",
    "cancellation_treatment": "define treatment of binds later cancelled",
    "product_version_attribution": "establish which product version a quote is attributed to",
    "observation_window": "establish the observation window, units and maturity rule",
    "temporal_knowledge_basis": "separate effective time from knowledge (recorded) time",
    "source_authority": "identify the authoritative source products",
    "access_enforcement": "enforce row access and stage-relation protection",
    "aggregation_behavior": "define numerator, denominator and aggregation algebra",
    "dimension_semantics": "define the meaning and code crosswalk of each requested dimension",
}


def derive_obligations(intent: dict[str, Any]) -> list[dict[str, Any]]:
    ids = ["quote_identity", "bind_linkage", "cancellation_treatment", "observation_window",
           "temporal_knowledge_basis", "source_authority", "access_enforcement", "aggregation_behavior"]
    if "product_version" in intent.get("dimensions", []):
        ids.append("product_version_attribution")
    if any(d != "product_version" for d in intent.get("dimensions", [])):
        ids.append("dimension_semantics")
    return [{"obligation_id": o, "text": OBLIGATION_TEXT[o], "mandatory": True} for o in ids]
