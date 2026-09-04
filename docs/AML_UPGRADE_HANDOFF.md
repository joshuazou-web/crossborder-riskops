# AML upgrade handoff

Read this instead of re-scanning the repository. It records what was here before
the AML layer, what the layer added, what was reused, and where the boundaries
are — so a later session can act without rebuilding context from the file tree.

---

## What this project is now

**CrossBorder AML RiskOps** · 跨境支付反洗钱预警调查与风险运营平台
*Evidence-Grounded Cross-Border Payment Monitoring & Investigation Workspace*

Two products in one warehouse, deliberately:

| Layer | Question it answers | Schemas |
| --- | --- | --- |
| Payment risk operations (existing) | This payment looks wrong — release, hold, or ask? | `raw` `core` `risk` |
| Cross-border AML (added) | This *account* looks worth investigating — which sixty do we open today? | `aml` |
| Shared | Who did what, and can it be checked? | `audit` `marts` |

They are not merged. A payment decision is about one transaction in flight; an
AML case is about one subject over a window. Forcing them into one table would
have made both worse.

---

## Stack

Unchanged from before the upgrade. Nothing was added.

- Python 3.10+ (developed on 3.13)
- DuckDB — single-file warehouse at `data/riskops.duckdb`
- pandas, scikit-learn (payments model only; the AML layer uses no model)
- Streamlit + Plotly — eleven pages in three navigation groups
- pytest, ruff
- playwright + ffmpeg — screenshots, GIF and demo video only

No new dependency was introduced by the AML layer. It is pandas and the standard
library.

---

## What was reused rather than rebuilt

This mattered more than what was added. The AML layer is thin because these
already existed and were correct:

| Reused | Where | Why it mattered |
| --- | --- | --- |
| `money.py` — integer minor units, currency exponents | `aml/world.py` | AML compares amounts across 16 currencies. The exponent table already handled JPY (0dp) and BHD (3dp); a second one would have drifted. |
| `audit/log.py` — SHA-256 forward hash chain | `aml/investigation.py` | An AML disposition and a payment decision are now equally hard to edit after the fact, through the same chain. |
| `audit/chain.py` verification | Audit Log page | Unchanged. It verifies AML entries because they are in the same log. |
| `config.py` `Settings` | throughout | Two settings added under `extra`; no new configuration mechanism. |
| `db.py` `session`/`replace_table` | `aml/pipeline.py` | Same connection discipline, so the app still opens one handle. |
| `app/_shared.py` `page_setup`, `kpi_row`, `BAND_COLOUR` | all three AML pages | The AML pages look like the payment pages because they are built from the same helpers. |
| `app/_i18n.py` `t()` | all three AML pages | English source text is the key; 232 AML strings added to the same table. |
| AI guardrails (`ai/guardrails.py`, `ai/conversation.py`) | unchanged | **The AML layer does not call them.** See "AI boundary" below. |

---

## What the AML layer added

`src/riskops/aml/` — seven modules, none of them large:

| Module | Responsibility |
| --- | --- |
| `typology.py` | The six typologies as one shared vocabulary: id, version, severity, explanation template, evidence fields, counter-evidence. No thresholds live anywhere else. |
| `world.py` | The synthetic generator. Entities, background traffic, and typology injection at three difficulties. |
| `detect.py` | Six deterministic detectors. No model, no LLM. `AmlContext.blind()` strips the generator's provenance before detection. |
| `aggregate.py` | Deduplication (same typology + subject within 7 days) then case building (same subject within 30 days), with the merge reason recorded. |
| `priority.py` | Eight-factor explainable investigation priority; every factor's contribution stored on the case. |
| `investigation.py` | The disposition vocabulary, the mandatory-reason check, and the refusal of any non-human actor. |
| `pipeline.py` | Orchestration and persistence into the `aml` schema. |
| `evaluate.py` | Six suites, three seeds, and the generated report. |

Pages: `app/views/7_AML_Alert_Queue.py`, `8_Investigation_Workbench.py`,
`9_AML_Operations.py`, `10_AML_Evaluation.py`. The router in `app/Home.py`
groups all eleven pages.

CLI: `python -m riskops aml build | status | eval`.

---

## What was modified in existing code

Kept deliberately small. Anything not listed here is untouched.

| File | Change | Why |
| --- | --- | --- |
| `app/Home.py` | `PAGES` list → `PAGE_GROUPS` dict; three groups | Ten flat entries would make a reader guess which belong together. `PAGES` is kept as a flattened view for the tests and capture scripts. |
| `app/_shared.py` | Eight `aml.*` tables added to `TABLES` | One connection per process; a second handle hits DuckDB's mixed-configuration refusal. |
| `app/_i18n.py` | `ZH_AML` table, merged into `ZH` | Kept separate so it is obvious which strings belong to which surface. |
| `app/views/3_Case_Detail.py` | Reasons routed through `translate_reason` | Pre-existing fix, not part of this upgrade. |
| `config.py` | `extra` now carries `aml_n_transfers`, `aml_review_capacity` | No new configuration mechanism. |
| `cli.py` | `aml` sub-command group | Follows the existing `sub.add_parser` pattern. |
| `tests/test_i18n.py` | Navigation reader handles `PAGE_GROUPS` (an `AnnAssign`); stale-string exemptions derived from the typology and disposition tables rather than hard-coded | The old reader silently found nothing after the router changed, which made the check vacuous. |
| `typology.py` (during development) | `band_low` added to `STRUCTURING.evidence_fields` | A test caught that structuring explanations were rendering the raw template with braces, because a template field outside `evidence_fields` raises and falls back. |

---

## Test baseline

| | Before the upgrade | After |
| --- | --- | --- |
| Tests | 304 | **453** |
| ruff | clean | clean |

New files: `tests/test_aml_typologies.py` (67 — positive, negative and boundary
per typology, plus the blindness guarantee), `tests/test_aml_boundaries.py`
(47 — vocabulary, actor, reason, no-model, aggregation integrity).

Run: `python -m pytest -q` · `python -m ruff check src app tests scripts`

---

## The AI boundary in this layer

Stronger than on the payments side, and worth being precise about.

**The AML core loop contains no model at all.** Detection, deduplication,
aggregation and prioritisation import no provider — asserted by
`test_detection_and_aggregation_import_no_provider`, which walks the import
graph rather than grepping the text. An unreachable language model cannot change
a single alert, case or priority score.

**No AI actor can record a disposition.** `investigation.validate()` refuses
`ai_copilot`, `system`, `model`, `assistant` and everything else outside
`INVESTIGATOR_ROLES`, and it runs before the first database write — proven by a
test that passes a connection object which raises if touched.

**The vocabulary cannot express what the system cannot do.** There is no
disposition, case state or button meaning "confirmed laundering", "filed",
"reported", "frozen" or "blocked". Not hidden — absent.

The existing AI copilot on the payments side is untouched and still available
there. Extending it to draft AML case notes is P1, not this release.

---

## Scope of this release (P0)

**Done.** Transfers → detection → dedup → aggregation → priority under capacity →
investigation → disposition with mandatory reason → audit. Six typologies with
difficulty-graded injection. Explainable priority. Bilingual UI. Six evaluation
suites over three seeds.

**Deliberately not done.** Graph neural networks or any learned AML model.
Real-time streaming. Sanctions or PEP screening (out of scope and impossible to
do honestly with synthetic data). Regulatory reporting of any kind. Automatic
disposition. AI-drafted case notes. Cross-case network analysis beyond a case's
own counterparties. Any production deployment.

---

## Traps worth knowing before changing anything

1. **`AmlContext` refuses provenance columns.** Build it with
   `AmlContext.blind()`. Passing the raw frame raises, on purpose — a detector
   that could read `scenario_id` would make every recall figure meaningless.

2. **A recall of 1.000 is a bug report, not a result.** The first version of the
   generator built every scenario squarely inside the thresholds and scored 100%
   on all six typologies. If recall returns to 1.000, check whether the
   difficulty spread in `world.py` still spans the thresholds.

3. **`below_threshold` recall should stay low** (currently ~0.16). A high number
   there means the detectors are firing on noise.

4. **Priority band cuts are calibrated, not universal.** They were cut against
   seed 20260815 at 40,000 transfers. Changing the population size or the factor
   weights requires re-cutting them; the evaluation prints the band mix so this
   is visible.

5. **The committed warehouse is 40,000 transfers, the default is 100,000.** The
   smaller pack keeps the repository clonable (`data/riskops.duckdb` is ~32 MB).
   `python -m riskops aml build` with no argument produces 100,000.

6. **`docs/AML_EVALUATION_REPORT.md` is generated.** Editing it by hand produces
   a claim nobody can reproduce. Regenerate with `python -m riskops aml eval`.

7. **Two rule catalogues exist.** `RULE_CATALOGUE.md` documents the 20 payment
   rules; `AML_RULE_CATALOG.md` documents the six typologies. Different spelling
   is deliberate only in that they are different documents — do not merge them.

---

## Where to start reading

| If you want to know | Read |
| --- | --- |
| What the six typologies are and why | [AML_RULE_CATALOG.md](AML_RULE_CATALOG.md) |
| What the product refuses to do, and how that is enforced | [AML_POLICY_BOUNDARIES.md](AML_POLICY_BOUNDARIES.md) |
| How the synthetic data is built and why it is honest | [SYNTHETIC_DATA_DESIGN.md](SYNTHETIC_DATA_DESIGN.md) |
| What the numbers are | [AML_EVALUATION_REPORT.md](AML_EVALUATION_REPORT.md) *(generated)* |
| What none of it proves | [TRUTH_AND_LIMITATIONS.md](TRUTH_AND_LIMITATIONS.md) |
| The payment layer this was built on | [ARCHITECTURE.md](ARCHITECTURE.md) |
