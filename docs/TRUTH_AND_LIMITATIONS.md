# Truth and limitations

A portfolio project is easy to oversell. This document exists so that anyone reading the code, the
dashboard or the evaluation report knows exactly which claims are supported and which are not.

If anything elsewhere in this repository contradicts this file, **this file is correct** and the
other place is a bug.

---

## 1. What is real

These are properties of the code, and they hold whatever data is fed in:

- **The payment state machine.** Eight states, eleven transitions, an explicit table. An event that
  is illegal for the state it arrives in is quarantined with a reason and never applied to a ledger.
  Verified by replaying all 6,000 transactions onto their stored state (100% agreement).
- **The money arithmetic.** Amounts are integer minor units. `Money` refuses floats at construction.
  FX conversion is decimal with an explicit rounding mode, and the rounding remainder is recorded
  rather than absorbed. Currency exponents are data, so USD, JPY (0 decimals) and BHD (3) are all
  handled without a special case.
- **The reconciliation.** Five money invariants, each computed exactly, each producing a break row
  with the expected value, the observed value and the gap in basis points. Independently recomputed
  by the evaluation harness with 100% agreement.
- **The rule engine.** 20 deterministic rules. Same input, same signals, every time.
- **The decision policy.** A pure function of signals and score.
- **The guardrails.** The input gate, the output gate, the citation check, the authority check and
  the closed action vocabulary all run in code and are covered by tests.
- **The audit chain.** Real SHA-256 forward chaining, verified over the whole log.
- **The evaluation harness.** Every number quoted anywhere is produced by it, from real runs, and a
  test fails if the README drifts.

## 2. What is synthetic

**Everything that looks like data about the world.**

- Every transaction, payment event, merchant, wallet, device, FX quote and country.
- Every risk label. The ground truth is what the generator decided when it created the row.
- Every human decision, handling time, reason code and appeal in the seeded history.
- The AI briefs, under the default provider.

No real payment network, bank, wallet provider, merchant, card scheme or customer is involved,
represented or approximated from real data.

## 3. What is simulated, and what that costs the numbers

### The population is deliberately enriched

About **22%** of transactions are actionable. A real cross-border corridor sees a small fraction of
a percent. Without enrichment a 6,000-row demo would contain a handful of cases and no queue worth
looking at.

**Consequence:** precision, recall, false-positive rate and review rate all move with the base rate.
The reported figures are **not comparable to production**, and no claim is made that they would
survive contact with a real base rate. The one figure that transfers best is *relative* — which
scenarios were caught and which were missed.

### Detection is measured against labels the generator produced

The rules did not see the labels, and the generator does not know the rules. But both were written
by the same person from the same understanding of how cross-border payments fail.

**Consequence:** the metrics show that the rules find the patterns that were planted. They do not
show that those patterns match real fraud. A real evaluation needs data the system's author did not
create.

### The default LLM provider is a deterministic mock

It reads the case packet and assembles a brief from it in plain Python. It is not a language model.

**Consequence:** "100% citation resolution" and "0% ungrounded claims" measure *the mock*, which is
grounded by construction — they are not a measurement of any language model. The output gate's
actual ability to catch bad output is measured separately, by a purpose-built adversarial suite of
ten scripted responses, which is the only honest way to test a gate that a well-behaved provider
never triggers.

Also note the mock's suggestion mapping (which rule suggests which action) and the generator's
`expected_action` were both derived from the same domain reasoning. Their agreement measures
internal consistency, not model skill.

### Human decisions are simulated

A seeded routine resolves cases using the ground-truth label with a fixed **12% error rate**, skewed
toward over-blocking because that is the cheap mistake under time pressure.

**Consequence:** every figure involving human behaviour — handling time, SLA breach rate, AI-human
agreement, appeal volume, false-positive recovery — is a property of those parameters. Change the
error rate and every one of them moves. **They describe a simulation, not a team.**

### Variance is now measured, and it is small

Earlier versions of this file quoted single-run figures. `python -m riskops eval --seeds 5`
regenerates the whole world under five independent seeds: recall **97.01% ± 0.42%**, precision
**80.06% ± 1.49%**, review rate **27.08% ± 0.42%**.

Two things worth stating plainly. The seed this repository ships with is the **worst of the five**
on both recall and precision, so nothing here was seed-shopped. And a standard deviation this
small is a property of a generator that draws every world from the same scenario mix — it is
*not* evidence that the system would be this stable on real traffic, where the population shifts
week to week.

### Latency is not meaningful

P95 brief latency is measured against the mock and is effectively zero. It says nothing about a real
model call, and nothing about behaviour under load.

---

## 4. Known weaknesses in the design

Stated here rather than discovered by a reviewer:

1. **Linkage risk is modelled per transaction, and it should not be.** A shared payout account is a
   fact about a *merchant*, true of every payment it ever takes. `R701_SHARED_PAYOUT_ACCOUNT` fires
   on each transaction, which means a ring merchant's whole volume enters the queue rather than one
   merchant-level case. It is mitigated (medium severity, amplifier-first, ring escalation only when
   another member produced a high-severity signal) but not solved. The right design is a separate
   entity-level case type.
2. **The first leg of an impossible-travel pair is undetectable**, because the evidence does not
   exist until the second payment arrives. Per-transaction recall on that scenario is therefore
   well below 100% while per-episode detection is much higher. The evaluation reports the
   pessimistic per-transaction figure.
3. **The geography rule is deliberately weak** (low severity), because at medium it routed
   essentially every legitimate traveller to a human. That is the correct trade-off for this
   population and it would need re-tuning for any other.
4. **The model adds little over the rules on this data, and this is now measured rather than
   asserted.** Against the honest baseline it is worth +0.0 pp recall and +2.52 pp precision;
   blinded to the three features that are the rule engine's own output it manages only 68.53%
   recall at 55.26% precision. It earns its place by suppressing single-low-signal noise, not by
   finding anything the rules missed. Anyone quoting the naive "delete the model" comparison
   (+24.56 pp recall) would be quoting the blend arithmetic, not the model.
5. **Incremental refresh is not implemented.** Every run rebuilds the world from the seed, which is
   also why the audit log of that world is rebuilt with it. A real system never truncates an audit
   log; this one does, because the log is a property of a generated dataset rather than a record of
   real events.
6. **The hash chain is not anchored.** It makes a partial edit detectable. Someone who can rewrite
   the whole table can recompute every link. Real non-repudiation needs the head signed or
   published somewhere the editor does not control.
7. **The injection gate is pattern-based.** It scores 100% against a purpose-built set of twelve
   payloads with zero false alarms on six benign controls, and a novel phrasing may still get
   through. This is why the *structural* defences matter more: there is no field in which the
   copilot can commit an action, and the audit log refuses AI actors regardless of what any text
   said.
8. **No adversarial testing against a real model.** The output gate has only been tested against
   scripted responses, never against an actual jailbreak of an actual model.
9. **The follow-up copilot recognises a fixed set of intents.** Under the mock provider it answers
   ten kinds of question and declines everything else by name. That makes the *decline* behaviour
   honest and measurable, and it means the breadth of what it can answer is a property of a
   keyword table, not of a language model. A real provider on the same packets would answer more
   and would need the same guardrails, which are provider-independent.
10. **The simulated conversations are simulated.** `riskops demo` seeds 93 follow-up turns across
   30 cases from a fixed question list. No analyst asked any of them. They exist so the demo shows
   the refusal and the decline rather than only the happy path.

---

## 5. Claims explicitly NOT made

- ❌ Not connected to any real payment network, bank, wallet, card scheme, merchant or customer.
- ❌ Not validated, reviewed or certified by any regulator, auditor or financial institution.
- ❌ Not an AML, KYC, sanctions-screening or PCI control, and no claim of meeting any regulatory
  requirement anywhere.
- ❌ No real users, no revenue, no adoption, no measured loss reduction, no production accuracy —
  because there is no production deployment.
- ❌ No claim that the AI is accurate at fraud detection. It does not do fraud detection.
- ❌ No claim that these metrics would hold on real data. See the base-rate discussion above.
- ❌ Not "production-ready", not "bank-grade", not "enterprise-grade". It is a portfolio project
  that runs on a laptop.
- ❌ Not affiliated with, endorsed by, or built using the data, branding or trademarks of any
  payment company.

---

## 6. How to check any of this yourself

```bash
python -m riskops demo    # rebuild everything from the seed
python -m riskops eval    # regenerate every quoted number
pytest                    # the invariants above, as tests
python -m riskops audit   # verify the hash chain
```

Every figure in the README and the dashboard traces to `reports/evaluation.json`, which traces to a
command you can run. If a number cannot be traced that way, it should not be there — please open an
issue.
