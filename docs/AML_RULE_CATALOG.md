# AML typology catalogue

The six patterns this release detects. Each one is a *shape of behaviour*, not a
verdict — see [AML_POLICY_BOUNDARIES.md](AML_POLICY_BOUNDARIES.md).

Source of truth: `src/riskops/aml/typology.py` (definitions and thresholds) and
`src/riskops/aml/detect.py` (the detectors). Typology version `1.0.0`. The
version is stamped on every alert, so a case opened last month can still be read
against the rules that opened it.

> **These are not any institution's rules.** They are implementations of patterns
> described in public AML literature, written for a synthetic dataset by someone
> who has never worked a real alert queue. The thresholds are chosen so the
> detectors behave sensibly on this generator's population. They carry no
> authority, and a real deployment would tune every one of them against real
> traffic and a real review capacity.

---

## How a detector works

All six are deterministic. No model, no LLM, no learned threshold. Each returns
alerts keyed on a **subject** — an account, because that is what an investigator
opens — and each alert carries:

| Field | Purpose |
| --- | --- |
| `typology_id`, `typology_version` | Which rule, at which version |
| `subject_id`, `subject_customer_id` | Account and customer |
| `window_start`, `window_end` | The period the pattern was observed over |
| `transfer_ids` | **Every transfer the claim rests on.** Must all resolve. |
| `entity_ids` | Accounts and customers involved |
| `feature_values` | What was measured |
| `threshold_values` | What it was measured against |
| `explanation` | The measured values rendered into a sentence |
| `counter_evidence` | What would argue against this |
| `dedup_key` | Identity for collapsing repeat firings |

Detectors run **daily-window style**: they re-evaluate their lookback on every
day new data arrived, exactly as a nightly batch job would, which is why
duplicates exist at all and why deduplication has real work to do
(~42% of raw firings across three seeds).

Detectors never see the generator's `scenario_id`. `AmlContext.blind()` strips
it, and building a context with it present raises.

---

## T01 · Structuring

**`AML_T01_STRUCTURING`** · severity `high`

> Were several transfers sized to stay under a threshold rather than sized by
> need?

**Fires when** an account sends **≥ 3** transfers priced between **7,000 and
10,000 USD** within a **72-hour** window.

| Threshold | Value |
| --- | --- |
| `threshold_usd` | 10,000 |
| `band_low_usd` | 7,000 |
| `window_hours` | 72 |
| `min_transfers` | 3 |

The window is walked over the *banded subset only*. Three banded transfers among
two hundred ordinary ones is not the pattern.

**Evidence:** transfer count, total, window length, beneficiary count, largest
single transfer.

**What would argue against it**
- A payroll, rent or instalment schedule produces similar amounts on a cadence.
- Per-transfer limits set by the customer's own bank or the channel cap amounts
  with no intent to avoid anything.
- Check the months before the window. A long-standing habit is weaker evidence
  than a new one.

**Measured recall** 0.818 ± 0.053

---

## T02 · Rapid movement

**`AML_T02_RAPID_MOVEMENT`** · severity `high`

> Did this account hold the money, or only pass it on?

**Fires when** an inbound transfer of **≥ 5,000 USD** is followed within
**24 hours** by an outbound transfer of **80–130%** of its value.

| Threshold | Value |
| --- | --- |
| `max_hold_minutes` | 1,440 |
| `min_passthrough_pct` | 80 |
| `min_amount_usd` | 5,000 |

Matched against the **largest single onward leg**, not the sum of everything
leaving. A sum over a busy account reaches 80% by accident, and the pattern being
described is one amount arriving and substantially the same amount leaving.

Above 100% the account paid out more than arrived, so it was funded from
elsewhere. The explanation says so explicitly rather than calling it a
pass-through — a reviewer reading "retained 0 USD" would otherwise form the wrong
picture of where the balance came from.

**What would argue against it**
- Treasury sweeps, supplier settlement and payroll runs are fast by design.
- A named, consistent counterparty on both legs is weaker evidence than a new one.
- Check whether the declared business makes same-day forwarding normal.

**Measured recall** 0.849 ± 0.000

---

## T03 · Funnel account

**`AML_T03_FUNNEL_ACCOUNT`** · severity `high`

> Why are these particular senders all paying the same account?

**Fires when** **≥ 8** distinct sending accounts pay one account **≥ 20,000 USD**
in total within **14 days**.

| Threshold | Value |
| --- | --- |
| `min_senders` | 8 |
| `window_days` | 14 |
| `min_total_usd` | 20,000 |

"Unrelated" is the load-bearing word. Many-to-one is ordinary; it is the
*absence of any connection between the senders* that makes the shape worth
looking at. The detector counts senders sharing **no customer, no beneficial
owner and no device** with each other, and reports that count separately — so an
investigator has something falsifiable to go and disprove.

**What would argue against it**
- Merchants, marketplaces, schools and landlords are legitimately many-to-one.
- A collection account for a real declared business explains the shape entirely.
- Remittance corridors concentrate by nature.

**Measured recall** 0.899 ± 0.046

---

## T04 · Circular flow

**`AML_T04_CIRCULAR_FLOW`** · severity `critical`

> Did this money travel, or only appear to?

**Fires when** a chain of **3–5** transfers returns **≥ 70%** of an initial
amount (≥ 5,000 USD) to the originating account within **7 days**.

| Threshold | Value |
| --- | --- |
| `min_hops` | 3 |
| `max_hops` | 5 |
| `min_return_pct` | 70 |
| `max_elapsed_hours` | 168 |

Implemented as a bounded depth-first walk rather than with a graph library. The
depth cap is five, so the search space is small, and keeping it explicit means
the evidence can name **every leg it walked** — the alert is a statement about
these specific transfers, not about a component of a graph.

The three-hop floor is what separates this from a refund, which produces a
two-leg cycle with an entirely innocent cause.

**What would argue against it**
- Intra-group treasury movement between accounts of one owner looks circular and
  is routine.
- A returned or reversed payment produces a short cycle. Check whether any leg is
  a refund.
- FX round-tripping to obtain a better rate is legal in most corridors.

**Measured recall** 0.828 ± 0.093

---

## T05 · Profile / purpose mismatch

**`AML_T05_PROFILE_MISMATCH`** · severity `medium`

> Does this account behave like what the customer said it was for?

**Fires when** an account moves **≥ 4×** its declared monthly expectation
(and ≥ 15,000 USD) within **30 days**.

| Threshold | Value |
| --- | --- |
| `min_ratio` | 4.0 |
| `window_days` | 30 |
| `min_actual_usd` | 15,000 |

The value floor matters: 20× a tiny declared expectation is arithmetic, not a
finding. The detector also counts transfers declaring a purpose outside the
customer's declared profile, and names them.

Severity is `medium` rather than `high` on purpose. A profile mismatch is very
often a stale profile, and treating a data-freshness problem as a risk signal at
high severity would flood the queue with the institution's own record-keeping.

**What would argue against it**
- A business that genuinely grew will breach its onboarding estimate. Check
  whether the rise is sustained or a spike.
- One-off events — a property sale, an inheritance, a funding round — explain a
  single large deviation.
- A stale profile is a data problem, not a customer problem.

**Measured recall** 0.889 ± 0.046

---

## T06 · Missing payment information

**`AML_T06_MISSING_INFORMATION`** · severity `medium`

> Can we say who is on both ends of this money, and why it moved?

**Fires when** **≥ 3** transfers totalling **≥ 5,000 USD** carry missing or
partial beneficiary information, or no declared purpose.

| Threshold | Value |
| --- | --- |
| `min_affected` | 3 |
| `min_affected_usd` | 5,000 |

Also reports the customer's beneficial-ownership status: `verified`,
`unverified`, `not_required` (individuals) or `missing` (a business with no owner
recorded — itself a gap).

This typology exists to produce **information requests, not escalations**. Its
counter-evidence says so first, because missing data is overwhelmingly an
operational defect at the sending institution rather than concealment.

**What would argue against it**
- Missing data is usually a defect at the sending institution.
- Some corridors and channels legitimately carry less structured data.
- Check whether the same fields are missing across all of that channel's traffic.
  A systemic gap says nothing about this customer.

**Measured recall** 0.949 ± 0.046

---

## Why recall is not 1.000, and why that is the point

The first version of this generator built every scenario squarely inside its
detector's thresholds and scored **100% recall on all six typologies**. That
number measured nothing: generator and detector were written against the same
constants, so a miss was impossible by construction, and a detector that fired on
everything would have scored identically.

Scenarios are now injected at three difficulties:

| Difficulty | Share | Built | Measured recall | Reading |
| --- | --- | --- | --- | --- |
| `clear` | 55% | Comfortably inside the thresholds | **1.000 ± 0.000** | Should be high |
| `borderline` | 30% | Just inside | **0.952 ± 0.027** | What the thresholds actually buy |
| `below_threshold` | 15% | Deliberately outside | **0.162 ± 0.054** | **Should be LOW** — finding these would mean firing on noise |

A high number in the last row would be a bad result, not a good one.

---

## From alerts to a queue

| Stage | Rule |
| --- | --- |
| Deduplicate | Same typology + same subject within **7 days** → one alert. The survivor is the one covering the most transfers; the count it absorbed is kept. |
| Aggregate | Alerts on one subject within **30 days** → one case. The merge reason is written on the case. |
| Prioritise | Eight weighted factors, every contribution stored. |
| Cut | At the day's review capacity. Everything below the line is **backlog**, not cleared. |

Measured across three seeds: 42.3% duplicate reduction, 1.41 alerts per case.

### The eight priority factors

| Factor | Weight | Reads |
| --- | --- | --- |
| Signal strength | 0.20 | Highest severity and alert count |
| Corroboration | 0.18 | Independent typologies agreeing. One typology earns **nothing** here. |
| Amount | 0.15 | Total USD, on a **log scale** between 1,000 and 1,000,000 |
| Velocity | 0.12 | Transfers per day over the case window |
| Network breadth | 0.12 | Counterparty accounts and countries |
| Information gaps | 0.10 | Incomplete beneficiary data and blank purposes |
| Customer history | 0.08 | Onboarding risk level; young accounts have less history to judge against |
| Waiting time | 0.05 | Queue fairness — an old case must eventually rise |

Amount is logarithmic because case totals span three orders of magnitude here
(≈5,000 to 7,200,000 USD). A linear ceiling high enough for the largest case
scored the median case at 0.06 and made the factor inert; low enough for the
median, every large case saturated.

Band cuts (0.45 / 0.36 / 0.25) were calibrated against seed 20260815 at 40,000
transfers, where the score distribution runs median 0.211, p90 0.373, p99 0.456,
maximum 0.521. **They are not universal constants.**
