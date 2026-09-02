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
    page_title="CrossBorder RiskOps",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# (module, title, icon, url path). Titles go through `t()` and so change with
# the language; url paths never do, because a link somebody pasted has to keep
# working whatever language they were reading in.
PAGES = [
    ("views/0_Overview.py", "Overview", "📊", "Overview"),
    ("views/1_Transaction_Explorer.py", "Transaction Explorer", "🔎", "Transaction_Explorer"),
    ("views/2_Case_Queue.py", "Case Queue", "📋", "Case_Queue"),
    ("views/3_Case_Detail.py", "Case Detail", "🗂️", "Case_Detail"),
    ("views/4_Audit_Log.py", "Audit Log", "🔐", "Audit_Log"),
    ("views/5_Evaluation.py", "Evaluation", "📐", "Evaluation"),
    ("views/6_Policy_Tuning.py", "Policy Tuning", "🎚️", "Policy_Tuning"),
]

navigation = st.navigation([
    st.Page(path, title=t(title), icon=icon, url_path=url, default=index == 0)
    for index, (path, title, icon, url) in enumerate(PAGES)
])
navigation.run()
