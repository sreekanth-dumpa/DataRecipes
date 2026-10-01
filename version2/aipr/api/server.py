"""Interactive request API for the always-running execution service (spec section 10.3).

Run:  uvicorn aipr.api.server:app --port 8100
The API returns an execution id after durable admission; clients poll
``GET /executions/{id}`` (a push channel can be added) and can reconnect to an
execution id without holding a worker session.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..service import AIPRService

app = FastAPI(title="Agentic Information Path Routing", version="2.0.0")
_service: AIPRService | None = None


def service() -> AIPRService:
    global _service
    if _service is None:
        _service = AIPRService()
    return _service


def set_service(svc: AIPRService) -> None:
    global _service
    _service = svc


class InterpretBody(BaseModel):
    text: str | None = None
    form: dict[str, Any] | None = None
    subject: str = "analyst_nc"
    purpose: str = "pricing_review"
    conversation_id: str | None = None
    parent_request_id: str | None = None
    constraints: dict[str, Any] | None = None


class ConfirmBody(BaseModel):
    actor: str
    answers: dict[str, str] = Field(default_factory=dict)


class EditBody(BaseModel):
    path: str
    value: Any


class PrepareBody(BaseModel):
    prefer_variant: str | None = None
    stats_scale: float | None = None


class ExecuteBody(BaseModel):
    queue: bool = False
    priority: int = 0


class AssessBody(BaseModel):
    feedback: str = ""
    rating: str | None = None


class CaseBody(BaseModel):
    to: str
    actor: str
    note: str = ""


class DraftBody(BaseModel):
    name: str
    logic: str
    answers: dict[str, str]
    owner: str


def _req(request_id: str) -> None:
    if service().store.get_request(request_id) is None:
        raise HTTPException(404, "unknown request")


def _exe(execution_id: str) -> None:
    if service().store.get_execution(execution_id) is None:
        raise HTTPException(404, "unknown execution")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", **service().runtime_info()}


@app.post("/requests")
def interpret(body: InterpretBody) -> dict[str, Any]:
    return service().interpret(body.text, body.form, body.subject, body.purpose, body.conversation_id,
                               body.parent_request_id, body.constraints)


@app.get("/requests/{request_id}")
def get_request(request_id: str) -> dict[str, Any]:
    _req(request_id)
    return service().describe_request(request_id)


@app.post("/requests/{request_id}/edit")
def edit(request_id: str, body: EditBody) -> dict[str, Any]:
    _req(request_id)
    return service().edit(request_id, body.path, body.value)


@app.post("/requests/{request_id}/confirm")
def confirm(request_id: str, body: ConfirmBody) -> dict[str, Any]:
    _req(request_id)
    return service().confirm(request_id, body.actor, body.answers)


@app.post("/requests/{request_id}/prepare")
def prepare(request_id: str, body: PrepareBody) -> dict[str, Any]:
    _req(request_id)
    p = service().prepare(request_id, body.prefer_variant, body.stats_scale)
    return {k: v for k, v in p.items() if k != "selected_plan"}


@app.post("/requests/{request_id}/execute")
def execute(request_id: str, body: ExecuteBody) -> dict[str, Any]:
    _req(request_id)
    return service().execute(request_id, body.queue, body.priority)


@app.get("/executions")
def executions(limit: int = 50) -> list[dict[str, Any]]:
    return service().list_executions(limit)


@app.get("/executions/{execution_id}")
def status(execution_id: str) -> dict[str, Any]:
    _exe(execution_id)
    return service().status(execution_id)


@app.get("/executions/{execution_id}/result")
def result(execution_id: str) -> dict[str, Any]:
    _exe(execution_id)
    return service().result(execution_id)


@app.post("/executions/{execution_id}/cancel")
def cancel(execution_id: str) -> dict[str, Any]:
    _exe(execution_id)
    return service().cancel(execution_id)


@app.post("/executions/{execution_id}/assess")
def assess(execution_id: str, body: AssessBody) -> dict[str, Any]:
    _exe(execution_id)
    return service().assess(execution_id, body.feedback, body.rating)


@app.get("/executions/{execution_id}/trace")
def trace(execution_id: str) -> list[dict[str, Any]]:
    _exe(execution_id)
    return [e["body"] for e in service().tracer.events(execution_id=execution_id)]


@app.get("/plans")
def plans() -> list[dict[str, Any]]:
    return service().registry.list_plans()


@app.get("/plans/{plan_id}/physical")
def physical(plan_id: str) -> list[dict[str, Any]]:
    return service().registry.list_physical(plan_id)


@app.get("/feedback")
def feedback() -> list[dict[str, Any]]:
    return service().feedback.list()


@app.post("/feedback/{case_id}/transition")
def transition(case_id: str, body: CaseBody) -> dict[str, Any]:
    try:
        return service().transition_case(case_id, body.to, body.actor, body.note)
    except (ValueError, PermissionError) as e:
        raise HTTPException(409, str(e))


@app.get("/builder/questions")
def builder_questions(logic: str = "") -> dict[str, Any]:
    return service().builder_questions(logic)


@app.post("/builder/drafts")
def builder_draft(body: DraftBody) -> dict[str, Any]:
    return service().builder_draft(body.name, body.logic, body.answers, body.owner)


@app.get("/trace/verify")
def verify_trace(anchor: str | None = None) -> dict[str, Any]:
    return service().tracer.verify_chain(anchor)


@app.post("/maintenance/sweep")
def sweep() -> dict[str, Any]:
    return service().sweep()
