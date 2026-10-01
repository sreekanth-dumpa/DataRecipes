# Architecture: specification section → implementation

This document records how each section of *Agentic Information Path Routing* became
code. For each section it gives the deliberate design decision and the claim boundary.

## Planes

- **Control plane** (`aipr/`). Holds intent, evidence, the plan registry, execution
  state, policy decisions, receipts and feedback.
  - Durable state lives in `aipr/control/store.py`. It uses SQLite with WAL and covers
    requests, logical plans, physical plans, executions, node runs and attempts,
    adaptations, information sets, profiles, stage relations, trace events and feedback
    cases.
  - Immutable artefacts are also written under `var/artifacts/`: plans, receipts and
    request text.
- **Data plane** (`aipr/engines/`). Holds the source relations (`src_quote`, `src_policy`,
  `ref`), the governed stage relations (`stage.*`) and the cached result relations
  (`cache.*`). The control plane only ever pulls counts and small aggregates.
- **Presentation plane** (`ui/`, `aipr/present/`). Receives only validated aggregates,
  as facts, a chart spec and tables.

## Section-by-section

### §2 Workflow and records
`AIPRService` in `aipr/service.py` implements one method per phase. The admission
rules are:
- `prepare` refuses to run before the confirmation is bound to the current intent hash.
- `execute` re-checks the confirmation, the preparation hash, access and the source cut.
- Confirmation (meaning) and admission (execution) are separate decisions.
- Conversation reuse: an intent that is equal to its confirmed parent's inherits the
  confirmation (`reused_from`).

### §3 Reference architecture
- The interface is provider-neutral: `IntentProposer`, with `NullProposer` and
  `AnthropicProposer` as adapters.
- The runtime is online: `Coordinator` starts with the service, owns a bounded pool of
  warm workers, wakes on completion events and polls every 250 ms as a fallback for
  reconciliation.
- No cron jobs, DAG files or Airflow are involved. A graph is data: a persisted
  physical-plan JSON.

### §4 Intent record
`aipr/intent/model.py` follows the template fields. It adds `semantics`, which holds the
explicit definition choices: conversion event, day basis, version attribution,
cancelled binds, maturity and quote identity. Three hashes derive from the record:
- `intent_hash` covers the whole interpretation.
- `semantic_fingerprint` covers meaning plus bound parameters plus the recipe.
- `coverage_fingerprint` is the semantic fingerprint without dimensions, used for derived
  reuse.

`clarify.maturity_analysis` computes how many cohort days cannot mature by the cutoff.
It offers three options:
- exclude the immature quotes (the default)
- move the cutoff
- a censored report, which is shown and marked as not installed

### §5 Obligation-directed retrieval
`derive_obligations` turns the intent into obligations. `retrieve` then:
1. ranks fragments by similarity (a stand-in for embeddings)
2. filters them with hard applicability rules: lob, state and the validity interval
   against the cutoff
3. rejects non-authoritative claims, recording any conflicts as contradictions
4. runs a greedy weighted set cover

The manifest records, for each claim: obligation ids, locator, hash, version, authority,
scope, knowledge timestamp, business-time interval, retrieval reason and contradiction
status. Missing obligations become specific gaps that block the request. The fixture has
five certified fragments and one non-authoritative wiki claim that contradicts the bind
linkage. The claim is retrieved by similarity and then rejected. The P01–P20 families
are listed in `context/packets.json`.

### §6 Physical resolution and probes
- `binding_contract` maps meaning to entities, keys and relationships (one-to-many binds,
  a many-to-one crosswalk that keeps unmatched codes) and to the row policy.
- `run_profile` executes four exact probes, each declaring the decision it informs:
  - mature cohort size
  - canonical key uniqueness
  - bind multiplicity with heavy hitters
  - crosswalk multiplicity
- Probes respect the discovery budget and probe count. Their results are cached per
  source cut and scope, so a repeated question does not re-profile.
- Duplicate canonical keys or a fan-out crosswalk block admission.

### §7 Typed IR and compilation
- **Logical IR** (`aipr/ir/logical.py`): operators, grains, joins with relationship and
  reduction, temporal anchors, obligations, and a pinned Python operator. The
  `template_fingerprint` identifies the reusable plan definition independently of bound
  values.
- **Operator catalog and rewrite rules** (`aipr/ir/operators.py`): every rule carries its
  preconditions and evidence class.
- **Compiler** (`aipr/compiler/`): dialect-aware fragments, with values always as binds
  and identifiers from allowlists.
- **AST validator** (sqlglot): checks run on the parsed tree and cover:
  - single-statement SELECT or CTAS only
  - governed CTAS schemas only
  - allowlisted relations only
  - no table functions, file readers or DDL/DML
  - placeholders equal to the declared parameters
- **DAG checks**: acyclic, bounded, outputs reachable.
- **Python operators**: chosen by id and version only. Parameters are allowlisted, the
  determinism class is declared and a runtime digest is recorded.

### §7.1 Rewrite verification scope
`certify_equivalence` runs every admitted variant on the fixed cut and compares each
against the others and against the oracle. It stores an `empirically_equivalent`
certificate, or a `counterexample`, on the logical plan. The VeriEQL slot reports
`unsupported` and `counts_as_verified: false`. Certification is developer validation and
is never on the interactive path.

### §8 Routing and optimisation
`RecipeRegistry.resolve` performs the hard match. Two supported meanings lead to a
clarification and are never cost-ranked. The cache is looked up after meaning and context
resolution and before profiling. Candidates are fused, staged and partitioned(N).
`costing.choose` then:
- applies budget and runtime as constraints
- builds the Pareto frontier over (p50 latency, notional credits, observability)
- minimises a normalised objective, unless the user picks among admitted equivalents

Every value is labelled as an estimate from an uncalibrated model.

### §9 Join explosion
`planner/cardinality.py` builds row envelopes from the probes. The raw-join cardinality
is Σ n_left·n_right, reported with max and p95 multiplicity and heavy hitters. The naive
raw join is emitted as a rejected alternative with rule `quote_grain_violation`. The
compiled route uses the key-reduction rule: semi-join plus GROUP BY to one outcome per
quote. Grain assertions after each materialisation block publication. Envelope
deviations are classified as performance observations, not correctness failures.

### §10 Online multi-DAG execution
- **Saved plans.** Logical and physical versions are immutable and content-hashed. Each
  plan carries an applicability record: parameter schema, schema fingerprint, policy
  version, recipe hash and statistics envelope. A semantic invalidation creates a new
  logical version. Statistical drift only affects the preferred physical variant.
  Repeated questions with new parameters reuse the plan definition (`plan_reused`).
- **Protocol.** Admission returns an execution id. Nodes are leased from durable state.
  The worker then:
  1. AST-validates the resolved CTAS
  2. persists the query id and stage relation
  3. executes
  4. emits a completion event

  The coordinator validates the output before admitting the relation and dispatching
  dependents.
- **Adaptation.** At the `canonical_cohort` checkpoint, the adapter does nothing while
  the observation is inside the planned envelope. Otherwise it evaluates suffix costs
  for 1..max partitions. It replans only the unstarted suffix, and only when the saving
  is at least 15% after overhead and the revision budget allows. The new physical
  version and an adaptation record (validity range, payoff, rules, assertions) are
  persisted. Completed nodes are never re-run.
- **Recovery.** If an attempt has a recorded query id and its output relation exists, it
  is reconciled, because the CTAS committed even though the response was lost. Otherwise
  it is marked orphaned and requeued as a new attempt. The retry limit is 2 for
  transient errors. Permission and AST errors are permanent.
- **Shared producers.** An identical semantic, source and access contract that is
  already in flight is attached as `waiting`. Cancelling a consumer detaches it.
  Cancelling a producer that still has waiters is refused.
- **Cancellation and timeouts.** Running statements are interrupted, pending nodes are
  cancelled, stage relations are marked orphaned for the sweeper, and an honest receipt
  is written.

### §11 Memory and volume
The control plane never materialises source rows. The partitioned suffix hashes
canonical quote ids after the bind reduction. Counts merge with SUM, the rate is
recomputed, and denominator conservation is checked against the materialised cohort. No
exact metric is silently approximated.

### §12 Source and temporal cuts
- The knowledge cutoff is applied to `recorded_at` for iterations and binds.
- The source cut is a vector of per-relation content fingerprints (fixture) or Time
  Travel timestamps (Snowflake dialect: `AT(TIMESTAMP => %(source_cut_ts)s)`).
- `execute` refuses a run whose source cut changed since preparation.
- A late-arriving correction changes the cut. It invalidates reuse but never rewrites
  the historical receipt (tested).

### §13 Information cache
The manifest follows the template, and its identity is semantic + source + access. Every
read is re-authorised. Derived reuse sums sufficient statistics. A coarser cache can
never serve a finer grain or a changed cohort. Publication requires a complete,
validated, exact result. The cache CTAS is AST-validated into `cache.*`. A policy change
revokes affected entries.

### §14 Trace and receipts
The common envelope uses OpenTelemetry-width trace and span ids. Events are hash-linked
and `verify_chain(anchor)` detects modification. The receipt records:
- intent and confirmation
- recipe, evidence, source, access and compiler versions
- initial and actual plans, adaptations and rejected alternatives
- attempts with query ids
- validations: passed, failed and unverified (for example, conservation on the fused route)
- estimates against actuals
- cost, as unavailable with a reason
- output digest, exactness and censoring
- caveats

### §15 Presentation
Facts come first, then the narrative, which uses association language. The Vega-Lite
chart has:
- a single axis
- grouped bars
- categorical colours in a fixed entity order
- tooltips showing numerators, denominators and Wilson intervals

Evidence categories take the place of a confidence score.

### §16 Assessment
The gate vector covers semantic admission, access, temporal consistency, structural
validity and result verification. Each metric from the table is computed when
measurable and is otherwise `None` with a reason. Business accuracy uses the independent
oracle on the fixture and is labelled fixture agreement. Recommendations are ranked and
classified by change type. User feedback is classified as data.

### §17 Feedback lifecycle
Each case is a folder containing `assessment.md`, `case.json`, `trace_refs.json`,
`expected_outputs.json`, `proposed_patch.json` and `promotion.json`. Cases move through
proposed, evaluated, approved, active, superseded and rejected. Approved cases are
retrieved as `advisory_only` for the same recipe and scope. Activating executable change
classes raises `PermissionError`.

### §18 Recipe builder
Six questions are derived from the obligations. Answers are checked against the
installed algebra. Unsupported meanings are listed with the recipe's own rejection text.
Eleven adversarial cases are generated with `expected_outcome: null`, pending
business-owner labels. The draft status is `not_certified`, and the builder never runs
SQL.

### §19–22 Research and evaluation
Each referenced mechanism appears only at the scope the spec allows:
- POP: application-level checkpoints
- Predicate Transfer: an exact semijoin/key reduction inside a certified inner-join region
- SQLancer TLP: a projection/filter harness
- VeriEQL: an adapter slot
- Bao: not implemented, because it needs comparable receipts first
- Leis et al.: q-error with zero cases reported separately

The tests act as the validation matrix for semantic routing, rewrite correctness, DAG
recovery, adaptive behaviour (correctness only, not speed claims) and cache isolation.
