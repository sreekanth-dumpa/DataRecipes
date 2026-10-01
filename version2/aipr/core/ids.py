"""Identifiers.  Trace and span ids follow the OpenTelemetry widths (32/16 hex)."""
from __future__ import annotations

import secrets
import uuid


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def trace_id() -> str:
    return secrets.token_hex(16)


def span_id() -> str:
    return secrets.token_hex(8)
