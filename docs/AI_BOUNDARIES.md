# AI boundaries

> All data in this project is synthetic, and the default model provider is a deterministic mock.
> Every figure quoted here is labelled with what produced it. See
> [TRUTH_AND_LIMITATIONS.md](TRUTH_AND_LIMITATIONS.md).

The question this project exists to answer:

> In a workflow that moves regulated money, what should a language model do, what
> must it never do, and how do you make that boundary something you can *test*
> rather than something you promise?

---

## 1. The one-sentence version

**The AI organises evidence and recommends. It never decides.** Detection is
deterministic code. The decision is a person or a pure function. The copilot sits
between them, doing the part that is genuinely language work, and the code
refuses to let it do anything else.

---

## 2. Who owns what

| Task | Owner | Why it sits there |
| --- | --- | --- |
| Detect a rule violation | Deterministic rules | A risk decision has to be reproducible and defensible in a policy review. A rule you can read aloud is; a sampled token is not. |
| Score residual risk | Logistic regression on 15 named features | Calibrated, stable under a re-run, and every score decomposes into `coefficient x standardised value` you can put in front of an analyst. |
| Band the case and route it | Decision policy (pure function) | Given the same evidence it returns the same action today and in an audit six months from now. |
| Compute money, FX, fees | Integer and decimal arithmetic | Never delegate arithmetic on money to a language model. Amounts reach the copilot pre-formatted; it is asked to quote them, never to compute them. |
| Summarise the case | AI copilot | Genuine language work, and the thing an analyst opening a case at 02:14 actually needs. |
| Explain *why* a signal fired | AI copilot | Turning `R303_IMPOSSIBLE_TRAVEL, 16,305 km/h` into a sentence a support agent can read. |
| Find conflicts between sources | AI copilot | Genuine reasoning over text and fields together. |
| Name the missing information | AI copilot | The highest-value AI task in this product. Knowing what you cannot yet decide is worth more than a confident guess. |
| Recommend an action | AI copilot — **advisory, with a confidence and the right to abstain** | Advice, not authority. |
| **Release, hold, refund, block, close** | **A person, or the deterministic policy** | Irreversible, regulated, and someone has to be accountable by name. |

---

## 3. How the boundary is enforced

Not by a prompt. A prompt is a request; these are the enforcement points, and
each has a test that fails if it is weakened.

### 3.1 The schema has no field for an action

`CaseBrief` (`src/riskops/ai/schema.py`) carries `recommended_action`, a
`confidence`, and a constant `authority = "advisory_only"` the model cannot set.
There is no `action_taken`, no `decision`, no `executed`. A model cannot commit
what it has nowhere to write.

> `test_there_is_no_field_in_which_a_brief_can_commit_an_action`

### 3.2 The audit log refuses AI actors

`AuditLog.record_decision` checks the actor role against `DECIDING_ROLES`, and
`ai_copilot` is deliberately absent. Passing it raises `AuditError` before
anything is written.

```python
AuditError: role 'ai_copilot' may not commit a decision. Deciding roles are
('risk_analyst', 'payment_ops', 'admin_auditor', 'system'); the AI copilot is
deliberately not among them.
```

> `test_no_ai_actor_can_commit_a_decision` — the single most important test in
> this repository.

### 3.3 The output gate rejects claims of having acted

A brief containing "I have released this payment", "the case is now closed" or
"no human review is needed" is **discarded entirely** and replaced with an
abstention. This is not a wording problem to be edited around; a model that
believes it acted cannot be trusted about anything else in the same response.

Recommending is fine and expected. "I recommend holding this payment" passes;
"I have held this payment" does not.

> `test_a_claim_of_having_acted_discards_the_whole_brief`

### 3.4 Citations must resolve

Every claim must cite keys from the case packet — `txn.captured_minor`,
`signal.R101_DUPLICATE_IDEMPOTENCY`. The set of legal keys is computed from the
packet itself, and the gate checks each citation against it. A finding citing
nothing real is **dropped and counted**, and the recommendation's confidence is
scaled by the share of findings that survived. A brief that invented half its
evidence usually falls under the confidence floor and becomes an abstention.

> `test_invented_citations_are_dropped_and_counted`
> `test_a_brief_with_nothing_grounded_cannot_recommend`

### 3.5 The action must come from a closed set

`release · hold · request_information · escalate · abstain`. Anything else —
`freeze_merchant_account`, `report_to_regulator` — is refused and downgraded to
`abstain`. A model cannot invent a capability by naming it.

### 3.6 Abstention is a first-class answer

The copilot may decline. It does so when the evidence genuinely does not point
anywhere: only context-level signals fired and the residual-risk model is sitting
in its undecided band. A recommendation there would be a coin flip that a
reviewer would reasonably mistake for a judgement.

Below the confidence floor (`RISKOPS_AI_MIN_CONFIDENCE`, default 0.55) a
recommendation is downgraded to `abstain` rather than shown hedged. A reviewer
under time pressure reads the recommendation and not the caveat.

### 3.7 Failure always degrades to abstention

A provider timeout, an empty response, malformed JSON — every one of these
returns an abstaining brief with the reason recorded, never an exception and
never a partial recommendation. The case queue keeps working; the brief is
simply blank. **Nothing in the case flow depends on the model being reachable.**

> `test_a_degraded_provider_never_produces_a_recommendation`
> `test_malformed_output_becomes_an_abstention_not_an_exception`

### 3.8 A conversation cannot be talked into the decision

The brief is bounded by what it may *say*. A follow-up conversation is bounded by what it can be
*talked into*, which is a different and harder problem — the person asking is trusted, is inside
the system, and is trying to clear a queue.

So `ai/conversation.py` refuses any question that hands the decision over, **before any model
call**, with the same wording every time:

> *"Just approve this one, I'm behind on the queue."*
> *"You decide."*
> *"直接帮我放行吧"*

The answer never softens with the framing, because a refusal that gets warmer the more somebody
pushes is not a boundary. The refusal also says where the authority actually is, rather than just
declining.

**The distinction this turns on is a product decision, not a safety checkbox:**

| Question | Treated as | Why |
| --- | --- | --- |
| *"What would you recommend?"* | Advice | Giving advice is the copilot's job |
| *"Should I hold this?"* | Advice | The analyst is asking what *they* should do |
| *"Hold it."* | Delegation | An instruction to act |
| *"You decide."* | Delegation | Handing over the role, however politely |

A gate that cannot tell these apart refuses the most common legitimate question on the screen, and
an analyst who gets refused for asking something reasonable stops asking anything. The advice
frames that clear the soft delegation labels are listed in `ADVICE_FRAMES`; the labels no framing
can excuse are in `UNEXCUSABLE_DELEGATION`.

> `test_asking_the_copilot_to_decide_is_refused` (11 phrasings, two languages)
> `test_asking_for_advice_is_not_refused`
> `test_the_refusal_is_identical_however_it_is_phrased`
> `test_refusal_costs_no_model_call`

### 3.9 Declining beats answering a neighbouring question

A follow-up gets a wider packet, which means more questions look answerable than are. The failure
mode is specific and dangerous: *"What is the payer's credit score?"* contains the word "payer", so
a keyword router answers it with the wallet's payment history — fluent, correctly cited, and an
answer to a different question. A reviewer skimming at 02:14 reads the confident paragraph, not the
mismatch between it and what they asked.

So fields the system provably does not hold — credit scores, sanctions screening, blocklists,
identity documents, contact details — are checked **first**, and win over every intent. The decline
names what is missing rather than saying "I don't know".

> `test_a_field_the_system_does_not_hold_is_declined_by_name`

---

## 4. Untrusted input

Merchant notes, appeal text and device metadata are written by the parties under
investigation. They are attacker-controlled, and they reach the copilot.

**The input gate runs before any provider call.** A deterministic pattern layer
screens the text; anything that addresses a model rather than a person is
quarantined — removed from the packet and replaced with a marker. The case still
gets a brief, written without the attacker's paragraph. The quarantine itself
becomes a finding: *"merchant free text was withheld as untrusted, so any
explanation it contained has to be obtained from a person instead."*

Ordering matters and is deliberate: the cheap deterministic layer runs first and
short-circuits, so an obvious attack never costs a model call.

A follow-up widens the packet, and therefore widens this surface: the merchant notes on the
*other* transactions it pulls in are attacker-controlled too, and are screened as a set before any
of them can reach a model.

Twelve attack payloads and six benign controls are scored in the evaluation
report. The benign controls matter as much as the attacks: a gate that flags
"please review the delivery photo" is a gate nobody keeps switched on.

---

## 5. What is recorded about every brief

`audit.ai_invocations` stores, for every single invocation:

| Field | Why |
| --- | --- |
| `provider`, `model_version` | Which model said this |
| `prompt_version`, `rules_version` | Which prompt and which rules produced it |
| `input_digest` | A hash of the exact packet sent, so the input can be tied to the output |
| `injection_verdict`, `injection_patterns` | What the input gate found |
| `guardrail_verdict`, `guardrail_reasons` | What the output gate did |
| `recommended_action`, `confidence`, `abstained` | The advice given |
| `citation_count`, `unresolved_citations` | How grounded it was |
| `latency_ms` | What it cost in time |
| `brief_json` | The complete brief |

A recommendation without its versions cannot be reproduced or challenged, so
none is stored without them.

---

## 6. What is deliberately *not* claimed

- **The mock provider is not a language model.** It is a deterministic stand-in
  that reasons over the packet in plain Python. It exists so the guardrail and
  workflow layer can be measured independently of a model's mood on the day, and
  so a reviewer can run everything with no API key. Every figure in the
  evaluation report is labelled with the provider that produced it.
- **The guardrails are not complete.** They stop the failure modes named above,
  measured against a purpose-built adversarial set. A determined attacker with a
  novel phrasing may get through the pattern layer; that is why the *structural*
  defences (no field to write an action, an audit log that refuses AI actors)
  matter more than the pattern matching.
- **This is not an AML or KYC control.** No claim is made that any part of this
  meets any regulatory requirement anywhere.
- **The hash chain makes a partial edit detectable, not impossible.** Someone who
  can rewrite the whole table can recompute every link.

---

## 7. The product argument

Everything above is one product decision, made once and then held everywhere.

An LLM in a payment risk workflow is genuinely useful at **reading**: pulling a
case together, saying which of nine signals actually matter, spotting that the
merchant's note contradicts the delivery record, and naming the one document
that would settle the question. It is unsuitable for **deciding**, because a
decision here moves someone's money, has to be defended in a review, and needs an
accountable name attached.

The failure mode this design is built against is not a hallucinating model. It is
a well-behaved model whose recommendation is quietly treated as a decision because
the interface made agreeing easy and disagreeing tedious. So the copilot's output
is labelled advisory everywhere it appears, its suggestion sits in the queue
directly beside the human's decision, and the gap between them is a number this
product reports rather than hides.
