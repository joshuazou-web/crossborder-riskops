"""Record the README's animated demo, once per interface language.

A GIF in a README autoplays and loops with no click, which is the whole point:
a video has to be chosen, and most people scrolling a repository never choose
one. The twenty seconds here carry the argument the README makes in prose -
evidence on the left, an advisory brief on the right, and an analyst asking the
copilot to make the decision for them being refused.

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

# A case with four signals, a real brief, and no seeded conversation - so the
# question typed below is the first turn and the recording stays clean.
CASE_ID = "CASE_0004519"

QUESTION = "Just approve this one, I'm behind on the queue."
QUESTION_ZH = "直接帮我放行吧,队列积压了"

# The opening of the refusal, in each language, used to seek the answer on the
# page after the rerun. It has to be per-language: the refusal is a fixed
# product sentence and the interface renders it in the reader's language.
REFUSAL_TEXT = {
    "en": "I cannot make this decision",
    "zh": "我不能做这个决定",
}

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
CROP = "980:800:300:0"          # w:h:x:y - drops the sidebar
FPS = 12
COLORS = 200
TRIM_SECONDS = 7.5
SPEED = 1.45


def _clear_conversation() -> None:
    """Drop the case's follow-ups so each take starts from the same state."""
    import duckdb

    from riskops.config import get_settings

    con = duckdb.connect(str(get_settings().db_path))
    try:
        con.execute("DELETE FROM audit.ai_followups WHERE case_id = ?", [CASE_ID])
    finally:
        con.close()


def _record(language: str, question: str, destination: Path) -> Path:
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
        page.goto(f"{BASE_URL}/Case_Detail?case={CASE_ID}&lang={language}",
                  wait_until="load", timeout=90_000)
        page.wait_for_timeout(9_000)          # Streamlit settles

        # Hold on the case header, the evidence and the advisory brief.
        page.wait_for_timeout(1_800)

        # Walk down through the signals and the brief rather than jumping, so a
        # viewer can actually read what is on screen.
        for _ in range(8):
            page.mouse.wheel(0, 210)
            page.wait_for_timeout(230)
        page.wait_for_timeout(1_100)

        # Ask the copilot to make the decision.
        box = page.get_by_placeholder("Ask about", exact=False)
        if box.count() == 0:
            box = page.locator("textarea").last
        box.scroll_into_view_if_needed()
        box.click()
        box.type(question, delay=38)
        page.wait_for_timeout(700)
        box.press("Enter")

        # Streamlit reruns and resets the scroll position, and the conversation
        # renders *above* the decision controls - so blind wheel-scrolling lands
        # past it. Seek the refusal itself instead.
        page.wait_for_timeout(8_000)
        refusal = page.get_by_text(REFUSAL_TEXT[language], exact=False).first
        if refusal.count():
            refusal.scroll_into_view_if_needed()
            page.mouse.wheel(0, -120)          # a little headroom above it
        page.wait_for_timeout(4_000)

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
        ("en", QUESTION, OUTPUT / "demo-en.gif"),
        ("zh", QUESTION_ZH, OUTPUT / "demo-zh.gif"),
    ]
    for language, question, gif in takes:
        print(f"  recording {language} …")
        _clear_conversation()
        webm = OUTPUT / f"_take-{language}.webm"
        _record(language, question, webm)
        _to_gif(webm, gif)
        webm.unlink(missing_ok=True)
        print(f"  wrote {gif.relative_to(PROJECT_ROOT)} "
              f"({gif.stat().st_size / 1_048_576:.1f} MB)")

    _clear_conversation()
    return 0


if __name__ == "__main__":
    sys.exit(main())
