"""Documentation that drifts from the code is worse than no documentation.

Two things are checked here:

  * the generated docs are actually generated from the current code;
  * **every number the README quotes matches what the evaluation harness
    produced**. A portfolio README full of figures nothing can reproduce is the
    exact failure this project is arguing against.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
README = PROJECT_ROOT / "README.md"
EVALUATION_JSON = PROJECT_ROOT / "reports" / "evaluation.json"


@pytest.fixture(scope="module")
def readme() -> str:
    return README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def evaluation() -> dict:
    if not EVALUATION_JSON.exists():
        pytest.skip("run `python -m riskops demo && python -m riskops eval` first")
    return json.loads(EVALUATION_JSON.read_text(encoding="utf-8"))


class TestGeneratedDocs:
    @pytest.mark.parametrize("name", ["DATA_DICTIONARY.md", "RULE_CATALOGUE.md"])
    def test_generated_docs_are_current(self, name):
        import subprocess
        import sys

        path = PROJECT_ROOT / "docs" / name
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "generate_docs.py")],
            cwd=PROJECT_ROOT, check=True, capture_output=True,
        )
        after = path.read_text(encoding="utf-8")
        assert before == after, (
            f"docs/{name} is stale. Run `python scripts/generate_docs.py` and commit the result."
        )

    def test_the_rule_catalogue_documents_every_rule(self):
        from riskops.risk.rules import RULES

        text = (PROJECT_ROOT / "docs" / "RULE_CATALOGUE.md").read_text(encoding="utf-8")
        for rule_id in RULES:
            assert rule_id in text, f"{rule_id} is missing from the rule catalogue"

    def test_the_data_dictionary_documents_every_taxonomy_value(self):
        from riskops.taxonomy import DIMENSIONS

        text = (PROJECT_ROOT / "docs" / "DATA_DICTIONARY.md").read_text(encoding="utf-8")
        for dimension in DIMENSIONS:
            for value in dimension.values:
                assert f"`{value.name}`" in text, f"{value.name} is missing from the dictionary"


class TestReadmeMatchesEvaluation:
    """The README's figures must be the harness's figures."""

    def test_readme_matches_the_evaluation_output(self, readme, evaluation):
        detection = evaluation["risk_detection"]
        ai = evaluation["ai_quality"]
        safety = evaluation["agent_safety"]
        money = evaluation["money_and_state"]
        operations = evaluation["operations"]

        sweep = evaluation.get("seed_sweep")

        # Whichever figure is authoritative is the one the README must quote.
        # Once a spread exists, quoting a single run instead is the mistake this
        # test is here to catch - so the expectation follows the sweep.
        if sweep:
            from riskops.eval.robustness import format_spread
            authoritative = {
                f"{name} (spread)": format_spread(sweep["spread"], metric)
                for name, metric in [
                    ("recall", "recall_pct"),
                    ("precision", "precision_pct"),
                    ("false positive rate", "false_positive_rate_pct"),
                    ("manual review rate", "manual_review_rate_pct"),
                    ("auto release leakage", "auto_release_leakage_pct"),
                    ("actionable base rate", "actionable_base_rate_pct"),
                ]
            }
        else:
            authoritative = {
                "recall": f"{detection['recall_pct']}%",
                "precision": f"{detection['precision_pct']}%",
                "false positive rate": f"{detection['false_positive_rate_pct']}%",
                "manual review rate": f"{detection['manual_review_rate_pct']}%",
                "auto release leakage": f"{detection['auto_release_leakage_pct']}%",
                "actionable base rate": f"{detection['actionable_base_rate_pct']}%",
            }

        expected = {
            **authoritative,
            # The shipped seed's own recall and precision stay quoted too, because
            # the README names it as the worst of the five.
            "shipped-seed recall": f"{detection['recall_pct']}%",
            "shipped-seed precision": f"{detection['precision_pct']}%",
            "briefs scored": f"{ai['briefs_scored']:,}",
            "abstention rate": f"{ai['abstention_rate_pct']}%",
            "ai human agreement": f"{ai['ai_human_agreement_pct']}%",
            "injection detection": f"{safety['input_gate']['detection_pct']:.0f}%",
            "output gate": f"{safety['output_gate']['handled_pct']:.0f}%",
            "settlements recomputed": f"{money['settlements_recomputed']:,}",
            "transactions replayed": f"{money['transactions_replayed']:,}",
            "illegal events": str(money["illegal_events_quarantined"]),
            "cases": f"{operations['cases']:,}",
            "sla breach": f"{operations['sla_breach_pct']}%",
            "false positive recovery": f"{operations['false_positive_recovery_pct']}%",
        }

        baselines = evaluation.get("baselines")
        if baselines:
            contribution = baselines["model_contribution"]
            expected["model recall delta"] = f"{contribution['recall_delta_pct']}"
            expected["model precision delta"] = f"{contribution['precision_delta_pct']}"
            blind = next(
                (row for row in baselines["configurations"]
                 if "blinded" in row["configuration"] or "without rule" in row["configuration"]),
                None,
            )
            if blind:
                expected["rule-blind model recall"] = f"{blind['recall_pct']}%"

        missing = [
            f"{label}: expected {value!r} in the README"
            for label, value in expected.items()
            if value not in readme
        ]
        assert not missing, (
            "The README quotes numbers that do not match reports/evaluation.json:\n  "
            + "\n  ".join(missing)
            + "\nRegenerate with `python -m riskops demo && python -m riskops eval`, then update "
              "the README."
        )

    def test_confusion_matrix_counts_appear(self, readme, evaluation):
        confusion = evaluation["risk_detection"]["confusion"]
        for label, value in confusion.items():
            assert f"{value:,}" in readme or str(value) in readme, (
                f"confusion count {label}={value} is not in the README"
            )

    def test_the_zero_claims_are_actually_zero(self, evaluation):
        # The README states these as zero. If the harness ever reports otherwise,
        # the README becomes a false claim, so fail here rather than there.
        safety = evaluation["agent_safety"]
        assert safety["output_gate"]["unauthorised_recommendations_allowed"] == 0
        assert safety["output_gate"]["pii_leaks"] == 0
        assert safety["authority"]["decisions_committed_by_ai"] == 0
        assert evaluation["money_and_state"]["ledger_over_captures"] == 0
        assert evaluation["money_and_state"]["ledger_over_refunds"] == 0
        assert evaluation["money_and_state"]["ledger_negative_amounts"] == 0
        assert evaluation["money_and_state"]["replay_disagreements"] == 0

    def test_the_seed_and_scale_quoted_in_the_readme_are_the_ones_used(self, readme, evaluation):
        config = evaluation["configuration"]
        assert str(config["seed"]) in readme
        assert f"{config['n_transactions']:,}" in readme
        assert config["as_of_date"] in readme


class TestHonesty:
    """Guard against the claims this project explicitly does not make."""

    FORBIDDEN = [
        r"\bproduction[- ]ready\b",
        r"\bbank[- ]grade\b",
        r"\benterprise[- ]grade\b",
        r"\bmilitary[- ]grade\b",
        r"\bbattle[- ]tested\b",
        r"\bindustry[- ]leading\b",
        r"\bstate[- ]of[- ]the[- ]art\b",
        r"\bfully compliant\b",
        r"\bregulator[- ]approved\b",
    ]

    @pytest.mark.parametrize("path", sorted((PROJECT_ROOT / "docs").glob("*.md")) + [README],
                             ids=lambda p: p.name)
    def test_no_unearned_marketing_claims(self, path):
        text = path.read_text(encoding="utf-8")
        for pattern in self.FORBIDDEN:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                line = text[:match.start()].count("\n") + 1
                # A sentence explicitly disclaiming the phrase is allowed, but only
                # when the disclaimer is right next to it - a "not" sixty words
                # earlier does not make the claim on this line honest.
                context = text[max(0, match.start() - 60): match.end() + 40].lower()
                if any(marker in context for marker in
                       ("not ", "no ", "never", "❌", "without ")):
                    continue
                pytest.fail(f"{path.name}:{line} makes an unearned claim: {match.group(0)!r}")

    @pytest.mark.parametrize("path", sorted((PROJECT_ROOT / "docs").glob("*.md")) + [README],
                             ids=lambda p: p.name)
    def test_synthetic_data_is_declared(self, path):
        text = path.read_text(encoding="utf-8").lower()
        assert "synthetic" in text, (
            f"{path.name} never says the data is synthetic. Every document that shows or "
            "discusses a number has to say where it came from."
        )

    def test_the_readme_declares_synthetic_data_above_the_fold(self, readme):
        head = readme[:1200].lower()
        assert "synthetic" in head
        assert "never run in production" in head or "not affiliated" in head


class TestNoSecretsOrMachinePaths:
    """Nothing machine-specific or secret may be committed."""

    SOURCE_GLOBS = ("src/**/*.py", "app/**/*.py", "tests/**/*.py", "scripts/**/*.py",
                    "*.md", "docs/*.md", "*.toml", "*.txt", ".env.example")

    def _files(self):
        for pattern in self.SOURCE_GLOBS:
            yield from PROJECT_ROOT.glob(pattern)

    def test_no_api_keys(self):
        patterns = [
            r"sk-[A-Za-z0-9]{20,}",
            r"AKIA[0-9A-Z]{16}",
            r"ghp_[A-Za-z0-9]{30,}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        ]
        for path in self._files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in patterns:
                assert not re.search(pattern, text), f"{path.name} looks like it contains a secret"

    def test_no_machine_specific_paths(self):
        patterns = [r"C:\\Users\\[A-Za-z0-9_.]+\\", r"/home/[a-z0-9_.]+/", r"/Users/[a-z0-9_.]+/"]
        offenders = []
        for path in self._files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in patterns:
                if re.search(pattern, text):
                    offenders.append(path.name)
        assert not offenders, f"machine-specific paths found in: {sorted(set(offenders))}"

    def test_no_personal_contact_details(self):
        # An email address in source is almost always an accident. `example.com`
        # and friends are the RFC 2606 reserved documentation domains and are the
        # correct thing to use in a test fixture, so they are allowed.
        reserved = ("example.com", "example.org", "example.net", "invalid", "test")
        for path in PROJECT_ROOT.glob("src/**/*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", text):
                assert match.group(1).lower() in reserved, (
                    f"{path.name} contains a real-looking email address: {match.group(0)}"
                )
