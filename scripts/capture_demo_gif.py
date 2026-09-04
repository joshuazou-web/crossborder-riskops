"""Record the README's animated demo, once per interface language.

A GIF in a README autoplays and loops with no click, which is the whole point:
a video has to be chosen, and most people scrolling a repository never choose
one. The twenty seconds here carry the argument the README makes in prose - an
investigation case whose priority is broken into its eight factors, and then the
evidence beside what would argue against it, rather than above it.

    pip install playwright && python -m playwright install chromium
    python -m streamlit run app/Home.py     # in another terminal
    python scripts/capture_demo_gif.py

Needs `ffmpeg` on PATH for the webm -> GIF conversion, because a palette-aware
conversion is several times smaller than anything Pillow will produce and this
file ships in the repository.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

OUTPUT = PROJECT_ROOT / "docs" / "screenshots"
BASE_URL = "http://localhost:8501"

# The one scene worth twenty autoplaying seconds: the investigation workbench,
# scrolled from the priority breakdown into the evidence-and-counter-evidence
# columns. That layout is the argument the README makes in prose - what would
# argue against the alert sits beside the evidence, not below it - and it is the
# only part of this product that reads at a glance.
VIEWPORT = {"width": 1280, "height": 800}

# Quality decisions, all of them trade-offs against file size:
#
#   CROP    the sidebar is static for the whole clip and unreadable at GIF scale,
#           so it is cut. Removing it buys every remaining pixel back for the
#           content, which is where the text is.
#   scale   none. Cropping instead of downscaling is what keeps the text sharp -
#           a 1280 -> 960 resample turns 12px UI type into grey mush.
#   COLORS  200, not 128. Flat UI panels band visibly below ~192.
#   dither  sierra2_4a, not bayer. Bayer lays an ordered crosshatch over large
#           flat areas, which is most of this interface.
#   TRIM    the first seconds are Streamlit loading. Nobody needs to watch that.
#   SPEED   1.45x. The recording pauses generously so it cannot race ahead of a
#           rerun; the viewer does not have to sit through the same margin.
# The workbench is a busier picture than the case-detail page this shot used to
# use - a plotly chart, two dataframes and several tinted panels - so the same
# settings produced an 8 MB file where the old scene produced 3.8. Colours and
# frame rate both come down, and the crop loses the bottom of the viewport,
# which in this shot is whitespace below the evidence columns.
CROP = "980:700:300:40"         # w:h:x:y - drops the sidebar and the dead margin
FPS = 10
COLORS = 128
TRIM_SECONDS = 8.0
SPEED = 1.5


def _record(language: str, destination: Path) -> Path:
    from playwright.sync_api import sync_playwright

    take_dir = Path(tempfile.mkdtemp(prefix=f"riskops-gif-{language}-"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(take_dir),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        page.goto(f"{BASE_URL}/Investigation_Workbench?lang={language}",
                  wait_until="load", timeout=90_000)
        page.wait_for_timeout(11_000)         # Streamlit settles
        if page.get_by_text("Page not found", exact=False).count():
            raise RuntimeError("Investigation_Workbench is not a route")

        # Park the cursor over the main column. At (0, 0) the wheel scrolls the
        # sidebar and the recording never leaves the top of the page.
        page.mouse.move(900, 420)
        page.wait_for_timeout(1_500)

        # Three stations with a quick move between them, rather than a
        # continuous drift. Continuous scrolling is the worst case for GIF
        # compression - every frame differs from the last, and the first version
        # of this shot came out at 7 MB. Holding still costs almost nothing per
        # frame, and it reads better anyway: a reader gets to actually stop on
        # each of the three things worth seeing.
        stations = (
            (0, 2_800),        # the case summary and why it merged
            (1_800, 3_400),    # the eight-factor priority breakdown
            (1_900, 5_200),    # evidence beside counter-evidence
        )
        for distance, dwell in stations:
            for _ in range(max(1, distance // 300)):
                page.mouse.wheel(0, 300)
                page.wait_for_timeout(90)
            page.wait_for_timeout(dwell)

        video = page.video
        context.close()
        browser.close()
        source = Path(video.path()) if video else None

    if source is None or not source.exists():
        raise RuntimeError("playwright produced no video")
    shutil.move(str(source), str(destination))
    shutil.rmtree(take_dir, ignore_errors=True)
    return destination


def _to_gif(webm: Path, gif: Path) -> None:
    """Two-pass palette conversion: crop, speed up, then quantise once.

    One shared palette across the clip rather than per-frame quantisation - the
    interface barely changes colour, so a single palette is both smaller and
    steadier than a palette that shifts every frame.
    """
    palette = webm.with_suffix(".png")
    common = f"crop={CROP},setpts=PTS/{SPEED},fps={FPS}"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(TRIM_SECONDS), "-i", str(webm),
         "-vf", f"{common},palettegen=max_colors={COLORS}:stats_mode=diff", str(palette)],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(TRIM_SECONDS), "-i", str(webm),
         "-i", str(palette),
         "-lavfi", f"{common}[x];[x][1:v]paletteuse=dither=sierra2_4a:diff_mode=rectangle",
         "-loop", "0", str(gif)],
        check=True,
    )
    palette.unlink(missing_ok=True)


def main() -> int:
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is not on PATH; it is needed for the webm -> GIF conversion.")
        return 1
    OUTPUT.mkdir(parents=True, exist_ok=True)

    takes = [
        ("en", OUTPUT / "demo-en.gif"),
        ("zh", OUTPUT / "demo-zh.gif"),
    ]
    for language, gif in takes:
        print(f"  recording {language} …")
        webm = OUTPUT / f"_take-{language}.webm"
        _record(language, webm)
        _to_gif(webm, gif)
        webm.unlink(missing_ok=True)
        print(f"  wrote {gif.relative_to(PROJECT_ROOT)} "
              f"({gif.stat().st_size / 1_048_576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
