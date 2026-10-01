"""Canonical hashing used for intent, semantic, source-cut and access fingerprints."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if hasattr(o, "to_dict"):
        return o.to_dict()
    raise TypeError(f"not canonically serialisable: {type(o).__name__}")


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ISO dates."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def fingerprint(obj: Any) -> str:
    """`sha256:<hex>` of the canonical JSON form of obj."""
    return "sha256:" + sha256_hex(canonical_json(obj))


def short(fp: str, n: int = 12) -> str:
    return fp.split(":", 1)[-1][:n]
