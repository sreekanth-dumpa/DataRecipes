"""Optional bounded SQL-equivalence certification adapter (VeriEQL [R15]).

If a VeriEQL executable is configured (``AIPR_VERIEQL_CMD``) the adapter would
submit the reconstructed relational expressions with integrity constraints and
a tuple bound.  It is not bundled.  Without it the verdict is ``unsupported`` --
reported explicitly and never counted as verified.  Certification is
developer validation, outside the interactive critical path.
"""
from __future__ import annotations

import os
import shutil
from typing import Any


def check_equivalence(sql_a: str, sql_b: str, constraints: list[str], tuple_bound: int = 4,
                      dialect: str = "duckdb") -> dict[str, Any]:
    cmd = os.environ.get("AIPR_VERIEQL_CMD")
    base = {"verifier": "VeriEQL", "dialect": dialect, "tuple_bound": tuple_bound, "integrity_constraints": constraints,
            "supported_features_note": "window functions, CTAS and engine-specific hash functions may be outside the verifier model"}
    if not cmd or not shutil.which(cmd.split()[0]):
        return {**base, "verdict": "unsupported", "verifier_version": None,
                "reason": "VeriEQL not installed/configured; no bounded verdict established",
                "counts_as_verified": False}
    return {**base, "verdict": "unknown", "verifier_version": "external",
            "reason": "external invocation not implemented in the reference POC", "counts_as_verified": False}
