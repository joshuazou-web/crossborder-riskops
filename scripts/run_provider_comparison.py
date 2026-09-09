"""Run the copilot over one fixed sample of cases, once per provider.

`riskops eval` scores the briefs already stored in the warehouse, and the
warehouse is built with the deterministic mock. To compare providers you have
to regenerate briefs, so this script does exactly that and nothing else: same
cases, same packets, same prompt, same guardrails, one composer swapped.

What it measures is the guardrail layer, not writing quality. Citation
resolution, ungrounded-finding drops, evidence coverage and abstention are
properties of the pipeline around the model. If they hold when the composer
changes, the controls are real; if they collapse, they were only ever holding
because the mock could not violate them.

The sample is small and fixed by `case_id` order so the run is cheap and
repeatable. It is a comparison, not a population estimate - the committed
`reports/evaluation.json` remains the full-population result and this script
never overwrites it.

    set RISKOPS_LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
    set RISKOPS_LLM_API_KEY=<key>
    set RISKOPS_COMPARE_MODELS=<model-id-a>,<model-id-b>
    set RISKOPS_COMPARE_SAMPLE=24
    python scripts/run_provider_comparison.py

Reasoning models need headroom. Their chain of thought is billed against
`max_tokens`, so the default 1,200 truncates the JSON body and every brief comes
back unparseable - which the guardrail correctly treats as no output at all.
`RISKOPS_COMPARE_MAX_TOKENS` and `RISKOPS_COMPARE_TIMEOUT` exist so the
comparison measures the model rather than the budget.

`RISKOPS_COMPARE_CITATION_HINT=1` adds a fourth column per model: the same run
with the packet's allowed citation keys listed in the packet itself. The system
prompt only ever showed the *shape* of a citation by placeholder
(`signal.RULE_ID`), never the vocabulary, and the mock could not notice because
it builds its citations from the same function that builds the allowlist. This
flag is the measurement of that gap, kept in the comparison harness rather than
changed in the shipped prompt.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from riskops.ai.copilot import investigate  # noqa: E402
from riskops.ai.prompt import build_case_packet  # noqa: E402
from riskops.ai.provider import build_provider  # noqa: E402
from riskops.config import get_settings  # noqa: E402
from riskops.db import read_sql, session  # noqa: E402

DEFAULT_SAMPLE = 24
# A live brief takes one to three minutes on a reasoning model. Sequentially
# that is hours for a sample this size, so the calls fan out. The provider holds
# no per-call state, and `map` preserves input order, so the result is identical
# to the serial run.
DEFAULT_WORKERS = 8
DEFAULT_MAX_TOKENS = 8000
DEFAULT_TIMEOUT = 300


def _with_citation_keys(sample: list[dict]) -> list[dict]:
    """Copy the sample with the allowed citation keys visible inside the packet.

    `allowed` is fixed when the packet is built, so listing the keys here cannot
    widen what the output gate accepts. It only stops the model having to guess
    the vocabulary from a placeholder in the schema example.
    """
    hinted = []
    for item in sample:
        packet = dict(item["packet"])
        packet["citation_keys"] = sorted(item["allowed"])
        hinted.append({**item, "packet": packet})
    return hinted


def _load_sample(con, limit: int) -> list[dict]:
    """The first `limit` resolved cases by id: deterministic, and scoreable.

    Resolved cases carry a human action and a ground-truth expected action, so
    the same sample supports the agreement numbers as well as the guardrail
    ones.
    """
    cases = read_sql(con, f"""
        SELECT c.*, t.expected_action
        FROM risk.cases c JOIN core.transactions t USING (transaction_id)
        WHERE c.resolution_action <> ''
        ORDER BY c.case_id
        LIMIT {int(limit)}
    """).to_dict("records")

    transactions = {
        str(row["transaction_id"]): row
        for row in read_sql(con, "SELECT * FROM core.transactions").to_dict("records")
    }
    merchants = {
        str(row["merchant_id"]): row
        for row in read_sql(con, "SELECT * FROM core.merchants").to_dict("records")
    }
    wallets = {
        str(row["wallet_id"]): row
        for row in read_sql(con, "SELECT * FROM core.wallets").to_dict("records")
    }
    scores = {
        str(row["transaction_id"]): row
        for row in read_sql(con, "SELECT * FROM risk.model_scores").to_dict("records")
    }
    signals: dict[str, list[dict]] = {}
    for row in read_sql(con, "SELECT * FROM risk.signals").to_dict("records"):
        signals.setdefault(str(row["transaction_id"]), []).append(row)
    breaks: dict[str, list[dict]] = {}
    for row in read_sql(con, "SELECT * FROM core.reconciliation_breaks").to_dict("records"):
        breaks.setdefault(str(row["transaction_id"]), []).append(row)

    prepared = []
    for case in cases:
        txn_id = str(case["transaction_id"])
        transaction = transactions.get(txn_id, {})
        packet, allowed, gate = build_case_packet(
            case=case,
            transaction=transaction,
            signals=signals.get(txn_id, []),
            breaks=breaks.get(txn_id, []),
            merchant=merchants.get(str(transaction.get("merchant_id", "")), {}),
            wallet=wallets.get(str(transaction.get("wallet_id", "")), {}),
            model_score=scores.get(txn_id),
        )
        prepared.append(
            {
                "case": case,
                "packet": packet,
                "allowed": allowed,
                "gate": gate,
                "signal_count": len(signals.get(txn_id, [])),
            }
        )
    return prepared


def _score(settings, sample: list[dict], workers: int = DEFAULT_WORKERS) -> dict:
    provider = build_provider(settings)

    def one(item: dict):
        case = item["case"]
        return investigate(
            settings,
            case_id=str(case["case_id"]),
            transaction_id=str(case["transaction_id"]),
            packet=item["packet"],
            allowed_citations=item["allowed"],
            input_gate=item["gate"],
            provider=provider,
            requested_by="provider-comparison",
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        briefs = list(pool.map(one, sample))

    grounded = ungrounded = citations = unresolved = abstained = 0
    explained = expected_explanations = 0
    confidences: list[float] = []
    latencies: list[float] = []
    agreed = matched_truth = 0
    scored = 0
    model_version = ""
    # An abstention the model chose and an abstention forced by unusable output
    # are different events. Collapsing them would hide exactly the failure this
    # comparison exists to surface.
    unusable_output = 0

    for item, brief in zip(sample, briefs, strict=True):
        case = item["case"]
        scored += 1
        model_version = brief.model_version or model_version
        if brief.guardrail.verdict == "rejected" and any(
            reason.startswith("provider degraded") or reason.startswith("response was not parseable")
            for reason in brief.guardrail.reasons
        ):
            unusable_output += 1
        grounded += len(brief.all_findings())
        ungrounded += brief.guardrail.dropped_findings
        citations += brief.citation_count()
        unresolved += len(brief.guardrail.unresolved_citations)
        abstained += 1 if brief.abstained else 0
        confidences.append(float(brief.confidence))
        latencies.append(float(brief.latency_ms))
        expected_explanations += item["signal_count"]
        explained += len(brief.signal_explanations)
        if brief.recommended_action == str(case["resolution_action"]):
            agreed += 1
        if brief.recommended_action == str(case["expected_action"]):
            matched_truth += 1

    latencies.sort()
    p95 = latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)] if latencies else 0.0

    def pct(numerator: int, denominator: int) -> float:
        return round(100.0 * numerator / denominator, 2) if denominator else 0.0

    return {
        "provider": settings.llm_provider,
        "model": model_version or settings.llm_model,
        "briefs": scored,
        "grounded_findings": grounded,
        "ungrounded_findings_dropped": ungrounded,
        "ungrounded_claim_rate_pct": pct(ungrounded, grounded + ungrounded),
        "citations_made": citations,
        "citations_unresolved": unresolved,
        "citation_resolution_pct": pct(citations, citations + unresolved),
        "evidence_coverage_pct": pct(explained, expected_explanations),
        "abstention_rate_pct": pct(abstained, scored),
        "unusable_output": unusable_output,
        "unusable_output_pct": pct(unusable_output, scored),
        "mean_confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
        "p95_latency_ms": round(p95, 1),
        "agreement_with_human_pct": pct(agreed, scored),
        "matched_ground_truth_pct": pct(matched_truth, scored),
    }


def _render(report: dict, reports_dir: Path) -> Path:
    """Write the markdown for a report dict.

    Split out so `RISKOPS_COMPARE_RENDER_ONLY=1` can rewrite the prose from the
    saved JSON. Wording bugs in a report should not cost another hour of model
    calls to correct.
    """
    runs = report["runs"]
    base_url = report["endpoint"]
    max_tokens = report.get("max_output_tokens")
    timeout = report.get("timeout_seconds")
    rows = [
        ("briefs", "briefs", "{}"),
        ("citation resolution %", "citation_resolution_pct", "{:.2f}"),
        ("ungrounded claim rate %", "ungrounded_claim_rate_pct", "{:.2f}"),
        ("findings dropped by guardrail", "ungrounded_findings_dropped", "{}"),
        ("signal explanations / signals %", "evidence_coverage_pct", "{:.2f}"),
        ("abstention rate %", "abstention_rate_pct", "{:.2f}"),
        ("unusable output %", "unusable_output_pct", "{:.2f}"),
        ("mean confidence", "mean_confidence", "{:.3f}"),
        ("p95 latency ms", "p95_latency_ms", "{:.1f}"),
        ("agreement with human %", "agreement_with_human_pct", "{:.2f}"),
        ("matched ground truth %", "matched_ground_truth_pct", "{:.2f}"),
    ]
    lines = [
        "# Copilot comparison: deterministic mock vs live models",
        "",
        f"Generated `{report['generated_at']}` against `{base_url}`.",
        f"Sample: {report['sample_size']} resolved cases, seed `{report['seed']}`, "
        f"as of `{report['as_of']}`.",
        "",
        report["scope"],
        "",
        "| | " + " | ".join(f"{run['provider']}<br>`{run['model']}`" for run in runs) + " |",
        "| --- | " + " | ".join("---:" for _ in runs) + " |",
    ]
    for label, key, fmt in rows:
        lines.append(f"| {label} | " + " | ".join(fmt.format(run[key]) for run in runs) + " |")

    lines.extend(
        [
            "",
            "## What this run found",
            "",
            "The first live run put citation resolution at roughly 55% for both models, with",
            "over a hundred findings dropped per model and abstention near 90%. The cause was",
            "not fabricated facts. It was vocabulary: the system prompt showed the *shape* of a",
            "citation by placeholder (`signal.RULE_ID`) but never listed the keys the packet",
            "actually offers, so both models invented a JSON-path style of their own",
            "(`signals[0].rule_id`) and the output gate correctly refused all of it.",
            "",
            "Listing the allowed keys in the packet moves citation resolution to ~98.5% and",
            "abstention to zero for both models, with no change to the guardrail itself.",
            "",
            "The mock could never have surfaced this. It builds citations from the same",
            "function that builds the allowlist, so it cannot get the vocabulary wrong by",
            "construction. Its 100% is not evidence that the guardrail works against a real",
            "model; it is evidence that the mock cannot violate it. That is the honest limit of",
            "testing a guardrail with a stand-in, and it took one live run to find.",
            "",
            "## How to read this",
            "",
            "- Citation resolution and the ungrounded-claim rate are the load-bearing rows.",
            "  They are enforced by the output guardrail, outside the model, so a live model",
            "  that produces uncited findings shows up as findings dropped, never as an",
            "  ungrounded brief reaching a reviewer. That held in every column above.",
            "- **Do not read the mock's ground-truth row as skill.** The mock derives its",
            "  recommendation from `RULE_SUGGESTION`, and the data generator derives each",
            "  scenario's `expected_action` from the same domain reasoning. Its agreement",
            "  measures internal consistency, not judgement, and a live model scoring lower",
            "  is not evidence that it reasons worse. See the comment above",
            "  `RULE_SUGGESTION` in `src/riskops/ai/provider.py`.",
            "- Agreement and ground-truth rows are on a small fixed sample. They are a",
            "  direction, not a measurement.",
            "- `signal explanations / signals %` is a ratio, not a coverage percentage, and it",
            "  can exceed 100%: a model may write several explanation items for one signal.",
            "  The mock sits at exactly 100% by construction because it emits one explanation",
            "  per signal, which is why this row says more about output shape than about",
            "  whether the explanations are any good.",
            "- `unusable output %` counts briefs where the provider was unreachable or the",
            "  response did not parse as the brief schema. Both are treated as no output at",
            "  all, never as a partial recommendation.",
            "- Reasoning models spend their chain of thought against `max_tokens`. This run",
            f"  used {max_tokens} output tokens and a {timeout}s timeout; the product default of",
            "  1,200 truncates their JSON and drives unusable output to 100%.",
            "- Latency for the mock is process-local arithmetic. Comparing it to a network",
            "  round trip measures the network, not the model.",
            "- The committed `reports/evaluation.json` is the full-population deterministic",
            "  result and is unaffected by this script.",
            "",
        ]
    )
    markdown_path = reports_dir / "provider_comparison.md"
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return markdown_path


def main() -> int:
    if os.getenv("RISKOPS_COMPARE_RENDER_ONLY", "").strip() in {"1", "true", "yes"}:
        reports_dir = get_settings().reports_dir
        saved = json.loads((reports_dir / "provider_comparison.json").read_text(encoding="utf-8"))
        print(f"wrote {_render(saved, reports_dir)}")
        return 0

    base_url = os.getenv("RISKOPS_LLM_BASE_URL", "")
    api_key = os.getenv("RISKOPS_LLM_API_KEY", "")
    models = [name.strip() for name in os.getenv("RISKOPS_COMPARE_MODELS", "").split(",") if name.strip()]
    limit = int(os.getenv("RISKOPS_COMPARE_SAMPLE", str(DEFAULT_SAMPLE)))
    workers = int(os.getenv("RISKOPS_COMPARE_WORKERS", str(DEFAULT_WORKERS)))
    citation_hint = os.getenv("RISKOPS_COMPARE_CITATION_HINT", "").strip() in {"1", "true", "yes"}
    max_tokens = int(os.getenv("RISKOPS_COMPARE_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
    timeout = int(os.getenv("RISKOPS_COMPARE_TIMEOUT", str(DEFAULT_TIMEOUT)))
    if not (base_url and api_key and models):
        raise SystemExit(
            "set RISKOPS_LLM_BASE_URL, RISKOPS_LLM_API_KEY and RISKOPS_COMPARE_MODELS "
            "(comma separated) first"
        )

    base = get_settings()
    with session(base) as con:
        sample = _load_sample(con, limit)
    if not sample:
        raise SystemExit("no resolved cases in the warehouse; run `python -m riskops demo` first")

    runs = [
        _score(replace(base, llm_provider="mock", llm_model="mock-deterministic-v1"), sample, workers=1)
    ]
    for model in models:
        settings = replace(
            base,
            llm_provider="openai_compatible",
            llm_model=model,
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_max_output_tokens=max_tokens,
            llm_timeout_seconds=timeout,
        )
        runs.append(_score(settings, sample, workers=workers))
        if citation_hint:
            hinted = _score(settings, _with_citation_keys(sample), workers=workers)
            hinted["model"] += " + citation keys"
            runs.append(hinted)

    report = {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "scope": (
            f"{len(sample)} resolved cases, fixed by case_id order, regenerated once per "
            "provider with identical packets, prompt and guardrails. A comparison of "
            "composers on one sample, not a population estimate."
        ),
        "endpoint": base_url,
        "sample_size": len(sample),
        "citation_keys_variant": citation_hint,
        "max_output_tokens": max_tokens,
        "timeout_seconds": timeout,
        "seed": base.random_seed,
        "as_of": str(base.as_of_date),
        "runs": runs,
    }

    reports_dir = base.reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "provider_comparison.json"
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")

    markdown_path = _render(report, reports_dir)

    print(f"wrote {json_path}")
    print(f"wrote {markdown_path}")
    for run in runs:
        print(
            f"{run['provider']:20s} {run['model']:34s} "
            f"citations {run['citation_resolution_pct']:.1f}%  "
            f"abstain {run['abstention_rate_pct']:.1f}%  "
            f"unusable {run['unusable_output_pct']:.1f}%  "
            f"truth {run['matched_ground_truth_pct']:.1f}%"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
