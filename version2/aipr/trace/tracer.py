"""Hash-linked trace (spec section 14).

Every phase writes a span with a common envelope.  Each event's hash covers
the previous hash and the canonical body, so modification is *detectable*
against a trusted anchor.  This is not tamper-proof storage: a writer of this
database can recompute hashes.  Production uses protected append-only
storage with signatures or external anchors.  No hidden chain-of-thought is
recorded -- only decisions, evidence references and measurements.
"""
from __future__ import annotations

import json
from typing import Any

from ..control.store import ControlStore
from ..core.clock import now_iso
from ..core.hashing import canonical_json, sha256_hex
from ..core.ids import span_id as new_span_id

GENESIS = "0" * 64


class Tracer:
    def __init__(self, store: ControlStore):
        self.store = store

    def emit(self, trace_id: str, stage: str, status: str, body: dict[str, Any], request_id: str | None = None,
             execution_id: str | None = None, parent_span_id: str | None = None, span_id: str | None = None) -> str:
        sid = span_id or new_span_id()
        at = now_iso()
        envelope = {"trace_id": trace_id, "span_id": sid, "parent_span_id": parent_span_id, "request_id": request_id,
                    "execution_id": execution_id, "stage": stage, "status": status, "at": at, **body}
        with self.store.transaction() as s:
            prev = s.db.execute("SELECT hash FROM trace_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev["hash"] if prev else GENESIS
            payload = canonical_json(envelope)
            h = sha256_hex(prev_hash + payload)
            s.db.execute("""INSERT INTO trace_events(trace_id, span_id, parent_span_id, request_id, execution_id, stage,
                            status, body_json, prev_hash, hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         (trace_id, sid, parent_span_id, request_id, execution_id, stage, status, payload,
                          prev_hash, h, at))
        return sid

    def events(self, request_id: str | None = None, execution_id: str | None = None) -> list[dict[str, Any]]:
        if execution_id:
            rows = self.store.all("SELECT * FROM trace_events WHERE execution_id=? ORDER BY seq", (execution_id,))
        elif request_id:
            rows = self.store.all("SELECT * FROM trace_events WHERE request_id=? ORDER BY seq", (request_id,))
        else:
            rows = self.store.all("SELECT * FROM trace_events ORDER BY seq")
        for r in rows:
            r["body"] = json.loads(r["body_json"])
        return rows

    def verify_chain(self, anchor: str | None = None) -> dict[str, Any]:
        prev = GENESIS
        n = 0
        seen: set[str] = set()
        for r in self.store.all("SELECT seq, body_json, prev_hash, hash FROM trace_events ORDER BY seq"):
            if r["prev_hash"] != prev:
                return {"ok": False, "broken_at_seq": r["seq"], "reason": "prev_hash mismatch"}
            if sha256_hex(prev + r["body_json"]) != r["hash"]:
                return {"ok": False, "broken_at_seq": r["seq"], "reason": "content hash mismatch"}
            prev = r["hash"]
            seen.add(prev)
            n += 1
        out = {"ok": True, "events": n, "head": prev}
        if anchor is not None:
            # the anchor is a previously exported head; it must still be on the chain
            out["anchor_matches"] = anchor in seen
            out["ok"] = out["ok"] and out["anchor_matches"]
        return out
