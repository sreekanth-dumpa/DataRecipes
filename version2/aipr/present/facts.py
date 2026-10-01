"""Structured answer facts, deterministic chart spec and evidence categories (spec section 15).

Facts come first; prose and charts are generated from them, so every number
shown links to validated data.  Differences are described as observed
associations.  Confidence is shown as evidence categories, never as a single
model confidence number.
"""
from __future__ import annotations

from typing import Any

# Categorical hues in fixed entity order (never by rank) -- reference palette slots 1..8.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
FIXED_ENTITY_ORDER = {"channel": ["agent", "direct", "web", "unknown"], "state": ["NC", "SC", "VA"],
                      "product_version": ["v3.1", "v3.2"]}


def answer_facts(rows: list[dict[str, Any]], intent: dict[str, Any], origin: dict[str, Any],
                 source_cut: dict[str, Any]) -> dict[str, Any]:
    eligible = sum(int(r["eligible"]) for r in rows)
    converted = sum(int(r["converted"]) for r in rows)
    immature = sum(int(r["immature_excluded"]) for r in rows)
    return {
        "metric": "quote_conversion",
        "cohort": intent["cohort"], "scope": intent["scope"], "window_days": intent["window"]["value"],
        "knowledge_cutoff": intent["knowledge_cutoff"], "dimensions": intent["dimensions"],
        "eligible_mature_quotes": eligible, "converted_quotes": converted,
        "conversion_rate": (converted / eligible) if eligible else None,
        "denominator": "mature first-eligible issued quotes", "immature_excluded": immature,
        "groups": len(rows),
        "source_cut": {k: v.get("content_md5", v.get("at_timestamp")) for k, v in source_cut.get("relations", {}).items()},
        "origin": origin,
    }


def narrative(facts: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    if not facts["eligible_mature_quotes"]:
        return "No mature quotes matched the confirmed population; no rate is reported."
    lines = [f"{facts['converted_quotes']:,} of {facts['eligible_mature_quotes']:,} mature quotes "
             f"({facts['conversion_rate']:.1%}) received a known bind within {facts['window_days']} calendar days "
             f"of issue ({facts['scope']['state']} {facts['scope']['lob']}, issued {facts['cohort']['start']} to "
             f"{facts['cohort']['end']}, as known by {facts['knowledge_cutoff'][:10]})."]
    if facts["immature_excluded"]:
        lines.append(f"{facts['immature_excluded']:,} quotes issued too late to complete the window by the cutoff "
                     "were excluded from both numerator and denominator.")
    rated = [r for r in rows if r.get("conversion_rate") is not None and r["eligible"] >= 30]
    if len(rated) >= 2:
        hi = max(rated, key=lambda r: r["conversion_rate"])
        lo = min(rated, key=lambda r: r["conversion_rate"])
        label = lambda r: " / ".join(str(r[d]) for d in facts["dimensions"]) or "all"
        lines.append(f"Highest observed rate: {label(hi)} ({hi['conversion_rate']:.1%}, n={hi['eligible']}); lowest: "
                     f"{label(lo)} ({lo['conversion_rate']:.1%}, n={lo['eligible']}). These are observed associations, "
                     "not evidence that a product or channel caused the difference.")
    return " ".join(lines)


def chart_spec(rows: list[dict[str, Any]], dims: list[str]) -> dict[str, Any] | None:
    """Deterministic Vega-Lite spec: one axis (rate), grouped bars, fixed entity colors, tooltips with counts."""
    data = [r for r in rows if r.get("conversion_rate") is not None]
    if not data:
        return None
    x = dims[0] if dims else None
    color = dims[1] if len(dims) > 1 else None
    tooltip = [{"field": d, "type": "nominal"} for d in dims] + [
        {"field": "conversion_rate", "type": "quantitative", "format": ".1%", "title": "conversion rate"},
        {"field": "converted", "type": "quantitative", "title": "converted"},
        {"field": "eligible", "type": "quantitative", "title": "eligible (denominator)"},
        {"field": "ci_low", "type": "quantitative", "format": ".1%", "title": "95% CI low"},
        {"field": "ci_high", "type": "quantitative", "format": ".1%", "title": "95% CI high"}]
    enc: dict[str, Any] = {
        "y": {"field": "conversion_rate", "type": "quantitative", "title": "Conversion rate",
              "axis": {"format": "%", "grid": True, "gridOpacity": 0.4}, "scale": {"zero": True}},
        "tooltip": tooltip,
    }
    if x:
        enc["x"] = {"field": x, "type": "nominal", "title": x.replace("_", " "), "axis": {"labelAngle": 0},
                    "sort": FIXED_ENTITY_ORDER.get(x)}
    if color:
        domain = [v for v in FIXED_ENTITY_ORDER.get(color, [])] + sorted(
            {str(r[color]) for r in data} - set(FIXED_ENTITY_ORDER.get(color, [])))
        enc["color"] = {"field": color, "type": "nominal", "title": color.replace("_", " "),
                        "scale": {"domain": domain, "range": SERIES_COLORS[:len(domain)]},
                        "legend": {"orient": "top"}}
        enc["xOffset"] = {"field": color, "sort": domain}
    else:
        enc["color"] = {"value": SERIES_COLORS[0]}
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "description": "Quote conversion rate by " + (" and ".join(dims) or "total"),
        "data": {"values": data},
        "mark": {"type": "bar", "cornerRadiusEnd": 4, "stroke": None},
        "encoding": enc,
        "config": {"bar": {"discreteBandSize": {"band": 0.8}}, "view": {"stroke": None}},
        "width": "container", "height": 320,
    }


def evidence_categories(*, confirmed: bool, recipe_certified: bool, manifest_complete: bool,
                        temporal_ok: bool | None, structural_ok: bool | None, business_verified: str) -> list[dict[str, str]]:
    def s(x):
        return "passed" if x is True else ("failed" if x is False else "not_checked")
    return [
        {"category": "meaning confirmed", "status": "passed" if confirmed else "failed"},
        {"category": "authoritative recipe resolved", "status": "passed" if recipe_certified else "failed"},
        {"category": "source coverage complete", "status": "passed" if manifest_complete else "incomplete"},
        {"category": "temporal checks", "status": s(temporal_ok)},
        {"category": "structural checks", "status": s(structural_ok)},
        {"category": "business truth independently verified", "status": business_verified},
    ]
