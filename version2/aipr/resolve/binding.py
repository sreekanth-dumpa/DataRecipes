"""Physical resolution: confirmed meaning -> typed entities, keys, joins (spec section 6)."""
from __future__ import annotations

from typing import Any

from ..core.hashing import fingerprint
from ..recipes.registry import Recipe


def binding_contract(intent: dict[str, Any], recipe: Recipe, access: dict[str, Any],
                     source_cut: dict[str, Any], schema_fp: str) -> dict[str, Any]:
    auth = recipe.body["input_authorities"]
    contract = {
        "recipe": recipe.key,
        "entities": {
            "quote": {"relation": auth["quote_iterations"]["relation"], "key": ["quote_id"],
                      "source_grain": auth["quote_iterations"]["grain"], "reduction": "canonical_event_selection"},
            "bind_event": {"relation": auth["bind_events"]["relation"], "key": ["bind_event_id"],
                           "link": {"to": "quote", "keys": ["quote_id"], "relationship": "one_to_many"}},
            "channel": {"relation": auth["channel_crosswalk"]["relation"], "key": ["channel_code"],
                        "link": {"from": "quote", "keys": ["channel_code"], "relationship": "many_to_one",
                                 "unmatched": "unknown"}},
        },
        "attributes": {
            "issue_date": "src_quote.quote_iteration.issue_date (effective time)",
            "known_at": "src_quote.quote_iteration.recorded_at (knowledge time)",
            "bind_date": "src_policy.bind_event.bind_date (effective time)",
            "bind_known_at": "src_policy.bind_event.recorded_at (knowledge time)",
            "product_version": "canonical iteration product_version",
            "channel": "ref.channel_crosswalk.channel via channel_code",
            "state": "src_quote.quote_iteration.state",
        },
        "filters": {"lob": intent["scope"]["lob"], "state": intent["scope"]["state"], "status": "ISSUED",
                    "eligible": True},
        "row_policy": {"applied": "state predicate bound from an entitled scope", "row_filter": access["row_filter"]},
        "source_cut_fingerprint": fingerprint(source_cut),
        "schema_fingerprint": schema_fp,
    }
    contract["binding_hash"] = fingerprint(contract)
    return contract
