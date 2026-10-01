"""Effective access decisions and access fingerprints (spec sections 7 and 13).

The access fingerprint hashes the *entitlements and policy version*, not a
role name: two subjects with identical entitlements share a fingerprint;
any policy change produces a new one and therefore invalidates cache reuse.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.hashing import fingerprint


@dataclass
class AccessDecision:
    allowed: bool
    subject: str
    reason: str
    row_filter: dict[str, list[str]]
    capabilities: list[str]
    fingerprint: str
    policy_version: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class AccessPolicy:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.reload()

    def reload(self) -> None:
        self.doc = json.loads(self.path.read_text())

    @property
    def version(self) -> str:
        return self.doc["policy_version"]

    def decide(self, subject: str, purpose: str, scope: dict[str, Any]) -> AccessDecision:
        s = self.doc["subjects"].get(subject)
        if s is None:
            return AccessDecision(False, subject, "unknown subject", {}, [], "sha256:none", self.version)
        fp = fingerprint({"tenant": self.doc["tenant"], "purposes": sorted(s["purposes"]),
                          "row_filter": {k: sorted(v) for k, v in s["row_filter"].items()},
                          "capabilities": sorted(s["capabilities"]), "policy_version": self.version,
                          "purpose": purpose})
        if purpose not in s["purposes"]:
            return AccessDecision(False, subject, f"purpose '{purpose}' not entitled", s["row_filter"],
                                  s["capabilities"], fp, self.version)
        if "read_source" not in s["capabilities"]:
            return AccessDecision(False, subject, "subject lacks read_source capability", s["row_filter"],
                                  s["capabilities"], fp, self.version)
        state = scope.get("state")
        if state not in s["row_filter"].get("state", []):
            return AccessDecision(False, subject, f"row policy denies state {state}", s["row_filter"],
                                  s["capabilities"], fp, self.version)
        return AccessDecision(True, subject, "entitled", s["row_filter"], s["capabilities"], fp, self.version)
