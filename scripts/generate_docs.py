"""Generate the data dictionary and the rule catalogue from code.

Documentation that is written by hand drifts from the code within a month. These
two files are generated instead, and `tests/test_docs_and_readme.py` fails if
they are stale, so a taxonomy change cannot be merged with a stale dictionary.

Run: `python scripts/generate_docs.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from riskops.generator.scenarios import SCENARIOS, TARGET_ACTIONABLE_SHARE  # noqa: E402
from riskops.models import (  # noqa: E402
    CASE_COLUMNS,
    MERCHANT_COLUMNS,
    RAW_EVENT_COLUMNS,
    RECON_BREAK_COLUMNS,
    SIGNAL_COLUMNS,
    TRANSACTION_COLUMNS,
    WALLET_COLUMNS,
)
from riskops.money import CURRENCY_EXPONENTS  # noqa: E402
from riskops.risk.rules import RULES, RULES_VERSION, SEVERITY_WEIGHT  # noqa: E402
from riskops.statemachine import TERMINAL_STATES, TRANSITIONS  # noqa: E402
from riskops.taxonomy import (  # noqa: E402
    DIMENSIONS,
    TAXONOMY_EFFECTIVE_DATE,
    TAXONOMY_VERSION,
    total_value_count,
)

BANNER = (
    "> **Generated file - do not edit by hand.** Produced by "
    "`python scripts/generate_docs.py` from the code that actually runs. "
    "`tests/test_docs_and_readme.py` fails if it is stale."
)


def _table(rows: list[list[str]], header: list[str]) -> str:
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _columns_table(columns: dict[str, str]) -> str:
    return _table([[name, sql_type] for name, sql_type in columns.items()], ["Column", "Type"])


def data_dictionary() -> str:
    parts: list[str] = ["# Data dictionary", "", BANNER, ""]
    parts += [
        f"Taxonomy version `{TAXONOMY_VERSION}` (effective {TAXONOMY_EFFECTIVE_DATE}) - "
        f"{len(DIMENSIONS)} dimensions, {total_value_count()} values.",
        "",
        "**All data described here is synthetic.** No table contains a real transaction, "
        "merchant, wallet, device or person.",
        "",
        "---",
        "",
        "## How money is stored",
        "",
        "Every money column is a `BIGINT` count of **minor units**, with its ISO-4217 currency in "
        "the column beside it. There is no `DECIMAL` and no `DOUBLE` anywhere on a money path. "
        "FX rates are stored as `VARCHAR` holding the exact decimal string that was quoted, so a "
        "rate is never re-rounded by a float round-trip.",
        "",
        "Minor-unit exponents are **data, not a constant** - assuming every currency has two "
        "decimal places is the classic cross-border bug:",
        "",
        _table(
            [[code, str(exponent), f"{1} unit = {10 ** exponent} minor units"]
             for code, exponent in sorted(CURRENCY_EXPONENTS.items())],
            ["Currency", "Exponent", "Meaning"],
        ),
        "",
        "---",
        "",
        "## Payment state machine",
        "",
        f"Terminal states: {', '.join(f'`{s}`' for s in TERMINAL_STATES)}. An event that is "
        "illegal for the state a transaction is in is **quarantined with a reason**, never "
        "silently applied.",
        "",
        _table(
            [[f"`{event}`", ", ".join(f"`{s}`" for s in sources) or "(initial)", f"`{target}`"]
             for event, (sources, target) in TRANSITIONS.items()],
            ["Event", "Legal from", "Resulting state"],
        ),
        "",
        "---",
        "",
        "## Controlled vocabulary",
        "",
    ]

    for dimension in DIMENSIONS:
        parts += [
            f"### `{dimension.key}` - {dimension.label}",
            "",
            dimension.purpose,
            "",
            f"Fallback when a value cannot be resolved: `{dimension.fallback}`.",
            "",
            _table(
                [[
                    f"`{value.name}`",
                    value.definition,
                    value.example,
                    value.boundary,
                    ", ".join(f"`{a}`" for a in value.aliases) or "-",
                ] for value in dimension.values],
                ["Value", "Definition", "Example", "Boundary case", "Aliases accepted"],
            ),
            "",
        ]

    parts += ["---", "", "## Tables", ""]
    for name, columns in [
        ("raw.payment_events", RAW_EVENT_COLUMNS),
        ("core.transactions", TRANSACTION_COLUMNS),
        ("core.merchants", MERCHANT_COLUMNS),
        ("core.wallets", WALLET_COLUMNS),
        ("core.reconciliation_breaks", RECON_BREAK_COLUMNS),
        ("risk.signals", SIGNAL_COLUMNS),
        ("risk.cases", CASE_COLUMNS),
    ]:
        parts += [f"### `{name}`", "", _columns_table(columns), ""]

    parts += [
        "---",
        "",
        "## Scenario catalogue",
        "",
        f"{len(SCENARIOS)} scenario families. `is_actionable` is the ground truth the evaluation "
        "harness scores against; `expected_action` is what the resolution should have been.",
        "",
        f"The population is deliberately **enriched**: about {TARGET_ACTIONABLE_SHARE * 100:.0f}% "
        "of transactions are actionable, against a small fraction of a percent in a real corridor. "
        "Detection metrics move with the base rate and are therefore not comparable to production.",
        "",
        _table(
            [[
                f"`{s.key}`", s.label, "yes" if s.is_actionable else "no",
                f"`{s.expected_action}`", f"{s.share * 100:.2f}%",
                ", ".join(f"`{r}`" for r in s.expected_rules) or "-",
            ] for s in SCENARIOS],
            ["Key", "Story", "Actionable", "Expected action", "Share", "Rules expected to fire"],
        ),
        "",
        "### What each scenario is",
        "",
    ]
    for scenario in SCENARIOS:
        parts += [f"**`{scenario.key}`** — {scenario.story}"]
        if scenario.notes:
            parts += ["", f"> {scenario.notes}"]
        parts += [""]

    return "\n".join(parts) + "\n"


def rule_catalogue() -> str:
    parts = [
        "# Rule catalogue", "", BANNER, "",
        f"Rules version `{RULES_VERSION}` - {len(RULES)} deterministic rules.",
        "",
        "These rules run against **synthetic** data generated by a seeded script. Any firing "
        "count or precision figure quoted for them describes that synthetic population, not a "
        "real payment estate.",
        "",
        "**No language model participates in detection.** Every signal in this product is "
        "produced by the code documented below, is reproducible from the same input, and names "
        "the exact fields it was computed from. That last property is what makes "
        "\"the copilot made an ungrounded claim\" a number rather than an opinion: the copilot "
        "may only cite from these fields.",
        "",
        "## Severity weights",
        "",
        "Severities combine with a noisy-OR, not a sum. Summing would let four low-severity "
        "signals outrank one critical signal, which is exactly backwards.",
        "",
        _table([[severity, f"{weight:.2f}"] for severity, weight in SEVERITY_WEIGHT.items()],
               ["Severity", "Weight"]),
        "",
        "## Rules",
        "",
    ]
    for spec in RULES.values():
        parts += [
            f"### `{spec.rule_id}`",
            "",
            f"**{spec.title}** · family `{spec.family}` · severity `{spec.severity}` "
            f"(weight {spec.weight:.2f})",
            "",
            spec.rationale,
            "",
            "Evidence fields: " + ", ".join(f"`{f}`" for f in spec.evidence_fields),
            "",
        ]
    return "\n".join(parts) + "\n"


def main() -> int:
    docs = PROJECT_ROOT / "docs"
    docs.mkdir(exist_ok=True)
    written = []
    for filename, content in [
        ("DATA_DICTIONARY.md", data_dictionary()),
        ("RULE_CATALOGUE.md", rule_catalogue()),
    ]:
        path = docs / filename
        path.write_text(content, encoding="utf-8")
        written.append(f"{path.relative_to(PROJECT_ROOT)} ({len(content.splitlines())} lines)")
    for line in written:
        print(f"wrote {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
