"""Certified recipe registry (read-only in the request path).

Recipes are versioned JSON contracts under version2/recipes/<id>/<version>.json.
The resolver performs the *hard match* of section 8: a recipe is admitted only
when every meaning-bearing intent field is a supported option.  Unsupported
meanings are reported explicitly, never silently mapped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.hashing import fingerprint


@dataclass
class Recipe:
    body: dict[str, Any]

    @property
    def id(self) -> str:
        return self.body["id"]

    @property
    def version(self) -> str:
        return self.body["version"]

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def certified(self) -> bool:
        return self.body.get("certification", {}).get("status") == "certified"

    @property
    def content_hash(self) -> str:
        return fingerprint(self.body)

    def option_supported(self, name: str, value: str) -> tuple[bool, str | None]:
        opt = self.body["options"].get(name)
        if opt is None:
            return False, f"recipe has no option '{name}'"
        if value in opt["supported"]:
            return True, None
        return False, opt.get("rejected", {}).get(value, f"'{value}' is not a supported {name}")


@dataclass
class RouteResolution:
    status: str  # supported | clarification_required | unsupported
    recipe_key: str | None
    gaps: list[dict[str, str]] = field(default_factory=list)
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    candidates_considered: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class RecipeRegistry:
    def __init__(self, recipes_dir: Path):
        self.recipes: dict[str, Recipe] = {}
        for p in sorted(Path(recipes_dir).glob("*/*.json")):
            r = Recipe(json.loads(p.read_text()))
            self.recipes[r.key] = r

    def get(self, key: str) -> Recipe:
        return self.recipes[key]

    def latest(self, recipe_id: str) -> Recipe | None:
        found = [r for r in self.recipes.values() if r.id == recipe_id and r.certified]
        return sorted(found, key=lambda r: tuple(int(x) for x in r.version.split(".")))[-1] if found else None

    def candidates(self, intent: dict[str, Any]) -> list[Recipe]:
        """Verified exact routes first (metric id / candidate list), then pattern matches."""
        out: list[Recipe] = []
        for rid in intent.get("candidate_recipes", []):
            r = self.latest(rid)
            if r and r not in out:
                out.append(r)
        text = (intent.get("metric") or "").replace("_", " ")
        for r in self.recipes.values():
            if r.certified and r not in out and any(p in text for p in r.body["intent_patterns"]):
                out.append(r)
        return out

    def resolve(self, intent: dict[str, Any]) -> RouteResolution:
        cands = self.candidates(intent)
        if not cands:
            return RouteResolution("unsupported", None, gaps=[{
                "field": "metric", "gap": f"No certified recipe implements metric '{intent.get('metric')}'"}])
        results = []
        for r in cands:
            results.append(self._hard_match(r, intent))
        ok = [x for x in results if x.status == "supported"]
        if len(ok) > 1:
            # Two routes with different meanings must not be cost-ranked into a hidden definition choice.
            return RouteResolution("clarification_required", None, candidates_considered=[x.recipe_key for x in ok],
                                   clarifications=[{"question": "Several certified recipes match; which definition?",
                                                    "options": [x.recipe_key for x in ok]}])
        if ok:
            ok[0].candidates_considered = [c.key for c in cands]
            return ok[0]
        best = results[0]
        best.candidates_considered = [c.key for c in cands]
        return best

    @staticmethod
    def _hard_match(r: Recipe, intent: dict[str, Any]) -> RouteResolution:
        gaps: list[dict[str, str]] = []
        sem = intent.get("semantics", {})
        for name in r.body["options"]:
            value = sem.get(name)
            if value is None:
                gaps.append({"field": name, "gap": "unresolved meaning (needs confirmation)"})
                continue
            ok, why = r.option_supported(name, value)
            if not ok:
                gaps.append({"field": name, "gap": why or "unsupported"})
        if intent.get("population") not in (None, r.body["population"]):
            gaps.append({"field": "population", "gap": f"population '{intent.get('population')}' is not this recipe's"})
        scope = intent.get("scope", {})
        params = r.body["parameters"]
        if scope.get("lob") not in params["lob"]["values"]:
            gaps.append({"field": "scope.lob", "gap": f"lob '{scope.get('lob')}' outside recipe applicability"})
        if scope.get("state") not in params["state"]["values"]:
            gaps.append({"field": "scope.state", "gap": f"state '{scope.get('state')}' outside recipe applicability"})
        w = intent.get("window", {})
        if not (params["window_days"]["min"] <= int(w.get("value", 0)) <= params["window_days"]["max"]):
            gaps.append({"field": "window", "gap": "window outside certified 1..90 day range"})
        if w.get("unit") != "calendar_day":
            gaps.append({"field": "window.unit", "gap": "only calendar_day windows are certified"})
        for d in intent.get("dimensions", []):
            if d not in r.body["dimensions"]["allowed"]:
                gaps.append({"field": "dimensions", "gap": f"dimension '{d}' is not supported by {r.key}"})
        if len(intent.get("dimensions", [])) > r.body["dimensions"]["max"]:
            gaps.append({"field": "dimensions", "gap": "too many dimensions"})
        unresolved = [g for g in gaps if g["gap"].startswith("unresolved")]
        if gaps and len(unresolved) == len(gaps):
            return RouteResolution("clarification_required", r.key, gaps=gaps,
                                   clarifications=[{"field": g["field"]} for g in unresolved])
        return RouteResolution("unsupported" if gaps else "supported", r.key, gaps=gaps)
