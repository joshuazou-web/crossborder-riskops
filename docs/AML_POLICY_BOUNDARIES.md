# AML policy boundaries

What this system says, what it refuses to say, and where each refusal is
enforced. Every claim on this page names the code that holds it up and the test
that would fail if it stopped.

---

## The sentence the whole product is arranged around

> **An unusual transaction is not a laundered transaction.**

A typology match says a pattern is present and worth a person's time. It says
which transfers and which entities to look at. It does not say a crime occurred,
and nothing in this system can.

This is not modesty. A monitoring system that presents a pattern as a conclusion
produces two failures at once: reviewers stop reading the evidence because the
system already decided, and the people on the other end of the alert have no way
to argue with a verdict nobody wrote down the reasoning for.

---

## What this system is not

- Not a real payment, clearing or settlement system.
- Not any institution's internal system. Not affiliated with Tencent, Tenpay,
  WeChat Pay, or any bank, payment company or regulator. No logo, brand asset or
  internal data of any organisation appears anywhere in it.
- Not a system that determines money laundering.
- Not a system that freezes, blocks or restricts any account.
- Not a system that declines or holds any payment.
- Not a system that files a suspicious activity report, or any report, with any
  authority. It has no channel to one.
- Not certified, reviewed or validated by any regulator or compliance function.
- Not tested against real customers, real transactions or real investigators.

It has never run anywhere but a laptop, against **synthetic data it generated
itself** from a fixed seed. Every customer, account, beneficial owner, device and
transfer named anywhere in this system was invented by
`src/riskops/aml/world.py`.

---

## Enforcement, not description

### 1. The vocabulary cannot express a conclusion the system cannot support

The complete disposition list:

| Disposition | Moves case to | What it means |
| --- | --- | --- |
| `close_no_action` | `closed_no_action` | The evidence on file explains the pattern. **Not** a statement that the customer was cleared of anything. |
| `continue_monitoring` | `monitoring` | Not explained, not enough to escalate. Stays in scope. |
| `request_information` | `awaiting_information` | A specific named gap must be filled first. |
| `enhanced_review` | `investigating` | Needs more work than the queue allows. |
| `escalate` | `escalated` | Referred to the responsible team. **Files nothing with anyone and reaches no authority.** |

The complete case-state list: `new`, `queued`, `investigating`,
`awaiting_information`, `monitoring`, `escalated`, `closed_no_action`.

There is no value meaning confirmed laundering, filed, reported, frozen, blocked
or blacklisted. Not disabled in the interface — **absent from the vocabulary**.

*Enforced by:* `investigation.FORBIDDEN_DISPOSITION_WORDS`,
`aggregate.FORBIDDEN_STATES`
*Tested by:* `TestVocabulary` — five tests over every disposition and state,
including that `escalate` explicitly says it reaches no authority and that
`close_no_action` explicitly says it clears nobody.

### 2. Only a person may record a decision

`INVESTIGATOR_ROLES = ("aml_investigator", "risk_ops_lead", "admin_auditor")`

`ai_copilot`, `system`, `model`, `assistant`, `agent`, `llm` and everything else
is refused with an error naming the rule.

The check runs **before the first database write**. A refused disposition does
not create a row, does not move a case, and does not append to the audit log.

*Enforced by:* `investigation.validate()`, called first in
`record_disposition()`
*Tested by:* `TestOnlyAPersonDecides` — including a test that passes a connection
object which raises if touched at all, proving nothing was attempted.

### 3. A decision without a written reason is not a decision

Minimum 30 characters, and `escalate` and `request_information` additionally
require the reason to reference something specific: a transfer id, an account, a
customer, a typology, or a named document type.

`"looks suspicious"`, `"escalating"`, `"ok"` and `"n/a"` are all refused.

The reason is the only part of a case that a second reviewer, a quality sampler,
or the customer themselves can argue with. Without it a disposition is an
unfalsifiable assertion.

*Enforced by:* `MINIMUM_REASON_CHARACTERS`, `_references_evidence()`
*Tested by:* `TestReasonIsMandatory` — nine token reasons refused, vague
escalations refused, specific ones accepted.

### 4. The detection loop contains no model at all

Detection, deduplication, aggregation and prioritisation import no provider, no
LLM client, and no HTTP library. An unreachable language model cannot change a
single alert, case, priority score or queue position.

This is stronger than "the AI is advisory". There is no AI in this path.

*Enforced by:* the import graph of `detect.py`, `aggregate.py`, `priority.py`,
`world.py`
*Tested by:* `test_detection_and_aggregation_import_no_provider`, which walks the
AST rather than grepping the text — the docstrings discuss models at length
precisely because their absence is the point.

### 5. Every alert carries what would argue against it

Each typology declares `counter_evidence_hints`: the ordinary, innocent
explanations a reviewer should actively rule out. The workbench renders them in a
column **beside** the evidence, not below it, because a reviewer under queue
pressure reads top-down and stops early.

Measured at 100% of alerts across three seeds.

*Enforced by:* `Typology.counter_evidence_hints`, required to have at least two
entries
*Tested by:* `test_every_typology_offers_counter_evidence`

### 6. Priority is an ordering, and the interface says so

The queue page states, in a permanent notice rather than a footnote:

> **A case below the capacity line has not been cleared.** It has not been
> reviewed.

The evaluation reports `injected_patterns_left_unreviewed` — the count of planted
patterns sitting in the backlog — as a headline row rather than omitting it.

### 7. Evidence must resolve

Every alert names the transfer ids it rests on. Every one of those must exist in
the transfer table. An alert citing a row that is not there would be an
unfalsifiable claim inside an interface that invites trust.

Measured at 100% across three seeds; a dangling reference is reported by count
and by example.

*Tested by:* `score_traceability` in the evaluation suite.

---

## What a reviewer is expected to do that the system cannot

The counter-evidence hints are not decoration; they name work only a person can
do:

- Pull the payroll or invoice schedule and see whether the amounts match it.
- Ask whether the customer's declared business makes same-day forwarding normal.
- Check whether a "funnel" is a school, a landlord or a marketplace.
- Check whether a leg of a circular flow is a refund.
- Check when the customer profile was last reviewed before treating a mismatch
  as a signal.
- Check whether a missing field is missing across an entire channel, in which
  case it says nothing about this customer.

Each of these is a question the data in this system cannot answer. Saying so is
the point.

---

## Known limits of the enforcement itself

Stated because a boundary document that lists only successes is advertising.

- **The audit chain detects tampering; it does not prevent it.** An actor who can
  rewrite the whole table can recompute every link. It makes a partial edit
  visible, which is the realistic guarantee.
- **`_references_evidence` is a shallow check.** It looks for an id prefix, a
  typology name or a document word. A determined user can satisfy it with
  "invoice" and no thought. It stops the reflexive one-word disposition; it
  cannot grade reasoning.
- **The role is self-declared in the demo.** There is no authentication. In a
  real deployment the actor would come from a session, not a dropdown. The
  boundary being demonstrated is that the *code path* refuses non-human actors,
  not that this prototype knows who you are.
- **Nothing here has been reviewed by a compliance professional.** The typologies
  are drawn from publicly described patterns. Whether they are the right six,
  whether the thresholds are sensible, and whether the counter-evidence lists are
  complete are all open questions that only a practitioner could close.
