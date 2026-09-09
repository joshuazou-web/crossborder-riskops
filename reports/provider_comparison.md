# Copilot comparison: deterministic mock vs live models

Generated `2026-09-09T05:00:54+00:00` against `https://ark.cn-beijing.volces.com/api/v3`.
Sample: 24 resolved cases, seed `20260815`, as of `2026-08-31`.

24 resolved cases, fixed by case_id order, regenerated once per provider with identical packets, prompt and guardrails. A comparison of composers on one sample, not a population estimate.

| | mock<br>`mock-deterministic-v1` | openai_compatible<br>`deepseek-v4-pro-ga-260813` | openai_compatible<br>`deepseek-v4-pro-ga-260813 + citation keys` | openai_compatible<br>`glm-5-2-260617` | openai_compatible<br>`glm-5-2-260617 + citation keys` |
| --- | ---: | ---: | ---: | ---: | ---: |
| briefs | 24 | 24 | 24 | 24 | 24 |
| citation resolution % | 100.00 | 55.25 | 98.55 | 56.86 | 98.58 |
| ungrounded claim rate % | 0.00 | 41.98 | 1.13 | 49.58 | 2.47 |
| findings dropped by guardrail | 0 | 110 | 3 | 118 | 6 |
| signal explanations / signals % | 100.00 | 78.79 | 154.55 | 57.58 | 109.09 |
| abstention rate % | 8.33 | 87.50 | 0.00 | 87.50 | 0.00 |
| unusable output % | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| mean confidence | 0.610 | 0.431 | 0.711 | 0.398 | 0.783 |
| p95 latency ms | 0.1 | 122697.5 | 124288.5 | 84845.4 | 74896.2 |
| agreement with human % | 45.83 | 0.00 | 16.67 | 0.00 | 12.50 |
| matched ground truth % | 66.67 | 0.00 | 12.50 | 4.17 | 16.67 |

## What this run found

The first live run put citation resolution at roughly 55% for both models, with
over a hundred findings dropped per model and abstention near 90%. The cause was
not fabricated facts. It was vocabulary: the system prompt showed the *shape* of a
citation by placeholder (`signal.RULE_ID`) but never listed the keys the packet
actually offers, so both models invented a JSON-path style of their own
(`signals[0].rule_id`) and the output gate correctly refused all of it.

Listing the allowed keys in the packet moves citation resolution to ~98.5% and
abstention to zero for both models, with no change to the guardrail itself.

The mock could never have surfaced this. It builds citations from the same
function that builds the allowlist, so it cannot get the vocabulary wrong by
construction. Its 100% is not evidence that the guardrail works against a real
model; it is evidence that the mock cannot violate it. That is the honest limit of
testing a guardrail with a stand-in, and it took one live run to find.

## How to read this

- Citation resolution and the ungrounded-claim rate are the load-bearing rows.
  They are enforced by the output guardrail, outside the model, so a live model
  that produces uncited findings shows up as findings dropped, never as an
  ungrounded brief reaching a reviewer. That held in every column above.
- **Do not read the mock's ground-truth row as skill.** The mock derives its
  recommendation from `RULE_SUGGESTION`, and the data generator derives each
  scenario's `expected_action` from the same domain reasoning. Its agreement
  measures internal consistency, not judgement, and a live model scoring lower
  is not evidence that it reasons worse. See the comment above
  `RULE_SUGGESTION` in `src/riskops/ai/provider.py`.
- Agreement and ground-truth rows are on a small fixed sample. They are a
  direction, not a measurement.
- `signal explanations / signals %` is a ratio, not a coverage percentage, and it
  can exceed 100%: a model may write several explanation items for one signal.
  The mock sits at exactly 100% by construction because it emits one explanation
  per signal, which is why this row says more about output shape than about
  whether the explanations are any good.
- `unusable output %` counts briefs where the provider was unreachable or the
  response did not parse as the brief schema. Both are treated as no output at
  all, never as a partial recommendation.
- Reasoning models spend their chain of thought against `max_tokens`. This run
  used 8000 output tokens and a 300s timeout; the product default of
  1,200 truncates their JSON and drives unusable output to 100%.
- Latency for the mock is process-local arithmetic. Comparing it to a network
  round trip measures the network, not the model.
- The committed `reports/evaluation.json` is the full-population deterministic
  result and is unaffected by this script.

