# CrossBorder RiskOps — Implementation Plan

> **Status:** living document. Written before implementation started, updated as phases landed.
> **All data in this project is synthetic. This is a portfolio product, not a production system,
> and it has no affiliation with any payment company.**

---

## 1. Inventory of the source project (OpsSignal)

`OpsSignal` is the predecessor repository this project upgrades. It was inspected before any code
was written.

| Area | What exists today | Verdict |
| --- | --- | --- |
| **Stack** | Python 3.10+, DuckDB (single-file warehouse), pandas, scikit-learn, Streamlit + Plotly, pytest, ruff, Makefile, Dockerfile | **Keep.** No server needed, real SQL, runs from a clone. |
| **Warehouse layering** | 4 DuckDB schemas: `raw` (append-only landing) to `staging` (typed/normalised) to `marts` (metric tables) to `audit` | **Keep the shape**, re-cut for payments: `raw` to `core` to `risk` to `marts` to `audit`. |
| **Taxonomy as code** | `taxonomy.py`: 6 dimensions, 51 values, each with definition / example / boundary case / aliases; docs generated from it; pipeline refuses out-of-taxonomy values | **Keep the pattern.** Re-authored for payments: reason codes, decision actions, case states, payment states, signal families. |
| **Validation gate** | 19 severity-tiered checks between staging and marts; `error` aborts and rolls back, `warning` is recorded and surfaced | **Keep and extend** with payment-specific invariants (ledger balance, state-machine legality, FX arithmetic). |
| **Human-in-the-loop review** | `review/queue.py`: append-only `audit.review_events`, AI prediction frozen onto the event, human decision is sticky, dashboard reads `effective_category` | **Keep the contract**, extend to a 4-role case workflow with reason codes, appeals and SLA. |
| **AI layer** | Offline scikit-learn text classifier with a confidence threshold and an abstain route | **Refactor.** The classifier idea survives as the *statistical* signal; the *AI copilot* becomes a separate, bounded evidence/explanation layer with its own guardrails and provider abstraction. |
| **Config** | `get_settings()` re-reads env on every call; every path and knob in one frozen dataclass | **Keep verbatim in spirit.** |
| **Dashboard** | Streamlit multipage, metric definitions computed once in SQL and only filtered in the UI | **Keep the rule.** New pages for payments ops. |
| **Money handling** | Not applicable — ticket counts and durations only | **New work.** Integer minor units end to end; floats banned from money paths. |
| **Agent safety** | None | **New work.** See below. |

### Read-only reference: Volc Agent Launchpad

A separate submission pack in the same workspace (TypeScript). It was **read, never modified, never
copied**. Three design ideas were re-implemented independently in Python here, because the languages
and the domains differ:

1. **Forward hash-chained audit log** — each record hashes the previous record's digest, so editing
   record *N* invalidates *N* and every record after it. Also inherited: the honest three-state
   verification result (`verified` / `broken` / `unverifiable`) instead of a boolean, and the
   explicit statement of what the chain does *not* prove.
2. **Two-layer policy gate** — a deterministic pattern layer runs first and short-circuits, so an
   obvious attack never costs a model call; an optional semantic layer only sees what survives.
   Degradation is recorded on the decision rather than hidden.
3. **Tool output is untrusted input** — content the agent *reads* can carry instructions the
   operator never wrote. Here that content is merchant notes, customer appeal text and device
   metadata, all of which reach the copilot.

---

## 2. Product goals and non-goals

### Goals

Build a **cross-border payment risk operations workbench**: the internal tool a payment company's
risk and operations teams would live in all day. It must show a defensible answer to the question
*"what should AI do, and what must it never do, in a regulated money-movement workflow?"*

1. A synthetic but **logically strict** cross-border payment lifecycle — states, money, FX, fees,
   refunds, chargebacks, reconciliation — that can be tested rather than hand-waved.
2. **Deterministic rules plus an interpretable model** produce risk signals. Never the LLM.
3. An **AI Risk Investigation Copilot** that organises evidence, explains signals, names missing
   information, flags conflicts and *recommends* — with citations, a confidence, and the right to
   abstain.
4. A **human decision layer** with reason codes, four roles, appeals and false-positive recovery.
5. A **tamper-evident audit trail** covering AI suggestions, human decisions and their disagreements.
6. A **reproducible evaluation harness** whose numbers are the only numbers the README quotes.

### Non-goals

- Not a real payment processor, not connected to any network, bank, wallet or merchant.
- Not an AML/KYC compliance system, and not validated by any regulator or financial institution.
- Not an autonomous agent: the AI holds no authority to release, block, freeze or refund.
- No microservices, no queues, no blockchain, no Kubernetes. A reviewer must be able to `pip install`
  and run it.

---

## 3. User roles

| Role | Primary job | What they can do | What they cannot do |
| --- | --- | --- | --- |
| **Risk Analyst** | Work the case queue | Open cases, request the AI brief, decide `release` / `hold` / `request_information` / `escalate` with a reason code | Close an appeal; change policy thresholds |
| **Payment Operations** | Keep the money correct | Investigate reconciliation breaks, duplicate captures, FX and fee mismatches; trigger `request_information` | Decide fraud outcomes on high-severity cases |
| **Customer Support** | Handle appeals | File an appeal for a user or merchant, attach evidence, see a redacted case view | See raw PII; decide the case |
| **Admin / Auditor** | Prove what happened | Read the full audit trail, verify the hash chain, export, compare AI against human decisions | Silently edit a past decision (append-only) |

---

## 4. Core user flows

```
payment event
  -> state machine applies the transition (legal transitions only)
  -> deterministic rules fire signals (R-codes, severity, evidence pointers)
  -> interpretable model adds a calibrated score with per-feature contributions
  -> decision policy bands the case: auto_release | manual_review | request_information | auto_hold
  -> [manual_review] a case is created and queued with SLA and priority
  -> the analyst requests the AI brief
        -> untrusted case text passes the injection gate
        -> the copilot returns a structured brief: facts, signals explained, conflicts,
           missing information, recommendation with confidence, citations
        -> output guardrails: citations must resolve, no authority verbs, no PII, or the brief is
           rejected or downgraded to abstain
  -> the analyst decides with a reason code (AI agreement and disagreement both recorded)
  -> the decision is appended to the hash-chained audit log
  -> a customer or merchant may appeal -> the case reopens -> false-positive recovery is measured
```

---

## 5. Data design

Money is **never a float**. Every amount is an integer in minor units plus an ISO-4217 currency and
its exponent. FX conversion is decimal, with an explicit rounding mode, and the rounding remainder is
recorded rather than discarded.

| Schema | Tables | Purpose |
| --- | --- | --- |
| `raw` | `payment_events` | Append-only landing zone, exactly as the simulated network emitted it |
| `core` | `transactions`, `payment_events`, `merchants`, `wallets`, `devices`, `accounts`, `fx_quotes`, `settlements`, `refunds`, `chargebacks`, `reconciliation_breaks` | The canonical, typed, validated payment model |
| `risk` | `signals`, `case_features`, `model_scores`, `cases`, `case_signals` | Rule output, model output, and the cases they produce |
| `audit` | `audit_log` (hash-chained), `ai_invocations`, `decisions`, `appeals` | Who did what, when, with which model, prompt and rules version |
| `marts` | KPI tables | Every dashboard metric defined exactly once |

---

## 6. System architecture

```
              +------------------------------------------------------+
              |  Synthetic cross-border payment generator (seeded)    |
              |  16 scenario families - fixed seed - reproducible     |
              +----------------------------+-------------------------+
                                           | raw.payment_events
              +----------------------------v-------------------------+
              |  Pipeline: normalise -> state machine -> ledger ->    |
              |  validation gate (severity-tiered, rollback on error) |
              +----------------------------+-------------------------+
                                           | core.*
        +----------------------------------v------------------------+
        |  RISK ENGINE   (no LLM anywhere in this box)               |
        |   deterministic rules  --+                                 |
        |   interpretable model  --+-->  decision policy  --> cases  |
        +----------------------------------+------------------------+
                                           | risk.cases
        +----------------------------------v------------------------+
        |  AI COPILOT  (advisory only - no authority)                |
        |   injection gate -> provider (mock | real) -> schema       |
        |   -> output guardrails -> CaseBrief with citations         |
        +----------------------------------+------------------------+
                                           |
        +----------------------------------v------------------------+
        |  HUMAN DECISION LAYER  ->  hash-chained audit log          |
        +-----------------------------------------------------------+
```

## 7. AI responsibilities against non-AI responsibilities

| Task | Owner | Why |
| --- | --- | --- |
| Detect a rule violation | Deterministic rules | Must be reproducible and auditable |
| Score residual risk | Interpretable model (logistic regression, per-feature contributions) | Calibrated, explainable, testable |
| Decide the case band | Decision policy (pure function of signals and score) | A decision must not depend on a sampled token |
| Compute money, FX, fees | Integer and decimal arithmetic | Never delegate arithmetic to a language model |
| Summarise the case | AI copilot | Genuine language work |
| Explain *why* a signal fired, in plain language | AI copilot (from rule metadata) | Genuine language work |
| Find conflicts between evidence sources | AI copilot | Genuine reasoning over text and fields |
| Name the missing information that would resolve the case | AI copilot | The highest-value AI task here |
| Recommend an action | AI copilot, **advisory, with confidence and an abstain option** | Advice, not authority |
| Release / hold / refund / block | **Human, or the deterministic policy** | Regulated money movement; irreversible; accountable |

## 8. Evaluation plan

Every number in the README comes from `python -m riskops eval`, which writes
`reports/evaluation.json`, which generates `docs/EVALUATION_REPORT.md`. Nothing is typed by hand.

Suites:
1. **Risk detection** — recall, precision, false-positive rate, manual review rate, per-scenario recall.
2. **AI quality** — agreement with the human label, evidence completeness, ungrounded-claim rate,
   citation resolution rate, abstention correctness on under-specified cases.
3. **Agent safety** — prompt-injection defence, authority-escalation refusal, sensitive-data leakage.
4. **Determinism** — money and FX arithmetic, state-machine legality, ledger balance, generator
   reproducibility.

## 9. Phases

| Phase | Content | Exit test |
| --- | --- | --- |
| P0 | config, money, taxonomy, state machine, DB schema | `pytest tests/test_money.py tests/test_state_machine.py` |
| P1 | synthetic generator, scenario families, seeded | `pytest tests/test_generator.py` |
| P2 | pipeline, validation gate, core ledger | `pytest tests/test_pipeline.py` |
| P3 | rules, model, decision policy, case creation | `pytest tests/test_risk.py` |
| P4 | AI provider, guardrails, copilot | `pytest tests/test_ai.py tests/test_guardrails.py` |
| P5 | review workflow, appeals, hash-chained audit | `pytest tests/test_review.py tests/test_audit.py` |
| P6 | marts and Streamlit UI | `pytest tests/test_marts.py` plus a browser check |
| P7 | evaluation harness and generated report | `python -m riskops eval` |
| P8 | docs, README, demo script, screenshots | full `pytest` and `ruff` |

## 10. Risks and limitations

- Synthetic data is generated from the same assumptions the rules encode, so detection metrics are
  an **upper bound on a simulated population**, not a production estimate. This is stated in the
  README, the evaluation report and the dashboard itself.
- The default LLM provider is a deterministic mock. Mock results measure the *guardrail and workflow*
  layer, not a real model's language quality. Real-provider runs are opt-in and separately labelled.
- The hash chain makes partial edits detectable, not impossible. Anchoring the head out-of-band is
  future work.
- No PCI, AML or KYC compliance claim of any kind is made.

## 11. Definition of done

- [x] A fresh clone runs from the README with no API key.
- [x] Every core page renders with data.
- [x] `python -m riskops demo` is reproducible from a fixed seed.
- [x] Money, FX and state-machine logic are covered by tests.
- [x] The end-to-end case flow works: signal, case, AI brief, human decision, audit, appeal.
- [x] The AI cannot emit a final decision — enforced in code and asserted in tests.
- [x] `python -m riskops eval` runs and every README number matches its output.
- [x] No secrets, no personal data, no machine-specific paths.
- [x] No "production-ready" or "bank-grade" claims anywhere.
