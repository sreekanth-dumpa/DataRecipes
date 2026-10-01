"""Provider-neutral agent interface for intent proposals (spec section 3).

The correctness and reuse contracts never depend on a model provider: a
proposer only returns *candidate* fields, which are then normalised by the
deterministic interpreter, hard-matched against certified recipes and shown to
the user for confirmation.  Every invocation yields a receipt with prompt,
context and response hashes plus provider/model identity (section 7), so an
accepted intent can be replayed without asking the model to reinterpret.

Adapters: ``NullProposer`` (default) and ``AnthropicProposer`` (optional,
requires the ``anthropic`` package and credentials).  A Cortex or other
approved provider is added by implementing ``IntentProposer``.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Protocol

from ..core.hashing import canonical_json, sha256_hex

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "metric": {"type": "string"},
        "state": {"type": "string"},
        "lob": {"type": "string"},
        "cohort_start": {"type": "string", "description": "YYYY-MM-DD"},
        "cohort_end": {"type": "string", "description": "YYYY-MM-DD"},
        "window_days": {"type": "integer"},
        "knowledge_cutoff": {"type": "string", "description": "YYYY-MM-DD"},
        "dimensions": {"type": "array", "items": {"type": "string"}},
        "ambiguities": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["metric", "state", "lob", "cohort_start", "cohort_end", "window_days",
                 "knowledge_cutoff", "dimensions", "ambiguities"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You translate an insurance analytics question into candidate request fields. "
    "Only extract what the user said; leave ambiguities in the 'ambiguities' list rather than guessing "
    "a business definition. Dimensions must be snake_case column-like names. "
    "You do not decide metric definitions; a certified recipe registry does."
)


class IntentProposer(Protocol):
    name: str

    def propose(self, text: str, context: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        ...


class NullProposer:
    name = "none"

    def propose(self, text: str, context: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        return None, {"provider": "none", "status": "not_invoked",
                      "reason": "no LLM provider configured; deterministic interpreter used"}


class AnthropicProposer:
    name = "anthropic"

    def __init__(self, model: str | None = None):
        import anthropic  # optional dependency

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("AIPR_LLM_MODEL", "claude-opus-5-5")

    def propose(self, text: str, context: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        ctx = canonical_json(context)
        receipt: dict[str, Any] = {"provider": "anthropic", "model": self.model,
                                   "prompt_hash": "sha256:" + sha256_hex(SYSTEM_PROMPT + text),
                                   "context_hash": "sha256:" + sha256_hex(ctx)}
        t0 = time.perf_counter()
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA}},
                messages=[{"role": "user", "content": f"Context: {ctx}\n\nQuestion: {text}"}],
            )
        except Exception as exc:  # proposal failure falls back to the deterministic path
            receipt.update(status="failed", error=type(exc).__name__)
            return None, receipt
        receipt["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        receipt["served_model"] = getattr(response, "model", self.model)
        if response.stop_reason == "refusal":
            receipt.update(status="refused")
            return None, receipt
        body = next((b.text for b in response.content if b.type == "text"), "")
        receipt["response_hash"] = "sha256:" + sha256_hex(body)
        try:
            proposal = json.loads(body)
        except json.JSONDecodeError:
            receipt.update(status="invalid_json")
            return None, receipt
        receipt["status"] = "proposed"
        return proposal, receipt


def get_proposer(provider: str) -> IntentProposer:
    if provider == "anthropic":
        try:
            return AnthropicProposer()
        except Exception:
            return NullProposer()
    return NullProposer()


def merge_proposal(intent: dict[str, Any], proposal: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Overlay proposed *parameters* onto a deterministic intent.  Definitions are never taken from the model."""
    notes: list[str] = []
    if not proposal:
        return intent, notes
    intent = json.loads(json.dumps(intent))
    intent["scope"]["state"] = proposal.get("state") or intent["scope"]["state"]
    intent["scope"]["lob"] = proposal.get("lob") or intent["scope"]["lob"]
    intent["cohort"] = {"start": proposal.get("cohort_start") or intent["cohort"]["start"],
                        "end": proposal.get("cohort_end") or intent["cohort"]["end"]}
    if proposal.get("window_days"):
        intent["window"]["value"] = int(proposal["window_days"])
    if proposal.get("knowledge_cutoff"):
        intent["knowledge_cutoff"] = proposal["knowledge_cutoff"][:10] + "T23:59:59Z"
    if proposal.get("dimensions"):
        intent["dimensions"] = list(proposal["dimensions"])
        intent["output_grain"] = list(proposal["dimensions"])
    for a in proposal.get("ambiguities", []):
        notes.append(f"model-flagged ambiguity: {a}")
    return intent, notes
