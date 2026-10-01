"""UI-side client.  With AIPR_API_URL set, the UI talks to the separately running
online service over HTTP; otherwise it hosts one service instance per
Streamlit server process (still always-running: its coordinator and warm
workers live as long as the process)."""
from __future__ import annotations

import os
from typing import Any

import requests


class HttpClient:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _get(self, path: str, **params: Any) -> Any:
        r = requests.get(self.base + path, params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        r = requests.post(self.base + path, json=body, timeout=120)
        r.raise_for_status()
        return r.json()

    def interpret(self, text=None, form=None, subject="analyst_nc", purpose="pricing_review", **kw):
        return self._post("/requests", {"text": text, "form": form, "subject": subject, "purpose": purpose, **kw})

    def describe_request(self, rid):
        return self._get(f"/requests/{rid}")

    def confirm(self, rid, actor, answers=None):
        return self._post(f"/requests/{rid}/confirm", {"actor": actor, "answers": answers or {}})

    def edit(self, rid, path, value):
        return self._post(f"/requests/{rid}/edit", {"path": path, "value": value})

    def prepare(self, rid, prefer_variant=None, stats_scale=None):
        return self._post(f"/requests/{rid}/prepare", {"prefer_variant": prefer_variant, "stats_scale": stats_scale})

    def execute(self, rid, queue=False, priority=0):
        return self._post(f"/requests/{rid}/execute", {"queue": queue, "priority": priority})

    def status(self, eid):
        return self._get(f"/executions/{eid}")

    def result(self, eid):
        return self._get(f"/executions/{eid}/result")

    def cancel(self, eid):
        return self._post(f"/executions/{eid}/cancel", {})

    def assess(self, eid, feedback="", rating=None):
        return self._post(f"/executions/{eid}/assess", {"feedback": feedback, "rating": rating})

    def trace(self, eid):
        return self._get(f"/executions/{eid}/trace")

    def list_executions(self, limit=50):
        return self._get("/executions", limit=limit)

    def list_plans(self):
        return self._get("/plans")

    def list_physical(self, plan_id):
        return self._get(f"/plans/{plan_id}/physical")

    def list_feedback(self):
        return self._get("/feedback")

    def transition_case(self, case_id, to, actor, note=""):
        return self._post(f"/feedback/{case_id}/transition", {"to": to, "actor": actor, "note": note})

    def builder_questions(self, logic=""):
        return self._get("/builder/questions", logic=logic)

    def builder_draft(self, name, logic, answers, owner):
        return self._post("/builder/drafts", {"name": name, "logic": logic, "answers": answers, "owner": owner})

    def verify_trace(self):
        return self._get("/trace/verify")

    def runtime_info(self):
        return self._get("/health")

    def sweep(self):
        return self._post("/maintenance/sweep", {})


class EmbeddedClient:
    def __init__(self):
        from aipr.service import AIPRService

        self.svc = AIPRService()

    def __getattr__(self, name):
        return getattr(self.svc, name)

    def trace(self, eid):
        return [e["body"] for e in self.svc.tracer.events(execution_id=eid)]

    def list_plans(self):
        return self.svc.registry.list_plans()

    def list_physical(self, plan_id):
        return self.svc.registry.list_physical(plan_id)

    def list_feedback(self):
        return self.svc.feedback.list()

    def verify_trace(self):
        return self.svc.tracer.verify_chain()

    def prepare(self, rid, prefer_variant=None, stats_scale=None):
        p = self.svc.prepare(rid, prefer_variant=prefer_variant, stats_scale=stats_scale)
        return {k: v for k, v in p.items() if k != "selected_plan"}


def make_client():
    url = os.environ.get("AIPR_API_URL")
    return HttpClient(url) if url else EmbeddedClient()
