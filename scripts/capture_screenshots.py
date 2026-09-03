"""Capture the dashboard screenshots used in the README.

The committed PNGs are the deliverable; this script exists so they can be
regenerated after a UI change rather than becoming a set of stale images nobody
dares touch.

    pip install playwright && python -m playwright install chromium
    python -m streamlit run app/Home.py     # in another terminal
    python scripts/capture_screenshots.py

Streamlit renders progressively and its dataframes are canvas-based, so each
page gets a settle wait rather than a `networkidle` check, which never fires on
a page holding a websocket open.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "docs" / "screenshots"
BASE_URL = "http://localhost:8501"
VIEWPORT = {"width": 1600, "height": 1200}

# (stem, path, settle seconds). Each is captured in BOTH languages: the English
# README shows the English interface and the Chinese one shows Chinese, because a
# Chinese page illustrated with English screenshots is exactly the half-finished
# look this project spent effort avoiding elsewhere.
PAGES: list[tuple[str, str, int]] = [
    # The router's default page is served at the root. Its own url_path is NOT a
    # route, and asking for it gets Streamlit's "Page not found" dialog over the
    # content - which shipped in the README once already. The check below refuses
    # to photograph that rather than saving a plausible-looking PNG.
    ("01-aml-operations", "/", 9),
    ("02-aml-alert-queue", "/AML_Alert_Queue", 9),
    ("03-investigation-workbench", "/Investigation_Workbench", 12),
    ("04-overview", "/Overview", 7),
    ("05-case-queue", "/Case_Queue", 7),
    ("06-case-detail", "/Case_Detail", 9),
    ("07-audit-log", "/Audit_Log", 8),
    ("08-transaction-explorer", "/Transaction_Explorer", 7),
    ("09-evaluation", "/Evaluation", 8),
    # The tuning page computes a 36-point policy curve on first load, so it needs
    # longer to settle than the others.
    ("10-policy-tuning", "/Policy_Tuning", 14),
    # A case carrying a seeded conversation that includes a refused request to
    # hand over the decision - the behaviour the screenshot exists to show.
    ("11-followup", "/Case_Detail?case=CASE_0000862", 12),
]

LANGUAGES = ("en", "zh")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed. Run:\n"
              "  pip install playwright && python -m playwright install chromium")
        return 1

    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)
        takes = [
            (f"{stem}.png" if language == "en" else f"{stem}-zh.png",
             f"{path}{'&' if '?' in path else '?'}lang={language}",
             settle)
            for stem, path, settle in PAGES
            for language in LANGUAGES
        ]
        for filename, path, settle in takes:
            url = f"{BASE_URL}{path}"
            print(f"  {filename:<32} {url}")
            page.goto(url, wait_until="load", timeout=60_000)
            page.wait_for_timeout(settle * 1000)
            # Nudge lazy charts into view, then return to the top for the shot.
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(1200)
            page.mouse.wheel(0, -2000)
            page.wait_for_timeout(1200)
            # A screenshot script that silently captures Streamlit's "Page not
            # found" dialog is worse than one that crashes: the PNG looks
            # plausible in a file listing and ships to the README.
            if page.get_by_text("Page not found", exact=False).count():
                raise RuntimeError(f"{url} is not a route (Streamlit: page not found)")
            page.screenshot(path=str(OUTPUT / filename), full_page=False)
        browser.close()

    print(f"\nwrote {len(PAGES)} screenshots to {OUTPUT.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
