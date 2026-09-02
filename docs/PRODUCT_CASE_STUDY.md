# Product case study

How CrossBorder RiskOps was scoped, what was decided, what was traded away, and what the numbers
say about whether it worked.

> All data is synthetic. See [TRUTH_AND_LIMITATIONS.md](TRUTH_AND_LIMITATIONS.md).

---

## 1. The problem worth solving

Cross-border payment risk is usually framed as a fraud-detection problem. Working through the
failure modes, it is mostly not:

| What goes wrong | Is it fraud? | Who owns it |
| --- | --- | --- |
| Retry storm captures the same authorisation twice | No — an operations defect that debits a real customer twice | Payment operations |
| Settlement 4% away from the quoted rate | No — a pricing or booking break | Payment operations |
| Capture against an expired FX quote | No — the payer was charged at a price nobody showed them | Payment operations |
| Refund then chargeback on the same payment | No — double credit, the merchant pays twice | Operations, then risk |
| Wallet paying from three devices in an hour | Probably | Risk analyst |
| Impossible travel between consecutive payments | Probably | Risk analyst |
| Six merchants sharing a beneficiary account | Wider than one payment | Investigations |
| Nothing on file to judge with | Not a risk claim at all | Support, then whoever asked |
| Traveller blocked while genuinely abroad | The system was wrong | Support, then risk |

So the product is not a fraud detector. **It is a triage and evidence system for a mixed queue**,
where the first job is working out *which kind of problem this is* and getting it to the right desk
with the evidence already assembled.

That framing produced most of the design decisions below.

---

## 2. Users, and what each of them is actually blocked on

| Role | Blocked on | What the product gives them |
| --- | --- | --- |
| **Risk Analyst** | Nine signals, no story. Which two matter? | Signals ranked by severity with the numbers that justify them, an AI summary, and a named list of what is missing |
| **Payment Operations** | Settlement is off — by how much, and why? | Reconciliation breaks with expected, observed and the gap in basis points, computed exactly |
| **Customer Support** | An angry payer and a redacted case view | An appeal path with evidence types, and visibility into what was decided and why |
| **Admin / Auditor** | "Show me that this decision was not edited afterwards" | An append-only hash-chained log with the rules, model and prompt versions on every entry |

Routing follows from this: a settlement break goes to payment operations, not to a fraud analyst.
`OWNER_BY_FAMILY` in `risk/cases.py` is one dictionary, and getting it right is most of what makes
an ops tool usable rather than a shared inbox.

---

## 3. The core flow

```
payment event
  → state machine (legal transitions only; illegal events quarantined with a reason)
  → reconciliation (5 money invariants, exact arithmetic)
  → 20 deterministic rules → signals with severity and evidence fields
  → logistic regression → residual score with per-feature contributions
  → decision policy (pure function) → auto_release | manual_review | request_information | auto_hold
  → case opened with an owner, a priority and an SLA clock
  → analyst opens it; AI brief assembled (untrusted text screened first)
  → analyst decides with a reason code; AI agreement or disagreement recorded
  → appended to the hash-chained audit log
  → payer or merchant may appeal → case reopens → false-positive recovery measured
```

---

## 4. The decisions, and the trade-offs

### 4.1 The AI recommends; it never decides

**Alternative considered:** let the model auto-resolve high-confidence cases to cut the review rate.

**Rejected.** A decision here moves someone's money and has to be defended in a review with a name
attached. And the realistic failure is not a hallucinating model — it is a well-behaved model whose
recommendation is quietly treated as a decision because the interface made agreeing easy.

**So the design pushes the other way:** the recommendation is labelled advisory everywhere, sits in
the queue directly beside the human's decision, and the gap between them is reported rather than
hidden. Full enforcement in [AI_BOUNDARIES.md](AI_BOUNDARIES.md).

**Cost:** the review rate stays at 26.9% where an auto-resolve policy could push it lower.
That is the trade being made deliberately.

### 4.2 The deterministic policy *may* auto-hold

**Alternative considered:** every hold requires a human.

**Rejected.** A duplicate capture debiting a customer twice should stop at 02:14, not at 09:00.
Waiting is itself a decision with a cost.

**Resolution:** `auto_hold` stops the *payment* and still opens a *case*. The money is safe by
default; the outcome needs a person. That split is why a wrong hold is recoverable, and recovery is
measured: **53.3%** of wrong holds were overturned on appeal.

### 4.3 Money is never a float

**Cost:** more code. `Money` refuses floats, currency mixing is an error, and `float()` appears once
behind a function named `minor_to_major_float` so it can be grepped.

**Why it earned that:** this product's whole premise is comparing an authorised amount to a captured
amount to a settled amount across two currencies and a fee split. `0.1 + 0.2 != 0.3` turns into a
reconciliation break that does not exist and a risk signal that fires on nothing. With exact
arithmetic, "settlement is 433 bps outside tolerance" means exactly one thing — and the evaluation
harness recomputes all 5,409 settlements independently and agrees with the pipeline 100% of the time.

### 4.4 Geography is low severity

**First attempt:** IP country differing from wallet country was medium severity.

**What happened:** ~98% of legitimate travellers were routed to a human. The rule was technically
correct and operationally useless — it would have buried the queue in tourists and taught analysts
to click through the signal.

**Change:** demoted to low. It now carries weight only in combination. Legitimate travellers routed
fell from 98% to **15.7%**, while impossible travel (critical) and jurisdiction conflict (high) still
fire. The lesson: *severity is a queue-budget decision, not a description of the evidence.*

### 4.5 Linkage amplifies; it does not trigger

**First attempt:** a shared payout account fired standalone at high severity.

**What happened:** 494 signals on ~8% of transactions, because it is a fact about the *merchant* and
therefore true of every payment it takes. A legitimate franchise group would have had its entire
volume queued.

**Change:** medium severity, and it fires either as an amplifier on a transaction that already has a
signal, or as a ring escalation when the payout group has three or more merchants and one of them
produced a high-severity signal elsewhere in the batch. The ring scenario is still caught 100% of
the time.

**Still wrong, and documented as such:** linkage risk belongs in a merchant-level case type, not on
every transaction. See limitation 1 in [TRUTH_AND_LIMITATIONS.md](TRUTH_AND_LIMITATIONS.md).

### 4.6 "Not enough information" is a first-class outcome

Cases with no device fingerprint, no merchant category and no payer history route to
`request_information` rather than to a guess. **100%** of those cases in the evaluation were
genuinely actionable, and the reason code `RC_INSUFFICIENT_EVIDENCE` is enforced for that action so
the reason a case is waiting is never ambiguous.

The copilot has the matching behaviour: it abstains (12.45% of briefs) rather than producing a
recommendation it cannot ground.

### 4.7 SLA measures time to first action, not time to close

**First attempt:** breach = not closed by the due date. Result: 99% of open cases breached, because
"awaiting information" cases wait on a merchant for days.

**Change:** two clocks. `first_actioned_at` is when a person acted — that is what the SLA is about.
`resolved_at` is when the case closed. Breach rate went from 99% to a meaningful **3.47%**, and the
number now measures the thing an operations manager can act on.

### 4.8 The benign population deliberately trips rules

`legitimate_traveller` is not actionable but pays from a far country hours after a payment at home.
Without it, a detector that flags everything scores 100% recall and the false-positive rate is
unmeasurable. It is also the source of the appeal and recovery flow — the product's most important
loop only exists because the dataset contains people the system gets wrong.

---

## 5. What the numbers say

Seed `20260815`, 6,000 synthetic transactions, 21.72% actionable.

| Question | Answer |
| --- | --- |
| Does it catch what it should? | Recall **96.47%**; 46 of 1,303 actionable transactions were auto-released |
| What does that cost? | **26.92%** of traffic reviewed; **7.62%** of benign traffic reviewed |
| Is the queue clean enough to trust? | Precision **77.83%** — roughly four in five reviews were justified |
| Which scenarios fail? | `impossible_travel` at 61.9% per transaction, because the first leg of a hop has no evidence until the second arrives; every other actionable scenario is at 100% |
| Does the AI hold its boundary? | **0** decisions by an AI actor, **0** unauthorised recommendations, **0** PII leaks, **100%** of adversarial responses handled |
| Can a wrong hold be recovered? | **53.3%** of wrong holds overturned on appeal |
| Is any of it reproducible? | Same seed, byte-identical data; the README's numbers are asserted against the harness output by a test |

**And the caveat that travels with all of them:** the base rate is enriched roughly a hundredfold,
the labels come from the same generator, and the human decisions are simulated. These figures
describe this system on this synthetic population. Nothing more.

---

## 6. What I would do next

1. **A merchant-level case type**, so linkage risk raises one investigation rather than 400 cases.
2. **A real-provider evaluation**, with the same suites run against an actual model and reported
   side by side with the mock, so the language-quality question gets a real answer.
3. **Threshold tuning as a product surface.** `auto_release_below` and `auto_hold_at_or_above` are
   env vars today. They are a policy decision with a visible cost curve and belong in the UI, with
   the review rate and the leakage rate updating as you move them.
4. **Time-to-decision as the primary metric.** Recall and precision are the model's metrics. What an
   operations lead actually manages is queue latency, and the product should optimise for it
   explicitly.
5. **Feedback into the rules.** Every overturned hold is a labelled example of a rule that was
   wrong. Nothing currently closes that loop.
