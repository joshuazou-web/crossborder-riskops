"""The bilingual layer.

The trap in any translation table is drift: a string changes in the page, the
key in the table does not, and the interface quietly falls back to English on
exactly the sentence somebody needed translated. So these tests read the pages
and check the table against them, rather than checking the table against itself.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP = PROJECT_ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from _i18n import DEFAULT_LANGUAGE, LANGUAGES, ZH  # noqa: E402
from riskops.ai.conversation import DELEGATION_REFUSAL  # noqa: E402

PAGE_FILES = sorted([APP / "Home.py", APP / "_shared.py", *(APP / "views").glob("*.py")])


def _t_arguments(path: Path) -> list[str]:
    """Every literal string passed to `t(...)` in one file.

    Parsed rather than regexed, so implicit concatenation across lines - which
    almost every long caption uses - resolves to the same string the running
    page will pass.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id == "t" and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                found.append(argument.value)
    return found


ALL_ARGUMENTS = {path.name: _t_arguments(path) for path in PAGE_FILES}


class TestTable:
    def test_english_is_the_default(self):
        assert DEFAULT_LANGUAGE == "en"
        assert set(LANGUAGES) == {"en", "zh"}

    def test_no_translation_is_empty(self):
        for source, translated in ZH.items():
            assert translated.strip(), f"empty translation for {source[:50]!r}"

    def test_no_translation_is_just_the_english_back(self):
        for source, translated in ZH.items():
            assert translated != source, f"untranslated entry: {source[:50]!r}"

    def test_every_translation_contains_chinese(self):
        # A "translation" with no CJK character is a copy-paste that got missed.
        cjk = re.compile(r"[一-鿿]")
        for source, translated in ZH.items():
            assert cjk.search(translated), f"no Chinese in the translation of {source[:50]!r}"

    def test_identifiers_are_not_translated(self):
        # Case ids, rule ids and citation keys must read identically in both
        # languages, or a screenshot in one language cannot be matched to a log
        # line in the other.
        forbidden = re.compile(r"(CASE_\d|TXN_\d|R\d{3}_[A-Z]|txn\.|wallet_ctx\.)")
        for source, translated in ZH.items():
            for text in (source, translated):
                assert not forbidden.search(text) or "R301_GEO_MISMATCH" in text, (
                    f"an identifier appears inside a translatable string: {text[:60]!r}"
                )


def _navigation_titles() -> list[str]:
    """The sidebar entries, read out of the PAGES table in the router.

    These are the strings Streamlit's `pages/` directory could not translate at
    all, because it builds the sidebar from filenames. They are worth checking
    separately: the navigation is the first thing anyone sees, and it is the one
    place a half-translated interface reads as broken rather than pragmatic.
    """
    tree = ast.parse((APP / "Home.py").read_text(encoding="utf-8"))
    titles: list[str] = []
    for node in ast.walk(tree):
        # PAGE_GROUPS: {group name: [(module, title, icon, url), ...]}. The group
        # names are sidebar headings and need translating too. It carries a type
        # annotation, so it parses as AnnAssign rather than Assign - matching
        # only Assign silently found nothing and made this whole check vacuous.
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        if not any(getattr(target, "id", "") == "PAGE_GROUPS" for target in targets):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=True):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                titles.append(key.value)
            if not isinstance(value, ast.List):
                continue
            for entry in value.elts:
                if isinstance(entry, ast.Tuple) and len(entry.elts) >= 2:
                    title = entry.elts[1]
                    if isinstance(title, ast.Constant) and isinstance(title.value, str):
                        titles.append(title.value)
    return titles


NAVIGATION_TITLES = _navigation_titles()


class TestNavigation:
    def test_the_router_declares_every_page(self):
        # One view file, one navigation entry. A view nobody can navigate to is
        # dead, and an entry with no view behind it is a broken link.
        import sys

        sys.path.insert(0, str(APP))
        view_files = sorted((APP / "views").glob("*.py"))
        # NAVIGATION_TITLES carries the group headings as well as the pages, so
        # count the page entries from the router's own flattened list.
        text = (APP / "Home.py").read_text(encoding="utf-8")
        entries = text.count('("views/')
        assert entries == len(view_files), (
            f"{entries} navigation entries against {len(view_files)} view files"
        )

    @pytest.mark.parametrize("title", NAVIGATION_TITLES)
    def test_every_sidebar_entry_is_translated(self, title):
        assert title in ZH, (
            f"the sidebar entry {title!r} has no Chinese translation, so the navigation "
            "stays English on an otherwise Chinese screen"
        )

    def test_url_paths_are_not_translated(self):
        # A link somebody pasted has to keep working whatever language they were
        # reading in, so the route never changes with the interface.
        text = (APP / "Home.py").read_text(encoding="utf-8")
        for route in ("Transaction_Explorer", "Case_Queue", "Case_Detail",
                      "Audit_Log", "Evaluation", "Policy_Tuning"):
            assert f'"{route}"' in text, f"route {route} is missing from the router"


class TestCoverage:
    def test_every_page_uses_the_translation_helper(self):
        for path in PAGE_FILES:
            # The router passes `t(title)` a variable rather than a literal, so
            # its coverage is checked by TestNavigation instead.
            if path.name in ("_i18n.py", "Home.py"):
                continue
            assert ALL_ARGUMENTS[path.name], f"{path.name} has no translated strings"

    @pytest.mark.parametrize("name", [p.name for p in PAGE_FILES])
    def test_no_string_is_wrapped_without_a_translation(self, name):
        # Wrapping a string in t() is a promise that it is translatable. A
        # wrapped string with no entry renders English inside an otherwise
        # Chinese screen, which reads as a bug rather than as a fallback.
        missing = [text for text in ALL_ARGUMENTS[name] if text not in ZH]
        assert not missing, (
            f"{name} wraps {len(missing)} string(s) with no Chinese translation:\n  "
            + "\n  ".join(repr(text[:70]) for text in missing)
        )

    def test_the_table_has_no_entries_nothing_uses(self):
        # A stale entry is a string that changed in a page and was left behind
        # in the table, which is the drift this whole file exists to catch.
        used = {text for arguments in ALL_ARGUMENTS.values() for text in arguments}
        # Keys used through `format_func=t` on option lists rather than a direct
        # call are legitimate and cannot be seen by the AST walk.
        # Strings a page renders through a variable - `t(typology.title)`,
        # `.map(t)` over a column, `format_func=t` on an option list - are
        # invisible to an AST walk over `t("literal")` calls. Rather than keep a
        # hand-written exemption list that rots, they are derived from the
        # tables they actually come from.
        from riskops.aml.aggregate import CASE_STATES
        from riskops.aml.investigation import DISPOSITIONS, INVESTIGATOR_ROLES
        from riskops.aml.typology import TYPOLOGIES

        indirect = {
            # Passed through `format_func=t` on an option list rather than called
            # directly, so the AST walk cannot see them.
            "Open only", "All cases", "Breached SLA", "Appealed",
            # Used inside _i18n.py itself, which is not a page.
            "Language",
            # The delegation refusal reaches `t()` as `turn.answer`, a variable,
            # so the AST walk cannot see it. Asserted by name below instead.
            DELEGATION_REFUSAL,
        }
        indirect |= set(NAVIGATION_TITLES)
        for typology in TYPOLOGIES:
            indirect |= {typology.title, typology.question,
                         *typology.counter_evidence_hints}
        for disposition in DISPOSITIONS:
            indirect |= {disposition.key, disposition.title,
                         disposition.description, disposition.next_state}
        indirect |= set(CASE_STATES) | set(INVESTIGATOR_ROLES)
        # Priority factor labels: read back out of a stored string by
        # parse_contributions, so they reach t() as data.
        from riskops.aml.evaluate import DATASET_CAVEAT
        from riskops.aml.priority import FACTOR_LABELS

        indirect |= set(FACTOR_LABELS.values())
        # Reaches t() through the report dictionary the evaluation page reads.
        indirect.add(DATASET_CAVEAT)
        # Column values rendered with `.map(t)`: enumerations from the data
        # dictionary, not page copy.
        indirect |= {
            "individual", "business", "personal", "verified", "unverified",
            "not_required", "complete", "partial", "missing",
            "critical", "high", "medium", "low",
        }
        stale = sorted(set(ZH) - used - indirect)
        assert not stale, (
            f"{len(stale)} translation(s) no page uses any more:\n  "
            + "\n  ".join(repr(text[:70]) for text in stale)
        )


class TestCriticalStrings:
    """Some sentences must never silently fall back to English."""

    CRITICAL = [
        "Synthetic data · simulated environment.",
        "**AI boundary.** The copilot organises evidence and *recommends*. "
        "It cannot release, hold, refund or close anything. Those are committed by a person "
        "or by the deterministic policy, and the audit log refuses to record an AI actor on a "
        "decision.",
        "The brief answers the first question. This answers the second — and it gets a **wider** "
        "packet than the brief did: this wallet's and merchant's history, the payout group, and "
        "the decision trail. Same rules apply: every answer cites the fields it used, an answer "
        "that cannot be grounded is withheld rather than guessed, and **a question that asks the "
        "copilot to decide is refused, not answered.**",
    ]

    def test_the_delegation_refusal_is_translated(self):
        # The single most important sentence the product says. A Chinese analyst
        # being refused in English is the one place this interface could look
        # like it was bolted on.
        assert DELEGATION_REFUSAL in ZH

    @pytest.mark.parametrize("text", CRITICAL)
    def test_the_honesty_and_boundary_notices_are_translated(self, text):
        # These three carry the product's central claims. A reviewer reading the
        # Chinese interface must get the claims, not the labels around them.
        assert text in ZH, f"not translated: {text[:60]!r}"


class TestReasonTranslation:
    """The guardrails speak canonical English into the audit log; the interface
    has to speak the reader's language. `translate_reason` is the seam."""

    @pytest.mark.parametrize(
        ("reason", "must_survive"),
        [
            ("delegation of the decision was refused", []),
            (
                "the question asked the copilot to make or take the decision: "
                "asked_ai_to_act",
                ["asked_ai_to_act"],
            ),
            ("4 PII-shaped string(s) redacted before display", ["4"]),
            (
                "confidence 0.31 is below the 0.55 floor; recommendation "
                "downgraded to abstain",
                ["0.31", "0.55"],
            ),
            ("provider degraded: connection refused", ["connection refused"]),
        ],
    )
    def test_translated_and_the_numbers_survive(
        self, reason: str, must_survive: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The counts, thresholds and machine codes inside a reason have to pass
        through untouched - they are the part a reviewer acts on."""
        import _i18n

        monkeypatch.setattr(_i18n, "current_language", lambda: "zh")
        translated = _i18n.translate_reason(reason)
        assert translated != reason, f"not translated: {reason}"
        for fragment in must_survive:
            assert fragment in translated, (
                f"{fragment!r} was lost in translation: {translated}"
            )

    def test_english_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import _i18n

        monkeypatch.setattr(_i18n, "current_language", lambda: "en")
        reason = "delegation of the decision was refused"
        assert _i18n.translate_reason(reason) == reason

    def test_unknown_reason_falls_through_rather_than_vanishing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A diagnostic the reader cannot understand beats one that disappeared."""
        import _i18n

        monkeypatch.setattr(_i18n, "current_language", lambda: "zh")
        assert _i18n.translate_reason("not a reason anyone translated") == (
            "not a reason anyone translated"
        )


class TestPriorityFactorLabels:
    """The eight priority factors reach `t()` as data, not as literals.

    They are read back out of a stored string by `parse_contributions`, so the
    AST coverage walk cannot see them. Left unchecked, seven of the eight sat
    untranslated on the Chinese workbench while the eighth happened to already
    be in the table - which is exactly the half-finished look this suite exists
    to prevent.
    """

    def test_every_factor_label_is_translated(self) -> None:
        from riskops.aml.priority import FACTOR_LABELS

        missing = sorted(label for label in FACTOR_LABELS.values() if label not in ZH)
        assert not missing, f"priority factor labels with no Chinese: {missing}"

    def test_every_weighted_factor_has_a_label(self) -> None:
        from riskops.aml.priority import FACTOR_LABELS, WEIGHTS

        assert set(WEIGHTS) == set(FACTOR_LABELS)


class TestChineseMarkdown:
    """Bold has to actually render, which needs a boundary CJK does not give it.

    `**加粗**它` renders the asterisks literally: markdown wants whitespace or
    punctuation after a closing delimiter, and a Chinese character is neither.
    Four strings shipped this way before the check existed, including the
    capacity-line notice - the one sentence on the alert queue that most needed
    emphasis.
    """

    @staticmethod
    def _unrendered_bold(value: str) -> list[str]:
        """Bold runs whose closing delimiter is followed by a CJK ideograph.

        Delimiters are paired left to right rather than matched with a regex. A
        pattern like ``\\*\\*[^*]+\\*\\*`` cannot tell the end of one bold run from
        the start of the next, so on a string containing two of them it matches
        the gap between and reports a problem that is not there - which is
        exactly what happened when this check was first written, and it took a
        correct translation with it.

        A full-width colon, comma or full stop is Unicode punctuation and closes
        a run correctly. Only an ideograph does not.
        """
        positions = [i for i in range(len(value) - 1) if value[i:i + 2] == "**"]
        broken = []
        for opening, closing in zip(positions[::2], positions[1::2], strict=False):
            after = value[closing + 2: closing + 3]
            if after and "\u4e00" <= after <= "\u9fff":
                broken.append(value[opening:closing + 3])
        return broken

    def test_bold_is_followed_by_a_boundary(self) -> None:
        broken = [
            run for value in ZH.values() for run in self._unrendered_bold(value)
        ]
        assert not broken, (
            "these bold runs are followed directly by a Chinese character, so markdown "
            f"renders the asterisks instead: {broken}"
        )

    def test_bold_delimiters_are_balanced(self) -> None:
        unbalanced = [
            value[:60] for value in ZH.values() if value.count("**") % 2
        ]
        assert not unbalanced, f"odd number of ** in: {unbalanced}"
