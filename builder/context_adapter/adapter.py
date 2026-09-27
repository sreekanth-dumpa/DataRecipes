"""
Context and FDP adapter (Section 6.1).

Real function: read schema, contracts, ontology bindings, mappings,
lineage and quality signals from Snowflake, Glue/Iceberg and the Kairos
context folder documentation for Quote and Policy. Output: versioned
source descriptors and evidence references, read with least privilege,
with no source data copied into the recipe registry.

STUB STATUS: the real Kairos context folder path was not available when
this pass was built (per plan review). This module returns hardcoded
placeholder descriptors for exactly the source tables the five seed
recipes reference, so the builder and recipe authoring flow have
something to bind against. Every field below is marked stub=True.
Swap in a real implementation by replacing StubContextAdapter with one
that reads the actual context folder / Snowflake INFORMATION_SCHEMA /
Glue Catalog, keeping the same SourceDescriptor shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ColumnDescriptor:
    name: str
    type: str
    ontology_concept: str | None = None
    stub: bool = True


@dataclass
class SourceDescriptor:
    fully_qualified_name: str
    fdp: str
    effective_dated: bool
    columns: list[ColumnDescriptor] = field(default_factory=list)
    lineage_evidence: list[str] = field(default_factory=list)
    quality_signals: dict[str, str] = field(default_factory=dict)
    stub: bool = True


class ContextAdapter:
    """Interface. Real implementations read Snowflake / Glue / docs;
    boundary: least-privilege reads, no source data persisted here."""

    def get_source_descriptor(self, fully_qualified_name: str) -> SourceDescriptor:
        raise NotImplementedError

    def list_sources(self, fdp: str) -> list[SourceDescriptor]:
        raise NotImplementedError


class StubContextAdapter(ContextAdapter):
    """Placeholder descriptors for QUOTE_FDP and POLICY_FDP, covering only
    the tables referenced by the five POC seed recipes. Not sourced from
    a real context folder -- see module docstring."""

    _DESCRIPTORS: dict[str, SourceDescriptor] = {
        "QUOTE_FDP.QUOTE_JOURNEY": SourceDescriptor(
            fully_qualified_name="QUOTE_FDP.QUOTE_JOURNEY",
            fdp="Quote",
            effective_dated=True,
            columns=[
                ColumnDescriptor("quote_journey_id", "string", "QuoteJourney.id"),
                ColumnDescriptor("is_eligible", "boolean"),
                ColumnDescriptor("exclusion_reason", "string"),
                ColumnDescriptor("effective_date", "date", "AsOf.effectiveDate"),
                ColumnDescriptor("journey_date", "date"),
                ColumnDescriptor("product_version", "string", "Product.version"),
            ],
            lineage_evidence=["stub: no real lineage graph wired yet"],
            quality_signals={"freshness": "unknown (stub)", "completeness": "unknown (stub)"},
        ),
        "QUOTE_FDP.RATED_DRIVER": SourceDescriptor(
            fully_qualified_name="QUOTE_FDP.RATED_DRIVER",
            fdp="Quote",
            effective_dated=False,
            columns=[
                ColumnDescriptor("quote_journey_id", "string", "QuoteJourney.id"),
                ColumnDescriptor("young_driver_segment", "string"),
            ],
            lineage_evidence=["stub: no real lineage graph wired yet"],
            quality_signals={"freshness": "unknown (stub)"},
        ),
        "POLICY_FDP.POLICY": SourceDescriptor(
            fully_qualified_name="POLICY_FDP.POLICY",
            fdp="Policy",
            effective_dated=True,
            columns=[
                ColumnDescriptor("quote_journey_id", "string", "QuoteJourney.id"),
                ColumnDescriptor("bound_policy_id", "string", "Policy.id"),
                ColumnDescriptor("is_bound", "boolean"),
                ColumnDescriptor("effective_date", "date", "AsOf.effectiveDate"),
            ],
            lineage_evidence=["stub: no real lineage graph wired yet"],
            quality_signals={"freshness": "unknown (stub)", "completeness": "unknown (stub)"},
        ),
    }

    def get_source_descriptor(self, fully_qualified_name: str) -> SourceDescriptor:
        if fully_qualified_name not in self._DESCRIPTORS:
            raise KeyError(
                f"{fully_qualified_name}: no stub descriptor. "
                f"Real Kairos context folder not wired yet -- only the 5 POC seed-recipe tables are stubbed."
            )
        return self._DESCRIPTORS[fully_qualified_name]

    def list_sources(self, fdp: str) -> list[SourceDescriptor]:
        return [d for d in self._DESCRIPTORS.values() if d.fdp.lower() == fdp.lower()]
