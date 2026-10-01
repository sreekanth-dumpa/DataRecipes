# DataRecipes version 2: Agentic Information Path Routing

This is a reference POC of the **evidence-bound information compiler** described in
*Agentic Information Path Routing* (1 Oct 2026). It is self-contained. Nothing here
imports from the version-1 code in the repository root.

An agent (or the deterministic interpreter) proposes a meaning. A typed contract
freezes the confirmed meaning. A deterministic compiler produces admitted physical
DAGs, which an always-running online runtime executes. Results are validated and
cached as coverage-aware information sets, each with an execution receipt.
Assessment compares the proposal with the actual run.

> **Central invariant.** These inputs must produce equivalent information across every
> admitted physical plan:
> - the same confirmed meaning
> - the same bound parameters
> - the same source cut
> - the same access decision
> - the same recipe versions
>
> SQL text, DAG shape and partitioning can change. A change of meaning creates a new contract.

## Quick start

```bash
cd version2
pip install -r requirements.txt
python demo.py                      # every workflow phase, end to end, on the fixture
pytest                              # 68 tests
streamlit run ui/app.py             # UI with an embedded always-running service
# or run the service separately and point the UI at it:
uvicorn aipr.api.server:app --port 8100
AIPR_API_URL=http://localhost:8100 streamlit run ui/app.py
```

On first use the service builds the synthetic warehouse at `var/warehouse.duckdb`. The
build is seeded and takes about 1 second. All runtime state lives under `var/`, which
git ignores:
- the control store (`control.db`)
- plan and receipt artefacts
- feedback cases
- recipe drafts

## Workflow phases → code

| Phase | What happens | Module |
|---|---|---|
| Interpret | Text or form becomes the intent record. Assumptions are recorded. An optional provider-neutral LLM proposer can add a proposal. | `aipr/intent/interpreter.py`, `aipr/intent/llm.py` |
| Confirm | Material questions are computed, including the maturity trade-off. The confirmation is bound to the intent hash, so any edit invalidates it. | `aipr/intent/clarify.py`, `aipr/intent/model.py` |
| Retrieve | Obligations are derived from the intent. Hard applicability rules and a weighted set cover build the evidence manifest. Missing obligations block the request. | `aipr/context/` |
| Resolve | Builds the binding contract, the access decision and the source cut. Runs bounded value-of-information probes and caches the profile. | `aipr/resolve/`, `aipr/access/` |
| Plan | Builds the typed logical IR. Compiles fused, staged and partitioned DAGs, plus the rejected raw-join alternative. Plans are saved in the plan registry. | `aipr/ir/`, `aipr/compiler/`, `aipr/control/registry.py` |
| Estimate | Estimates latency and notional credits along the critical path. Builds a Pareto frontier. Budgets act as constraints. | `aipr/planner/` |
| Execute | Online coordinator with leases and warm workers. Query ids are persisted before waiting. Completion events, cancellation, recovery, shared producers and queueing are handled here. | `aipr/runtime/coordinator.py` |
| Adapt | Replans only the unstarted suffix at the canonical-cohort checkpoint, under a validity envelope, a benefit margin and a revision budget. | `aipr/runtime/adaptation.py` |
| Validate and cache | Grain, temporal, subset, conservation and numerator ≤ denominator checks. Only complete, validated results are published. Reuse is exact or a derived roll-up. | `aipr/runtime/validation.py`, `aipr/cache/` |
| Present | Answer facts, narrative, a deterministic Vega-Lite chart and evidence categories. | `aipr/present/facts.py` |
| Receipt and trace | Hash-linked trace and a receipt whose missing values carry reasons. | `aipr/trace/` |
| Assess | Gate vector, the section 16 metrics (null with a reason when not measurable) and a case folder. | `aipr/assess/assessment.py` |
| Improve | Feedback lifecycle. An approval is advisory only and is retrieved for matching scopes. Executable activation is refused. | `aipr/assess/feedback.py` |
| Build recipe | Six obligation-driven questions produce a versioned draft. A draft is never certified by being generated. | `aipr/builder/recipe_builder.py` |
| Verify | Independent Python oracle, cross-route equivalence certificates, a TLP metamorphic harness and a VeriEQL adapter slot. | `aipr/verify/` |

The full mapping from spec sections to code is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Layout

```
version2/
  aipr/                 the package (control plane + engines)
  recipes/              certified recipe contracts (quote_conversion/1.0.0.json)
  context/              packet catalog (P01-P20), evidence fragments, access policies
  fixtures/             hand-labelled adversarial edge cases with expected outputs
  ui/                   Streamlit app + HTTP/embedded client
  tests/                68 tests (see "What the tests establish")
  demo.py               scripted walkthrough
  docs/ARCHITECTURE.md  spec section -> module mapping, design decisions, boundaries
```

## What the tests establish

- **Correctness against hand labels.** Fused, staged and partitioned routes each match
  17 hand-labelled adversarial quotes. The cases cover:
  - draft first iterations
  - several binds on one quote
  - binds outside the window
  - late-arriving binds
  - immature quotes
  - the inclusive window end
  - cancellations
  - unmapped channel codes
  - binds dated before the quote
  - iterations recorded after the cutoff
- **Equivalence.** All routes agree with each other and with an independent Python
  oracle on the synthetic cut, including after checkpoint adaptation.
- **Cache safety.** The adversarial suite produced zero unsafe cache hits:
  - Changed cutoff, finer cohort, other state, new dimension, different entitlements
    and changed source data all miss.
  - Identical entitlements under a different role name hit.
  - Roll-ups sum the counts and never average rates.
- **Admission.** Execution is blocked without confirmation, when meaning is unsupported,
  when an obligation is missing, when access is denied, and when no plan fits the budget.
- **Runtime.**
  - Admission returns an execution id.
  - The suffix is replanned only on a material deviation, and the oracle still agrees afterwards.
  - Cancellation interrupts running statements.
  - The `max_runtime` budget is enforced.
  - A committed CTAS is reconciled after a coordinator crash without being resubmitted.
  - A shared producer survives when one of its consumers cancels.
  - A queued run is admitted later.
- **Governance.**
  - The AST validator rejects DDL, unknown relations, table functions and undeclared binds.
  - The Snowflake rendering uses binds and Time Travel.
  - Tampering with the trace is detected.
  - Receipts mark unavailable telemetry instead of recording zero.

## Implementation boundaries

These boundaries are deliberate and are not hidden.

- **One certified algebra.** Only `quote_conversion@1.0.0` is implemented: personal auto,
  NC/SC/VA, with dimensions product_version, channel and state. Other meanings are
  reported as unsupported.
- **The fixture stands in for Snowflake.**
  - The fixture is a DuckDB file of about 8,000 quotes. Its source cut is a content
    fingerprint.
  - `aipr/engines/snowflake_engine.py` provides async submission, Time Travel-pinned
    reads and `GET_QUERY_OPERATOR_STATS` collection. It is untested here and needs
    `snowflake-connector-python` plus credentials.
  - In live mode the profiler uses configured volume estimates.
- **Estimates are uncalibrated.** The cost model is `fixture_linear_v0`. Credits are
  notional and never billed cost. Actual cost is reported as unavailable.
- **The LLM is optional.** It only proposes interpretations (`AIPR_LLM_PROVIDER=anthropic`).
  The planner is deterministic.
- **Single host.** The control store is SQLite. Leases are single-host. The UI can host
  the service in-process, or the FastAPI app can run as a separate long-lived service.
- **Verification is limited.** Empirical equivalence holds on a fixed cut only. The
  VeriEQL adapter reports `unsupported` until a verifier is configured. TLP covers only
  projection/filter queries.
- **The trace is not tamper-proof.** Hash linking detects modification relative to a
  trusted anchor.
- **Not implemented:**
  - production identity
  - a live enterprise context service
  - a semantic-graph service
  - Bao-style learned selection
  - generalized multi-hop predicate transfer
  - output paging and export
  - hierarchical decomposition across several recipes

No result here is a benchmark at scale. The architecture provides the mechanisms, and
scale has to be measured.
