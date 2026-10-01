"""HTTP API round trip: admission returns an execution id; status is polled (spec section 10.3)."""
import time

from fastapi.testclient import TestClient

from aipr.api import server

from conftest import QUESTION


def test_http_round_trip(fresh_svc):
    server.set_service(fresh_svc)
    c = TestClient(server.app)
    assert c.get("/health").json()["status"] == "ok"
    r = c.post("/requests", json={"text": QUESTION}).json()
    rid = r["request_id"]
    assert c.post(f"/requests/{rid}/execute", json={}).json()["admitted"] is False
    c.post(f"/requests/{rid}/confirm", json={"actor": "api", "answers": {"maturity_tradeoff": "exclude_without_complete_window"}})
    prep = c.post(f"/requests/{rid}/prepare", json={"prefer_variant": "staged"}).json()
    assert prep["status"] == "ready"
    ex = c.post(f"/requests/{rid}/execute", json={}).json()
    eid = ex["execution_id"]
    for _ in range(300):
        st = c.get(f"/executions/{eid}").json()
        if st["state"] == "complete" and c.get(f"/executions/{eid}/result").json()["receipt"]:
            break
        time.sleep(0.05)
    res = c.get(f"/executions/{eid}/result").json()
    assert res["result"]["facts"]["immature_excluded"] > 0
    assert res["result"]["chart"]["mark"]["type"] == "bar"
    assert c.get(f"/executions/{eid}/trace").json()
    assert c.get("/trace/verify").json()["ok"]
    a = c.post(f"/executions/{eid}/assess", json={"feedback": ""}).json()
    case = a["assessment"]["case_id"]
    assert c.post(f"/feedback/{case}/transition", json={"to": "active", "actor": "x"}).status_code == 409
    assert c.get("/plans").json()
    assert len(c.get("/builder/questions").json()["questions"]) == 6
