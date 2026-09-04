"""Entry point and navigation.

Streamlit's `pages/` directory builds its sidebar from filenames, which cannot
be translated - so a Chinese interface kept an English navigation, which is the
one place a half-translated product looks broken rather than pragmatic.

`st.navigation` takes an explicit title per page instead, so the sidebar reads
the same language as the rest of the screen. `url_path` is pinned so the routes
survive the change: `/Case_Detail?case=CASE_0000862` still works, and so do the
links in the README and the demo script.
"""

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
for candidate in (str(APP_ROOT), str(APP_ROOT.parent / "src")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import streamlit as st  # noqa: E402

from _i18n import t  # noqa: E402

# Called once, here, because `st.navigation` owns the page from now on and
# calling it again inside a view raises.
st.set_page_config(
    page_title="CrossBorder AML RiskOps",
    page_icon="🛡️",
    layout="wide",
    # "auto", not "expanded": on a 375px screen an expanded sidebar covers the
    # whole page on load, so the first thing a phone user sees is a navigation
    # menu rather than the product. Streamlit collapses it below its own
    # breakpoint and leaves it open on a desktop, which is the behaviour wanted
    # in both places.
    initial_sidebar_state="auto",
)

# (module, title, icon, url path). Titles go through `t()` and so change with
# the language; url paths never do, because a link somebody pasted has to keep
# working whatever language they were reading in.
# Grouped, because there are now two products in one warehouse: the payment
# anomaly side this started as, and the AML monitoring layer built on top of it.
# A flat list of ten pages would make a reader guess which ones belong together.
PAGE_GROUPS: dict[str, list[tuple[str, str, str, str]]] = {
    "Cross-border AML": [
        ("views/9_AML_Operations.py", "AML Operations", "🌐", "AML_Operations"),
        ("views/7_AML_Alert_Queue.py", "AML Alert Queue", "🚩", "AML_Alert_Queue"),
        ("views/8_Investigation_Workbench.py", "Investigation Workbench", "🔬",
         "Investigation_Workbench"),
        ("views/10_AML_Evaluation.py", "AML Evaluation", "📏", "AML_Evaluation"),
    ],
    "Payment risk operations": [
        ("views/0_Overview.py", "Overview", "📊", "Overview"),
        ("views/1_Transaction_Explorer.py", "Transaction Explorer", "🔎",
         "Transaction_Explorer"),
        ("views/2_Case_Queue.py", "Case Queue", "📋", "Case_Queue"),
        ("views/3_Case_Detail.py", "Case Detail", "🗂️", "Case_Detail"),
    ],
    "Evidence and evaluation": [
        ("views/4_Audit_Log.py", "Audit Log", "🔐", "Audit_Log"),
        ("views/5_Evaluation.py", "Evaluation", "📐", "Evaluation"),
        ("views/6_Policy_Tuning.py", "Policy Tuning", "🎚️", "Policy_Tuning"),
    ],
}

# Flat list kept for the tests and the capture scripts, which need to know every
# route without reconstructing the grouping.
PAGES = [page for group in PAGE_GROUPS.values() for page in group]

_first = PAGES[0][0]
navigation = st.navigation({
    t(group): [
        st.Page(path, title=t(title), icon=icon, url_path=url, default=path == _first)
        for path, title, icon, url in pages
    ]
    for group, pages in PAGE_GROUPS.items()
})
navigation.run()
