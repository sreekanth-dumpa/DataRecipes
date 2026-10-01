"""Interpret / Confirm phases (spec sections 2, 4)."""
from aipr.intent.model import is_confirmed

from conftest import QUESTION, form


def test_text_interpretation_extracts_meaning_fields(svc):
    d = svc.interpret(QUESTION)
    i = d["record"]["intent"]
    assert i["scope"] == {"state": "NC", "lob": "personal_auto"}
    assert i["cohort"] == {"start": "2026-08-01", "end": "2026-08-31"}
    assert i["window"]["value"] == 30 and i["knowledge_cutoff"].startswith("2026-09-15")
    assert i["dimensions"] == ["product_version", "channel"]
    assert d["route"]["status"] == "supported"
    assert not d["confirmed"]


def test_maturity_question_is_material_and_computed(svc):
    d = svc.interpret(QUESTION)
    q = next(q for q in d["questions"] if q["id"] == "maturity_tradeoff")
    assert q["analysis"]["last_mature_issue_date"] == "2026-08-16"
    assert q["analysis"]["immature_cohort_days"] == 15
    assert any(o["value"] == "move_cutoff_to:2026-09-30" for o in q["options"])
    assert any(o["value"] == "censored_cohort_report" and not o["supported"] for o in q["options"])


def test_no_execution_before_confirmation(svc):
    d = svc.interpret(QUESTION)
    p = svc.prepare(d["request_id"])
    assert p["status"] == "blocked" and "not confirmed" in p["blockers"][0]
    assert svc.execute(d["request_id"])["admitted"] is False


def test_edit_invalidates_confirmation(svc):
    d = svc.interpret(form=form())
    svc.confirm(d["request_id"], "tester")
    assert svc.describe_request(d["request_id"])["confirmed"]
    e = svc.edit(d["request_id"], "window.value", 45)
    assert not e["confirmed"]
    assert e["record"]["confirmation"]["status"] == "invalidated"


def test_moving_cutoff_answer_changes_meaning(svc):
    d = svc.interpret(QUESTION)
    c = svc.confirm(d["request_id"], "tester", {"maturity_tradeoff": "move_cutoff_to:2026-09-30"})
    assert c["record"]["intent"]["knowledge_cutoff"] == "2026-09-30T23:59:59Z"
    assert c["maturity"]["fully_mature"] and c["confirmed"]


def test_unsupported_meaning_is_explicit_not_silently_mapped(svc):
    d = svc.interpret("quote conversion net of cancellations for NC personal auto issued in August 2026 by channel as of Sept 15 2026")
    assert d["route"]["status"] == "unsupported"
    gaps = " ".join(g["gap"] for g in d["route"]["gaps"])
    assert "No certified rule resolves cancelled binds" in gaps
    svc.confirm(d["request_id"], "tester")
    p = svc.prepare(d["request_id"])
    assert p["status"] == "blocked"


def test_unsupported_metric_and_dimension(svc):
    d = svc.interpret("loss ratio for NC personal auto in August 2026")
    assert d["route"]["status"] == "unsupported"
    d2 = svc.interpret(form=form(dimensions=["driver_age"]))
    assert d2["route"]["status"] == "unsupported"
    assert "driver_age" in d2["route"]["gaps"][0]["gap"]


def test_conversation_reuse_of_unchanged_confirmation(svc):
    d = svc.interpret(form=form())
    svc.confirm(d["request_id"], "tester")
    child = svc.interpret(form=form(), parent_request_id=d["request_id"], conversation_id=d["record"]["conversation_id"])
    assert child["confirmed"] and child["record"]["confirmation"]["reused_from"] == d["request_id"]
    changed = svc.interpret(form=form(window_days=14), parent_request_id=d["request_id"])
    assert not changed["confirmed"]


def test_deterministic_path_records_agent_invocation(svc):
    d = svc.interpret(QUESTION)
    inv = d["record"]["agent_invocations"][0]
    assert inv["provider"] == "none" and inv["status"] == "not_invoked"
