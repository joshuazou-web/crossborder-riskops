# Architecture

> All data is synthetic. See [TRUTH_AND_LIMITATIONS.md](TRUTH_AND_LIMITATIONS.md).

---

## The shape of it

```
┌────────────────────────────────────────────────────────────────────────────┐
│  GENERATION                                                                 │
│  riskops.generator  ·  one random.Random(seed) threads through everything   │
│    scenarios.py   20 named stories, each with a ground-truth label          │
│    synth.py       merchants, wallets, devices, FX quotes, payment events    │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │  raw.payment_events (append-only landing)
┌──────────────────────────────────▼─────────────────────────────────────────┐
│  PIPELINE                                                                   │
│    build.normalise_events   collapse aliases, drop re-deliveries            │
│    build.build_core         replay each transaction through the state       │
│                             machine; illegal events quarantined with a      │
│                             reason, never applied                           │
│    build._reconcile         5 money invariants, exact arithmetic            │
│    validate.run_checks      severity-tiered gate: error → rollback,         │
│                             warning → recorded and surfaced                 │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │  core.transactions, core.payment_events,
                                   │  core.reconciliation_breaks, core.{merchants,wallets,...}
┌──────────────────────────────────▼─────────────────────────────────────────┐
│  RISK ENGINE                     no language model anywhere in this box      │
│    rules.evaluate      20 deterministic rules → risk.signals                 │
│    scoring.train/score logistic regression, 15 named features →              │
│                        risk.model_scores with per-feature contributions      │
│    policy.decide       pure function → risk.policy_decisions                 │
│    cases.build_cases   → risk.cases with an owner, a priority and an SLA     │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼─────────────────────────────────────────┐
│  AI COPILOT                                     advisory only, no authority  │
│    guardrails.screen_untrusted_text   INPUT GATE, before any provider call   │
│    prompt.build_case_packet           packet + the set of legal citations    │
│    provider.build_provider            mock (default) | openai_compatible     │
│    copilot.investigate                parse → schema                         │
│    guardrails.apply_output_guardrails OUTPUT GATE                            │
│    copilot.record_invocation          → audit.ai_invocations                 │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼─────────────────────────────────────────┐
│  HUMAN DECISION LAYER                                                        │
│    review.workflow    assign · submit_decision · file_appeal · resolve_appeal│
│    audit.log          record_decision refuses any AI actor                   │
│    audit.chain        SHA-256 forward chain over every entry                 │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │  audit.audit_log, audit.decisions, audit.appeals
┌──────────────────────────────────▼─────────────────────────────────────────┐
│  MARTS  sql/marts.sql   every dashboard metric defined exactly once          │
│  APP    Streamlit       filters and aggregates marts; never redefines a metric│
│  EVAL   riskops.eval    5 suites → reports/evaluation.json → the report       │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## Warehouse layers

One DuckDB file, five schemas. Chosen over Postgres deliberately: a reviewer clones the repo and
runs the demo with no server, while the analytics stay real SQL rather than pandas in a trench coat.

| Schema | Contents | Rule it obeys |
| --- | --- | --- |
| `raw` | `payment_events` | Exactly what the simulated network sent. Append-only, never edited. |
| `core` | `transactions`, `payment_events`, `merchants`, `wallets`, `devices`, `fx_quotes`, `reconciliation_breaks` | The canonical model. Every state here came from replaying events, never from a status field. |
| `risk` | `signals`, `model_scores`, `policy_decisions`, `cases` | Rule output, model output, and the cases they produced. |
| `audit` | `audit_log` (hash-chained), `ai_invocations`, `decisions`, `appeals`, `refresh_log`, `validation_results` | Append-only. Nothing is updated; a change writes a new entry. |
| `marts` | ~16 KPI tables plus `fct_transactions` and `fct_cases` | Every metric defined exactly once. The UI filters these and never re-implements a definition. |

---

## Why each technology is here

| Choice | Reason | What was rejected |
| --- | --- | --- |
| **DuckDB** | One file, no server, real SQL. `git clone && pip install && run`. | Postgres (needs a server), SQLite (weak analytics), pandas-only (no SQL to read) |
| **scikit-learn logistic regression** | Interpretable, stable under a re-run, and `coefficient × standardised value` is a number an analyst can argue with | Gradient boosting: better on paper, drifts between seeds on 6,000 rows, and unexplainable in a policy review |
| **Streamlit** | Data-dense internal tooling in Python, which is what this is | React + FastAPI: more control, three times the code, none of it the point |
| **`urllib` for the LLM call** | One HTTP POST with a JSON body does not justify a dependency, and a reviewer can read exactly what leaves the process | An SDK |
| **Integer minor units** | Exactness on a money path is not optional | `float`, `Decimal` on storage |
| **Seeded `random.Random`** | A number nobody can reproduce is not a result | `numpy.random` global state |

---

## Key modules

| Module | Responsibility | Why it is separate |
| --- | --- | --- |
| `money.py` | The only place money arithmetic happens | Confining it makes "money is never a float" checkable by reading one file |
| `taxonomy.py` | The controlled vocabulary — 6 dimensions, 48 values | A value not defined here cannot be written to the warehouse |
| `statemachine.py` | Legal transitions and ledger legality | Returns a result object rather than raising, so the pipeline quarantines a bad event instead of aborting a batch of 40,000 |
| `risk/rules.py` | Detection | Deterministic, and every signal names its evidence fields — which is what makes "ungrounded claim" computable later |
| `risk/policy.py` | The routing decision | A pure function; the only component that may act without a human |
| `ai/guardrails.py` | Both gates | The enforcement point for the product's central claim |
| `ai/conversation.py` | Follow-up questions, entity context, delegation refusal | A conversation can be *talked into* things a one-shot brief cannot, so it needs a boundary of its own |
| `eval/robustness.py` | Seed sweep, baselines, ablation, threshold curve | A single-run number implies a precision nobody measured |
| `audit/log.py` | Append-only writes and the authority check | `record_decision` refuses `ai_copilot` before anything is written |
| `audit/chain.py` | Hash chaining and verification | Returns three states (`verified`/`broken`/`unverifiable`), because "predates chaining" and "was tampered with" are opposite findings |
| `eval/runner.py` | Every quoted number | Nothing is typed by hand into a README |
| `app/_i18n.py` | English and Chinese | The English source text *is* the key, so a missing translation degrades to correct English rather than to a placeholder, and the page code stays readable on its own |
| `app/Home.py` | Entry point and navigation | Streamlit builds a `pages/` sidebar from *filenames*, which cannot be translated. `st.navigation` takes an explicit title per page, so the sidebar reads the same language as the screen. The views moved to `app/views/` and `url_path` is pinned, so a pasted link keeps working |

---

## What the Chinese interface does and does not translate

The interface translates. Three things deliberately do not:

- **Identifiers** — `CASE_0001121`, `R303_IMPOSSIBLE_TRAVEL`, `txn.captured_minor`. A screenshot in
  one language has to be matchable to a log line in the other.
- **Money and currency codes.** `2,488.83 PHP` is the same string in both, because it is a value,
  not prose.
- **Generated content** — rule details, AI briefs, audit summaries. These come from the pipeline and
  the model rather than the interface, and machine-translating a signal's `detail` string would put
  a translation error *inside the evidence*. The sidebar says so rather than leaving a reader to
  wonder why half the case detail is still English.

`tests/test_i18n.py` parses each page's AST, collects every literal passed to `t()`, and fails if a
wrapped string has no translation or if a translation exists that no page uses any more. It also
reads the `PAGES` table out of the router and checks every sidebar entry separately, because the
navigation is the first thing anyone sees and the one place a half-translated interface reads as
broken rather than pragmatic. That second
check is the one that matters over time: it catches the case where a caption is reworded and the
Chinese quietly stops appearing.

---

## Two ordering decisions worth knowing

**The input gate runs before the provider call.** A deterministic pattern layer screens untrusted
text and short-circuits, so an obvious injection never costs a model call, and only what survives
reaches anything probabilistic. The same idea in reverse governs the output: whatever comes back is
untrusted too, and passes the citation, authority, vocabulary and PII checks before a reviewer sees it.

**Linkage rules run in a second pass.** A shared payout account is only interesting in the context
of the whole batch — either as an amplifier on a transaction that already has a signal, or as a ring
escalation when another merchant in the group produced a high-severity signal. Neither is visible
from a single transaction, so the rule engine makes two passes over the data rather than one.

---

## Refresh, and its rollback

`python -m riskops refresh` runs: generate → land raw → replay to core → **validate** → rules →
model → policy → cases → briefs → simulated history → marts.

The validation gate is a gate. An error-severity failure restores `core.*` from the snapshot taken
at the start of the run and stops — a dashboard showing stale-but-correct data beats one showing
fresh-but-wrong data. Warning-severity failures are recorded, surfaced on the Overview page, and
let the run continue.

Every run writes its seed and the taxonomy, rules, model and policy versions to `audit.refresh_log`.
A metric without its versions is not reproducible, and this project only quotes reproducible metrics.

---

## Performance

A full build on a laptop takes about 90 seconds for 6,000 transactions and 23,671 events: about 20s
generating, 25s replaying and reconciling, 10s in the rule engine, 5s training and scoring, 25s
generating 1,615 briefs, and the rest in the marts.

The only optimisation that was needed: the brief loop originally filtered a 6,000-row DataFrame per
case, which is 10 million row comparisons per frame. Indexing once turned a two-minute job into a
twenty-second one. Nothing else was tuned, because nothing else needed it.
