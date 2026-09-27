"""
Real (non-stubbed) context/FDP adapter (Section 6.1), backed by an OSI
(Open Semantic Interchange / Apache Ossie Core Metadata Spec) context file.

Supersedes StubContextAdapter for the tables covered by
context/osi/quote_policy_auto_home.yaml -- i.e. everything in the
synthetic Auto+Home warehouse (data/warehouse.duckdb). StubContextAdapter
in adapter.py remains as a fallback shape/reference for tables that don't
yet have real OSI context.

OSI spec: https://github.com/open-semantic-interchange/OSI
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .adapter import ColumnDescriptor, ContextAdapter, SourceDescriptor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OSI_PATH = REPO_ROOT / "context" / "osi" / "quote_policy_auto_home.yaml"

_SCHEMA_TO_FDP = {
    "quote_fdp": "Quote",
    "policy_fdp": "Policy",
}


def _first_ansi_expression(field: dict) -> str | None:
    for dialect_entry in field.get("expression", {}).get("dialects", []):
        if dialect_entry.get("dialect") == "ANSI_SQL":
            return dialect_entry.get("expression")
    return None


def _ai_synonym(entry: dict) -> str | None:
    ai_context = entry.get("ai_context")
    if isinstance(ai_context, dict):
        synonyms = ai_context.get("synonyms") or []
        return synonyms[0] if synonyms else None
    return None


class OSIContextAdapter(ContextAdapter):
    """Reads an OSI semantic model YAML and exposes it through the same
    SourceDescriptor/ColumnDescriptor shape as the stub adapter, so the
    builder doesn't need to know which one backs a given table.

    Boundary (Section 6.1): reads the OSI context file only -- never
    queries the warehouse itself, never persists source data."""

    def __init__(self, osi_path: Path = DEFAULT_OSI_PATH):
        self.osi_path = osi_path
        self._model = yaml.safe_load(osi_path.read_text())
        self._datasets_by_source = {d["source"]: d for d in self._model.get("datasets", [])}

    def _to_source_descriptor(self, dataset: dict) -> SourceDescriptor:
        columns = []
        has_effective_date = False
        for field in dataset.get("fields", []):
            expr = _first_ansi_expression(field) or field["name"]
            if field["name"] == "effective_date" or expr == "effective_date":
                has_effective_date = True
            columns.append(ColumnDescriptor(
                name=field["name"],
                type=field.get("datatype", "Opaque"),
                ontology_concept=_ai_synonym(field),
                stub=False,
            ))

        schema_prefix = dataset["source"].split(".")[0]
        return SourceDescriptor(
            fully_qualified_name=dataset["source"],
            fdp=_SCHEMA_TO_FDP.get(schema_prefix, schema_prefix),
            effective_dated=has_effective_date,
            columns=columns,
            lineage_evidence=[f"OSI context: {self.osi_path.relative_to(REPO_ROOT)}, dataset '{dataset['name']}'"],
            quality_signals={"note": "no quality signals modeled in this OSI context (out of POC scope)"},
            stub=False,
        )

    def get_source_descriptor(self, fully_qualified_name: str) -> SourceDescriptor:
        dataset = self._datasets_by_source.get(fully_qualified_name)
        if dataset is None:
            raise KeyError(f"{fully_qualified_name}: not covered by {self.osi_path.relative_to(REPO_ROOT)}")
        return self._to_source_descriptor(dataset)

    def list_sources(self, fdp: str) -> list[SourceDescriptor]:
        return [
            self._to_source_descriptor(d)
            for d in self._model.get("datasets", [])
            if _SCHEMA_TO_FDP.get(d["source"].split(".")[0], "").lower() == fdp.lower()
        ]

    def get_metrics(self) -> list[dict]:
        """OSI metrics aren't part of SourceDescriptor (which models tables,
        not derived measures); exposed separately for anything that wants
        to cross-reference the OSI context's own metric definitions
        against recipe outputs (e.g. young_driver_bind_rate vs.
        quote.bind_rate_by_young_driver)."""
        return self._model.get("metrics", [])
