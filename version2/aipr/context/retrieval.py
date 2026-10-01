"""Obligation-directed retrieval and the evidence manifest (spec section 5).

1. Semantic search ranks candidate fragments (token-overlap similarity is the
   POC stand-in for an embedding index).
2. Hard applicability rules filter them: authority class, scope, validity
   interval vs. the knowledge cutoff.  Similarity never supplies authority.
3. A greedy weighted set cover selects a small fragment set that discharges all
   mandatory obligations at minimum retrieval cost.
4. Non-authoritative fragments that contradict selected claims are recorded as
   contradictions; uncovered obligations become explicit gaps that block
   compilation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.hashing import fingerprint

AUTHORITATIVE = {"certified", "authoritative"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{3,}", s.lower())}


@dataclass
class EvidenceStore:
    fragments: list[dict[str, Any]]
    packets: list[dict[str, Any]]

    @classmethod
    def load(cls, context_dir: Path) -> "EvidenceStore":
        frags = [json.loads(p.read_text()) for p in sorted((Path(context_dir) / "evidence").glob("*.json"))]
        for f in frags:
            f["hash"] = fingerprint({k: v for k, v in f.items() if k != "hash"})
        packets = json.loads((Path(context_dir) / "packets.json").read_text())["families"]
        return cls(frags, packets)


@dataclass
class EvidenceManifest:
    obligations: list[dict[str, Any]]
    entries: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    missing_obligations: list[dict[str, Any]] = field(default_factory=list)
    retrieval_cost: float = 0.0

    @property
    def complete(self) -> bool:
        return not self.missing_obligations

    @property
    def completeness(self) -> float:
        n = len(self.obligations)
        return (n - len(self.missing_obligations)) / n if n else 1.0

    def to_dict(self) -> dict[str, Any]:
        d = {"obligations": self.obligations, "entries": self.entries, "rejected": self.rejected,
             "contradictions": self.contradictions, "missing_obligations": self.missing_obligations,
             "retrieval_cost": self.retrieval_cost, "complete": self.complete,
             "completeness": round(self.completeness, 4)}
        d["manifest_hash"] = fingerprint(d)
        return d


def applicable(frag: dict[str, Any], intent: dict[str, Any]) -> tuple[bool, str | None]:
    scope = frag.get("scope", {})
    if intent["scope"]["lob"] not in scope.get("lob", []):
        return False, "lob outside fragment scope"
    if intent["scope"]["state"] not in scope.get("states", []):
        return False, "state outside fragment scope"
    cutoff = intent["knowledge_cutoff"][:10]
    if frag.get("valid_from") and cutoff < frag["valid_from"]:
        return False, f"claim not valid before {frag['valid_from']} (cutoff {cutoff})"
    if frag.get("valid_to") and cutoff > frag["valid_to"]:
        return False, f"claim expired {frag['valid_to']}"
    return True, None


def retrieve(store: EvidenceStore, intent: dict[str, Any], obligations: list[dict[str, Any]],
             budget: float = 10.0) -> EvidenceManifest:
    manifest = EvidenceManifest(obligations=obligations)
    needed = {o["obligation_id"] for o in obligations if o["mandatory"]}
    query = _tokens(" ".join(o["text"] for o in obligations) + " " + intent.get("metric", "").replace("_", " ")
                    + " " + " ".join(intent.get("dimensions", [])))
    ranked = []
    for f in store.fragments:
        sim = len(query & _tokens(f["claim"])) / max(1, len(query | _tokens(f["claim"])))
        ranked.append((sim, f))
    ranked.sort(key=lambda x: -x[0])

    eligible: list[tuple[float, dict[str, Any]]] = []
    for sim, f in ranked:
        ok, why = applicable(f, intent)
        if not ok:
            manifest.rejected.append({"claim_id": f["claim_id"], "similarity": round(sim, 3), "reason": why})
        elif f["authority"] not in AUTHORITATIVE:
            manifest.rejected.append({"claim_id": f["claim_id"], "similarity": round(sim, 3),
                                      "reason": f"authority '{f['authority']}' cannot discharge a mandatory obligation"})
            for ob, conflict in f.get("conflicts_with", {}).items():
                manifest.contradictions.append({"claim_id": f["claim_id"], "obligation_id": ob, "conflict": conflict,
                                                "resolution": "certified claim prevails; non-authoritative claim ignored"})
        else:
            eligible.append((sim, f))

    covered: set[str] = set()
    spent = 0.0
    while needed - covered:
        best, best_score = None, 0.0
        for sim, f in eligible:
            gain = len((set(f["discharges"]) & needed) - covered)
            if gain == 0:
                continue
            score = gain / f.get("retrieval_cost", 1.0) + 0.01 * sim
            if score > best_score:
                best, best_score = (sim, f), score
        if best is None or spent + best[1].get("retrieval_cost", 1.0) > budget:
            break
        sim, f = best
        newly = sorted((set(f["discharges"]) & needed) - covered)
        covered |= set(newly)
        spent += f.get("retrieval_cost", 1.0)
        manifest.entries.append({
            "claim_id": f["claim_id"], "obligation_ids": newly, "claim": f["claim"],
            "source_locator": f["source_locator"], "source_hash": f["hash"], "version": f["version"],
            "authority_class": f["authority"], "packets": f["packets"], "tier": f.get("tier"),
            "scope": f["scope"], "knowledge_ts": f["knowledge_ts"],
            "applicable_business_time": {"from": f.get("valid_from"), "to": f.get("valid_to")},
            "retrieval_reason": f"discharges {', '.join(newly)} (similarity {sim:.2f})",
            "contradiction_status": "contested_by_noncertified" if any(
                c["obligation_id"] in newly for c in manifest.contradictions) else "none",
        })
        eligible = [(s, x) for s, x in eligible if x is not f]
    manifest.retrieval_cost = spent
    for o in sorted(needed - covered):
        manifest.missing_obligations.append({
            "obligation_id": o,
            "gap": f"No certified, applicable claim resolves '{o}' for {intent['scope']['lob']} "
                   f"{intent['scope']['state']} as of {intent['knowledge_cutoff'][:10]}"})
    return manifest
