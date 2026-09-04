# Synthetic data design

How the cross-border remittance dataset is built, and why it is constructed the
way it is. Source of truth: `src/riskops/aml/world.py`.

> **Everything here is invented.** No real customer, account, institution,
> corridor volume or transfer is represented, and no field is derived from one.
> Names come from a fixed word list. Country codes are used as labels. The
> behaviour is whatever these functions say it is.

---

## The two rules the generator has to obey

Everything else follows from these. Break either one and the evaluation numbers
stop measuring anything.

### 1. Injected patterns must be reachable from behaviour alone

Each typology is built out of ordinary transfers whose *shape* matches the
typology. The `scenario_id` written next to them is provenance for scoring only,
and the detectors never see it — `AmlContext.blind()` drops the column, and
building a context with it present raises.

A generator that also planted a flag would be scoring the flag.

### 2. Normal behaviour must be able to produce the same shapes

The background population deliberately contains:

| Confounder | Share | Which typology it confuses |
| --- | --- | --- |
| Fast-forwarding accounts (money in, money out within hours) | ~2% of accounts | Rapid movement |
| Collection accounts (many senders, one recipient) | ~2% of accounts | Funnel |
| Businesses whose volume outgrew their onboarding estimate | continuous | Profile mismatch |
| `agent_cash_in` channel carrying incomplete beneficiary data | ~42% of that channel | Missing information |
| Amounts distributed log-normally, so some land in the structuring band naturally | continuous | Structuring |

Without these, every detector would score perfectly and the precision figure
would be a measurement of nothing. **The false positives in the evaluation report
come from here, on purpose.**

This is why alert precision is 22% rather than 90%. A higher number would mean
the background was too easy.

---

## Entities

| Entity | Count at 40k transfers | Fields |
| --- | --- | --- |
| `Customer` | 3,333 | id, name, type (individual/business), home country, industry, onboarding risk level, onboarded date, expected monthly USD, profile last reviewed |
| `Account` | 4,418 | id, customer, country, currency, opened, **account age in days**, type |
| `BeneficialOwner` | 1,438 | id, customer, hashed reference, ownership %, **verification status**, recorded |
| `Device` | ~1,100 | id, OS family, first seen, shared-terminal flag |
| `Transfer` | 40,000 | see below |
| `Scenario` | 198 | id, typology, subject, transfer ids, **difficulty**, construction note |

Customers are 28% business. Businesses get 1–3 beneficial owners, 20% of which
are `unverified`. Individuals get `not_required` — a business with none recorded
is itself a gap the missing-information typology reports.

**Onboarding risk level is deliberately only weakly correlated with behaviour.**
It is an opinion recorded at onboarding, not an outcome. A detector leaning on it
would be reading the generator's opinion rather than the transfers.

---

## Transfer schema

Every field the brief requires, plus provenance:

| Field | Notes |
| --- | --- |
| `transaction_id` | `TRF_00000001` |
| `timestamp` | Within the configured history window (90 days) |
| `payer_account`, `beneficiary_account` | |
| `payer_customer_id`, `beneficiary_customer_id` | |
| `amount_minor`, `currency` | **Integer minor units.** Never a float. |
| `normalized_amount_usd_minor` | **Integer USD cents.** Every comparison runs on this. |
| `origin_country`, `destination_country` | |
| `declared_purpose` | Nine values, or blank |
| `channel` | `wallet_transfer`, `bank_wire`, `card_funded`, `agent_cash_in` |
| `merchant_category` | Eleven values, or `none` |
| `device_id` | |
| `account_age_days` | Payer's, at `as_of` |
| `customer_risk_level` | Payer's onboarding level |
| `beneficiary_information_status` | `complete` / `partial` / `missing` |
| `is_cross_border` | Derived |
| `scenario_id`, `scenario_role` | **Provenance. Stripped before detection.** |

### Money

Amounts run through the same `money.py` exponent table as the payment side, so
there is exactly one currency-exponent map in the repository. Sixteen currencies,
including **JPY (0 decimal places)** and **BHD (3)** — kept deliberately, so any
code that assumed two decimal places breaks here rather than somewhere it
matters.

Every threshold, total and priority factor compares
`normalized_amount_usd_minor`, never the presentment amount. Comparing a JPY
amount to a USD one without normalising is how a 1,000,000 JPY transfer gets
treated as a hundred times larger than a 10,000 USD one.

The USD rates are fixed, invented constants used only to put transfers on one
comparable scale. They are not market rates and are not claimed to be.

---

## Corridors

Eighteen country codes, restricted to the currencies `money.py` already knows.

`SG HK GB US AU JP DE FR NL MY ID PH VN TH KR CA CN BH`

There is a `HIGHER_ATTENTION_COUNTRIES` list used to give corridor concentration
something to concentrate on. **It is a property of this generated dataset and
says nothing about any country's actual risk.** The operations page excludes
corridors with fewer than 30 transfers from its alert-rate chart for exactly this
reason — a 100% alert rate over two transfers is noise, and displaying it would
invite a conclusion about a country.

---

## Injection at three difficulties

The most important design decision in this file, and the one that took two
attempts.

**The first version scored 100% recall on all six typologies.** Every scenario
was built squarely inside its detector's thresholds, so a miss was impossible by
construction. The number measured nothing.

Scenarios are now spread across the threshold rather than sitting on it:

| Difficulty | Weight | Meaning |
| --- | --- | --- |
| `clear` | 55 | Comfortably inside the thresholds. Should be found. |
| `borderline` | 30 | Just inside. Finding these is what a threshold is worth. |
| `below_threshold` | 15 | Deliberately outside — fewer legs, slower, weaker. **Should be missed.** |

Concretely, per typology:

| Typology | `clear` | `borderline` | `below_threshold` |
| --- | --- | --- | --- |
| Structuring | 5–7 legs in 6–40h | 3–4 legs in 50–68h | 2 legs, or 3 spread over 110h |
| Rapid movement | 15–300 min, 90–98% | 1000–1400 min, 81–86% | >26h hold, or 55–74% forwarded |
| Funnel | 12–18 senders | 8–10 senders | 4–7 senders |
| Circular | 3–4 hops, 94–98% back | 3 hops, 90–92% back | 2 hops, or 78–86% back |
| Profile mismatch | 8–14× expectation | 4.2–5.5× | 1.8–3.4× |
| Missing information | 6–9 affected | 3–4 affected | 2 affected, or tiny amounts |

Each scenario records a `construction` note saying how it was built — written for
a reader auditing the evaluation, and the answer to "how do you know your recall
figure is not measuring your own hint?"

**A high recall on `below_threshold` would be a bad result.** It currently sits
at 0.162 ± 0.054.

---

## Sizing

| | Value |
| --- | --- |
| Generator default | **100,000 transfers** (`RISKOPS_AML_N_TRANSFERS`) |
| Committed demo warehouse | **40,000 transfers** |
| Evaluation | 40,000 × 3 seeds |
| Build time | ~21s at 40k, ~49s at 100k |

The committed pack is smaller so `data/riskops.duckdb` stays around 32 MB and the
repository remains clonable. Nothing about the *shape* differs between them —
only how many.

Customers scale at roughly one per twelve transfers. Too few customers and every
account looks like a funnel simply because there is nowhere else for the money to
go, which would inflate both the alert count and the apparent recall. This was a
real bug during development: at one customer per forty transfers, the funnel and
profile detectors fired on almost everything.

---

## Determinism

Everything is seeded. The same seed produces the same warehouse: the same
transfers, the same alert ids, the same cases, the same queue order.

Alert ids are derived by **SHA-256 of the dedup key**, not by a counter and not
by Python's `hash()` — string hashing is randomised per process, so `hash()`
produced different ids on every run over identical data. That was a real bug,
caught because it would have broken any link to an alert across a restart.

Case ids are SHA-256 of `subject | window start date`.

---

## What this dataset cannot tell you

- **Nothing about real laundering.** The typologies are drawn from publicly
  described patterns and implemented by someone who has never worked a real alert
  queue. Whether real laundering looks like this is an open question.
- **Nothing about real false-positive rates.** The base rate here is enriched by
  orders of magnitude. Roughly 4% of transfers belong to an injected scenario; in
  real traffic the equivalent figure would be a small fraction of a percent.
- **Nothing about real corridors, currencies or countries.** The volume
  distribution is invented.
- **Nothing about real customer behaviour.** The background population is a
  log-normal amount distribution and a set of hand-written confounders, not an
  observed population.

Every number in
[AML_EVALUATION_REPORT.md](AML_EVALUATION_REPORT.md) is a measurement of *this*
generator. It is reproducible, and that is the only property claimed for it.
