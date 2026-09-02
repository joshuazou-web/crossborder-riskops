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

PAGE_FILES = sorted([APP / "Home.py", APP / "_shared.py", *(APP / "pages").glob("*.py")])


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


class TestCoverage:
    def test_every_page_uses_the_translation_helper(self):
        for path in PAGE_FILES:
            if path.name in ("_i18n.py",):
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
        indirect = {
            # Passed through `format_func=t` on an option list rather than called
            # directly, so the AST walk cannot see them.
            "Open only", "All cases", "Breached SLA", "Appealed",
            # Used inside _i18n.py itself, which is not a page.
            "Language",
            # Streamlit derives the sidebar entry from the filename, so the page
            # names are translated only where they appear in body text.
            "Evaluation",
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

    @pytest.mark.parametrize("text", CRITICAL)
    def test_the_honesty_and_boundary_notices_are_translated(self, text):
        # These three carry the product's central claims. A reviewer reading the
        # Chinese interface must get the claims, not the labels around them.
        assert text in ZH, f"not translated: {text[:60]!r}"
