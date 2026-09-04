# AML evaluation report

> **Generated file - do not edit by hand.** Every number below is written by
> `python -m riskops aml eval`. Editing it by hand would produce a claim nobody
> can reproduce.
> Generated 2026-09-04T15:14:17 · typology `1.0.0`
> · priority `1.0.0` · world `1.0.0`.

## Read this before any number

- Every figure below was produced by the generator in aml/world.py. No real customer, account, institution or transfer is involved. The population is deliberately enriched with injected typologies, so recall and precision are both far higher than any real monitoring system would see, and neither number transfers to production traffic.
- **An unusual transaction is not a laundered transaction.** Recall here means the
  detectors found a pattern the generator planted. It does not mean anything was
  laundered, and no figure in this document is a detection rate for crime.
- Precision is measured against *injected scenarios*, not against ground truth. An
  alert counted as a false positive may describe genuinely unusual behaviour; it
  means only that it does not overlap something the generator planted.
- These figures describe this generator's population. They do not transfer to any
  real institution, corridor or payment system, and comparing them with a published
  industry figure would be meaningless.

## Dataset

| Setting | Value |
| --- | --- |
| Kind | synthetic, seeded, enriched |
| Seeds | 20260815, 20260816, 20260817 |
| Transfers per seed | 40,000 |
| Python | 3.13.14 on Windows |

## Headline

Mean ± standard deviation across the seeds above.

| Metric | Value | What it means |
| --- | --- | --- |
| Scenario recall | 0.8720 ± 0.0278 | Injected patterns the detectors found, across every difficulty. |
| — on *clear* scenarios | 1.0000 ± 0.0000 | Built comfortably inside the thresholds. Should be high. |
| — on *borderline* scenarios | 0.9522 ± 0.0269 | Built just inside. This is what the thresholds actually buy. |
| — on *below-threshold* scenarios | 0.1623 ± 0.0540 | **Should be LOW.** Built outside the thresholds on purpose; finding them would mean the detectors fire on noise. |
| Alert precision (on injections) | 0.2229 ± 0.0041 | Share of alerts overlapping a planted pattern. Low, and expected to be. |
| **Precision at review capacity** | 0.8697 ± 0.0268 | Of the cases a team of this size could actually open, the share touching a planted pattern. This is the number prioritisation exists to move. |
| Case aggregation rate | 1.4085 ± 0.0336 | Alerts per case. 1.00 would mean aggregation did nothing. |
| Duplicate alert reduction | 0.4232 ± 0.0064 | Share of raw firings that were repeats of a finding already reported. |
| Evidence traceability | 1.0000 ± 0.0000 | Alerts whose every cited transfer resolves to a row that exists. |
| Counter-evidence rate | 1.0000 ± 0.0000 | Alerts carrying what would argue against them. |
| Unsupported-claim rate | 0.0 | Adversarial probes against the disposition boundary that succeeded. |
| Boundary probe failures | 0 | Attempts to record a decision as a non-human, without a reason, or naming an action this system cannot perform. |
| Degradation cases survived | True | The core loop with fields blanked, identifiers removed, and an empty feed. |

### The one comparison worth making

Raw alert precision is 0.2229 ± 0.0041. Precision inside the review capacity is 0.8697 ± 0.0268.

That gap is what investigation priority is for. The alerts are mostly noise; the
ordering is what makes a day of work worth doing. It is also the honest limit of
the claim, because the cases below the capacity line were not cleared - the count
of planted patterns sitting in that backlog is reported below.

## Recall by typology

| Typology | Recall (mean ± sd) |
| --- | --- |
| Structured transfers below a reporting threshold | 0.8182 ± 0.0525 |
| Funds forwarded cross-border shortly after arriving | 0.8485 ± 0.0000 |
| Many unrelated senders converging on one account | 0.8990 ± 0.0463 |
| Funds returning to their origin through intermediaries | 0.8283 ± 0.0926 |
| Activity inconsistent with the declared profile | 0.8889 ± 0.0463 |
| Required payment information absent or unverified | 0.9495 ± 0.0463 |

## One run in detail (seed 20260815)

| | |
| --- | --- |
| Transfers | 40,000 |
| Scenarios injected | 198 |
| Raw alert firings | 1,638 |
| Alerts after deduplication | 957 |
| Duplicates removed | 681 |
| Transfers lost to deduplication | 170 |
| Cases | 693 |
| Multi-typology cases | 120 |
| Review capacity | 242 |
| Backlog | 451 |
| **Planted patterns left unreviewed** | 70 |

The last row is the cost of a capacity limit, stated rather than hidden. Those
cases were not judged low-risk; nobody opened them.

## Boundary probes

35 probes, 0 failures.

Each probe attempts something the product must refuse: recording a disposition as
an AI actor, closing a case with an empty or token reason, escalating without
naming any evidence, or finding a word anywhere in the vocabulary that asserts a
crime, a regulatory filing or an account freeze.

## Degradation

| Case | Survived | Alerts | Cases |
| --- | --- | --- | --- |
| intact | True | 363 | 100 |
| every declared purpose missing | True | 703 | 417 |
| no beneficiary information anywhere | True | 703 | 417 |
| no device identifiers | True | 363 | 100 |
| empty transfer feed | True | 0 | 0 |

Core loop contains no model call: `True`. Detection,
deduplication, aggregation and prioritisation import no provider at all, so an
unreachable language model cannot affect any of them.

## What this evaluation cannot tell you

- Whether these typologies match real laundering behaviour. They are drawn from
  publicly described patterns, implemented against a generator written by the same
  author, and tested against that generator.
- What the false-positive rate would be on real traffic. The base rate here is
  enriched by orders of magnitude.
- Whether an investigator would agree with the priority ordering. No investigator
  has used this.
- Anything at all about a real institution's controls, staffing or effectiveness.

