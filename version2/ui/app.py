"""Streamlit front end for the AIPR reference POC.

Tabs: Ask (interpret -> confirm -> prepare/estimate -> execute -> present -> assess),
Plans & runs, Feedback, Recipe builder, Runtime & trace.
Run from version2/:  streamlit run ui/app.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from client import make_client  # noqa: E402

st.set_page_config(page_title="Information Path Routing", layout="wide")


@st.cache_resource
def client():
    return make_client()


api = client()
SUBJECTS = ["analyst_nc", "analyst_nc_2", "analyst_all", "viewer"]
EXAMPLE = ("What was the 30 day quote conversion for NC personal auto quotes issued in August 2026 "
           "by product version and channel, as known by Sept 15 2026?")

st.title("Agentic Information Path Routing")
st.caption("Evidence-bound information compiler - reference POC (version 2). Fixture data; DuckDB stands in for Snowflake.")

tab_ask, tab_runs, tab_fb, tab_builder, tab_rt = st.tabs(
    ["Ask", "Plans & runs", "Feedback", "Recipe builder", "Runtime & trace"])

# ---------------------------------------------------------------------------- Ask
with tab_ask:
    with st.sidebar:
        st.subheader("Requester")
        subject = st.selectbox("Subject (entitlements)", SUBJECTS, index=0)
        purpose = st.selectbox("Purpose", ["pricing_review", "regulatory_review", "marketing"])
        st.subheader("Execution policy")
        auto_exec = st.checkbox("Auto-execute when within budget envelope", value=False,
                                help="A system policy: admission still checks budget, access and source cut.")
        prefer = st.selectbox("Physical variant", ["(planner choice)", "fused", "staged", "partitioned"])
        stale = st.checkbox("Simulate stale statistics (x0.25)", value=False,
                            help="Deflates estimates so checkpoint adaptation can be observed.")

    mode = st.radio("Input", ["Question", "Form"], horizontal=True)
    if mode == "Question":
        text = st.text_area("Business question", EXAMPLE, height=80)
        if st.button("Interpret", type="primary"):
            st.session_state["req"] = api.interpret(text=text, subject=subject, purpose=purpose)
            st.session_state.pop("exe", None)
    else:
        c1, c2, c3, c4 = st.columns(4)
        state = c1.selectbox("State", ["NC", "SC", "VA"])
        cs = c2.date_input("Cohort start", pd.Timestamp("2026-08-01"))
        ce = c3.date_input("Cohort end", pd.Timestamp("2026-08-31"))
        win = c4.number_input("Window (calendar days)", 1, 120, 30)
        c5, c6 = st.columns(2)
        cutoff = c5.date_input("Knowledge cutoff", pd.Timestamp("2026-09-15"))
        dims = c6.multiselect("Dimensions", ["product_version", "channel", "state", "driver_age"],
                              ["product_version", "channel"])
        if st.button("Interpret form", type="primary"):
            st.session_state["req"] = api.interpret(form={"state": state, "cohort_start": str(cs), "cohort_end": str(ce),
                                                          "window_days": int(win), "knowledge_cutoff": f"{cutoff}T23:59:59Z",
                                                          "dimensions": dims}, subject=subject, purpose=purpose)
            st.session_state.pop("exe", None)

    req = st.session_state.get("req")
    if req:
        rid = req["request_id"]
        req = api.describe_request(rid)
        rec = req["record"]
        i = rec["intent"]
        st.subheader("1. Interpretation")
        route = req["route"]
        badge = {"supported": "Supported", "clarification_required": "Clarification required",
                 "unsupported": "Unsupported"}[route["status"]]
        st.markdown(f"**Route:** {badge} ({route.get('recipe_key') or 'no certified recipe'}) - "
                    f"**intent hash** `{req['intent_hash'][:23]}...`")
        st.dataframe(pd.DataFrame([{
            "population": f"{i['scope']['state']} {i['scope']['lob']}", "cohort (issue date)": f"{i['cohort']['start']} .. {i['cohort']['end']}",
            "window": f"{i['window']['value']} {i['window']['unit']}", "known by": i["knowledge_cutoff"][:10],
            "maturity policy": i["maturity_policy"]}]), hide_index=True, use_container_width=True)
        st.write("Grain / dimensions:", ", ".join(i["dimensions"]), " - metric:", i["metric"])
        if rec["assumptions"]:
            with st.expander(f"Assumptions applied ({len(rec['assumptions'])})"):
                for a in rec["assumptions"]:
                    st.write("-", a)
        for g in route.get("gaps", []):
            st.error(f"{g['field']}: {g['gap']}")

        st.subheader("2. Confirm meaning")
        answers = {}
        for q in req["questions"]:
            if q["kind"] == "maturity":
                st.warning(q["question"])
                opts = [o["value"] for o in q["options"]]
                labels = {o["value"]: o["value"] + ("" if o["supported"] else "  (not installed)") for o in q["options"]}
                answers[q["id"]] = st.radio("Maturity handling", opts, format_func=labels.get, key=f"q_{rid}_{q['id']}")
        with st.expander("Definitions (material distinctions)"):
            for q in req["questions"]:
                if q["kind"] != "definition":
                    continue
                opts = [o["value"] for o in q["options"]]
                cur = opts.index(q["current"]) if q["current"] in opts else 0
                answers[q["id"]] = st.selectbox(q["question"], opts, index=cur, key=f"q_{rid}_{q['id']}",
                                                format_func=lambda v, q=q: v + ("" if next(o for o in q["options"] if o["value"] == v)["supported"] else "  (unsupported)"))
        if req["confirmed"]:
            st.success(f"Confirmed by {rec['confirmation']['actor']} - bound to the intent hash")
        if st.button("Confirm meaning"):
            req = api.confirm(rid, subject, answers)
            st.rerun()

        if req["confirmed"]:
            st.subheader("3. Retrieve, resolve, plan and estimate")
            if st.button("Prepare"):
                st.session_state["prep"] = api.prepare(rid, None if prefer.startswith("(") else prefer,
                                                       0.25 if stale else None)
            prep = st.session_state.get("prep")
            if prep and prep.get("intent_hash") == req["intent_hash"]:
                if prep["status"] == "blocked":
                    for b in prep["blockers"]:
                        st.error(b)
                else:
                    ev = prep.get("evidence", {})
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Obligations satisfied", f"{len(ev.get('obligations', [])) - len(ev.get('missing_obligations', []))}"
                              f"/{len(ev.get('obligations', []))}")
                    c2.metric("Cache", prep["cache"]["kind"])
                    est = prep["estimate"]
                    c3.metric("Estimated duration (s)", f"{est['duration_seconds']['range'][0]:.2f} - {est['duration_seconds']['range'][1]:.2f}")
                    st.caption(f"Notional credits {est['credits_notional']['range']} - model "
                               f"{est.get('model', 'cache')} (uncalibrated estimate, not billed cost)")
                    with st.expander("Evidence manifest"):
                        st.dataframe(pd.DataFrame([{"claim": e["claim_id"], "obligations": ", ".join(e["obligation_ids"]),
                                                    "authority": e["authority_class"], "packets": ", ".join(e["packets"]),
                                                    "reason": e["retrieval_reason"]} for e in ev.get("entries", [])]),
                                     hide_index=True, use_container_width=True)
                        for r in ev.get("rejected", []):
                            st.write(f"Rejected {r['claim_id']}: {r['reason']}")
                        for c in ev.get("contradictions", []):
                            st.write(f"Contradiction {c['claim_id']} on {c['obligation_id']}: {c['resolution']}")
                    if prep.get("advisories"):
                        with st.expander(f"Approved advisory feedback ({len(prep['advisories'])})"):
                            st.json(prep["advisories"])
                    if prep.get("candidates"):
                        with st.expander("Physical candidates (Pareto)", expanded=True):
                            st.dataframe(pd.DataFrame([{
                                "candidate": c["label"], "p50 s": c["estimate"]["duration_seconds"]["p50"],
                                "p90 s": c["estimate"]["duration_seconds"]["p90"],
                                "writes": c["estimate"]["materialized_writes"], "objective": c["objective"],
                                "pareto": c["on_pareto_frontier"], "feasible": c["feasible"],
                                "selected": c["physical_id"] == prep["selected"]["physical_id"]} for c in prep["candidates"]]),
                                hide_index=True, use_container_width=True)
                            st.caption(prep.get("selection_reason", ""))
                            for r in prep.get("rejected_alternatives", []):
                                st.write(f"Rejected **{r['plan']}** ({r['rule']}): {r['detail']}")
                        with st.expander("Profiling probes"):
                            st.json(prep.get("profile", {}).get("probes", []))
                    go_now = st.button("Execute now", type="primary")
                    go_q = st.button("Queue")
                    if go_now or go_q or (auto_exec and "exe" not in st.session_state):
                        st.session_state["exe"] = api.execute(rid, queue=go_q)

        exe = st.session_state.get("exe")
        if exe:
            st.subheader("4. Execute")
            if not exe.get("admitted"):
                st.error(exe.get("reason"))
            else:
                eid = exe["execution_id"]
                st.caption(f"Execution `{eid}` admitted in {exe.get('admission_ms')} ms")
                box = st.empty()
                for _ in range(200):
                    s = api.status(eid)
                    with box.container():
                        st.write(f"State: **{s['state']}** - nodes {s['progress'] or 'n/a'}")
                        if s["nodes"]:
                            st.dataframe(pd.DataFrame(s["nodes"]), hide_index=True, use_container_width=True)
                        if s["adaptations"]:
                            st.info(f"Plan adapted at checkpoint: {s['adaptations']}")
                    if s["state"] in ("complete", "failed", "cancelled"):
                        break
                    time.sleep(0.1)
                if s["state"] not in ("complete", "failed", "cancelled") and st.button("Cancel"):
                    st.write(api.cancel(eid))
                res = api.result(eid)
                if res["state"] == "complete" and res["result"]:
                    r = res["result"]
                    st.subheader("5. Result")
                    st.write(r["narrative"])
                    if r.get("chart"):
                        st.vega_lite_chart(r["chart"], use_container_width=True)
                    df = pd.DataFrame(r["rows"])
                    st.dataframe(df, hide_index=True, use_container_width=True)
                    st.markdown("**Evidence categories**")
                    st.dataframe(pd.DataFrame(r.get("evidence_categories", [])), hide_index=True)
                    st.caption(f"Origin: {r['origin']}")
                    with st.expander("Execution receipt"):
                        st.json(res["receipt"])
                    st.subheader("6. Assess")
                    fb = st.text_input("Feedback (optional)")
                    if st.button("Assess feedback"):
                        a = api.assess(eid, fb)
                        st.json(a["assessment"]["gate_vector"])
                        st.dataframe(pd.DataFrame([{"metric": k, "value": json.dumps(v["value"], default=str),
                                                    "evidence": v["evidence"], "reason": v.get("reason", "")}
                                                   for k, v in a["assessment"]["metrics"].items()]),
                                     hide_index=True, use_container_width=True)
                        for rr in a["assessment"]["recommendations"]:
                            st.write(f"{rr['rank']}. [{rr['priority']}] {rr['recommendation']}")
                        st.caption(f"Saved case {a['assessment']['case_id']} at {a['case_path']}")
                elif res["state"] in ("failed", "cancelled"):
                    st.error(f"{res['state']}: {res['error']}")
                    with st.expander("Receipt"):
                        st.json(res["receipt"])

# ---------------------------------------------------------------------------- Plans & runs
with tab_runs:
    st.subheader("Saved logical plans")
    plans = api.list_plans()
    st.dataframe(pd.DataFrame(plans), hide_index=True, use_container_width=True)
    if plans:
        pid = st.selectbox("Physical versions for plan", [p["plan_id"] for p in plans])
        st.dataframe(pd.DataFrame(api.list_physical(pid)), hide_index=True, use_container_width=True)
    st.subheader("Executions")
    st.dataframe(pd.DataFrame(api.list_executions()), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------------------- Feedback
with tab_fb:
    st.subheader("Feedback cases")
    cases = api.list_feedback()
    st.dataframe(pd.DataFrame(cases), hide_index=True, use_container_width=True)
    if cases:
        cid = st.selectbox("Case", [c["case_id"] for c in cases])
        to = st.selectbox("Transition to", ["evaluated", "approved", "active", "superseded", "rejected"])
        if st.button("Apply transition"):
            try:
                st.json(api.transition_case(cid, to, "ui_user"))
            except Exception as e:  # refused transitions are expected (e.g. executable activation)
                st.error(str(e))
        path = next(c["path"] for c in cases if c["case_id"] == cid)
        md = Path(path) / "assessment.md"
        if md.exists():
            st.markdown(md.read_text())

# ---------------------------------------------------------------------------- Recipe builder
with tab_builder:
    st.subheader("Recipe builder (drafts are never certified by generation)")
    name = st.text_input("Recipe name", "Quote to bind conversion")
    logic = st.text_area("Logic in plain language",
                         "Share of first eligible issued quotes that bind within 30 days; cancellations don't undo a bind.")
    qs = api.builder_questions(logic)
    ans = {}
    for q in qs["questions"]:
        opts = list(q["options"])
        ans[q["id"]] = st.selectbox(q["question"], opts, index=opts.index(qs["suggested"].get(q["id"], opts[0])),
                                    format_func=q["options"].get, key=f"b_{q['id']}")
    owner = st.text_input("Business owner", "pricing-analytics")
    if st.button("Generate draft"):
        d = api.builder_draft(name, logic, ans, owner)
        (st.success if d["algebra_match"]["matches"] else st.warning)(d["algebra_match"]["consequence"])
        st.json(d)

# ---------------------------------------------------------------------------- Runtime & trace
with tab_rt:
    st.subheader("Online runtime")
    st.json(api.runtime_info())
    st.subheader("Trace integrity")
    st.json(api.verify_trace())
    if st.button("Sweep expired / orphaned stage relations"):
        st.json(api.sweep())
    exe = st.session_state.get("exe")
    if exe and exe.get("execution_id"):
        st.subheader(f"Trace for {exe['execution_id']}")
        for e in api.trace(exe["execution_id"]):
            st.write(f"`{e['at']}` **{e['stage']}** - {e['status']}")
