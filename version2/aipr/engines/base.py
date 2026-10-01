"""Execution engine interface.  The control plane never pulls source rows; it
submits parameterised statements, receives counts/small aggregates, and records
query ids *before* awaiting completion (spec section 10.3)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class StatementResult:
    query_id: str
    elapsed_ms: float
    telemetry: dict[str, Any] = field(default_factory=dict)


class Engine(Protocol):
    name: str
    dialect_name: str

    def fetch(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...
    def execute(self, sql: str, params: dict[str, Any], query_id: str) -> StatementResult: ...
    def interrupt(self, query_id: str) -> bool: ...
    def relation_exists(self, name: str) -> bool: ...
    def drop_relation(self, name: str) -> None: ...
    def source_cut(self) -> dict[str, Any]: ...
    def schema_fingerprint(self) -> str: ...
    def query_status(self, query_id: str) -> str: ...
