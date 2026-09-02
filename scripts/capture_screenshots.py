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

# (filename, path, settle seconds, optional case to preselect)
PAGES: list[tuple[str, str, int]] = [
    ("01-overview.png", "/", 7),
    ("02-case-queue.png", "/Case_Queue", 7),
    ("03-case-detail.png", "/Case_Detail", 9),
    ("04-audit-log.png", "/Audit_Log", 8),
    ("05-transaction-explorer.png", "/Transaction_Explorer", 7),
    ("06-evaluation.png", "/Evaluation", 8),
    # The tuning page computes a 36-point policy curve on first load, so it needs
    # longer to settle than the others.
    ("07-policy-tuning.png", "/Policy_Tuning", 14),
]


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
        for filename, path, settle in PAGES:
            url = f"{BASE_URL}{path}"
            print(f"  {filename:<32} {url}")
            page.goto(url, wait_until="load", timeout=60_000)
            page.wait_for_timeout(settle * 1000)
            # Nudge lazy charts into view, then return to the top for the shot.
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(1200)
            page.mouse.wheel(0, -2000)
            page.wait_for_timeout(1200)
            page.screenshot(path=str(OUTPUT / filename), full_page=False)
        browser.close()

    print(f"\nwrote {len(PAGES)} screenshots to {OUTPUT.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
