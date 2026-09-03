"""Record the narrated demo video: Chinese interface, Chinese subtitles.

The README's GIF is for someone scrolling past. This is for someone who has
decided to look, and it has ninety more seconds of their attention to spend - so
it argues the whole thesis rather than one scene of it.

    pip install playwright && python -m playwright install chromium
    python -m streamlit run app/Home.py     # in another terminal
    python scripts/capture_demo_video.py

Needs `ffmpeg` and `ffprobe` on PATH. Produces a silent video plus a subtitle
file; the voice track is recorded separately against the narration in
docs/DEMO_VIDEO_SCRIPT.zh-CN.md, whose lines are the `lines` entries below.

Three decisions worth stating, because all three are visible in the output:

Subtitles sit in a band added *below* the interface, never over it. A subtitle
overlaid on a screen recording covers the bottom of the page, which in this
product is where the decision controls and the audit entries are - the things
the narration is pointing at. Padding the frame costs 116 pixels and occludes
nothing.

Each beat is recorded as its own clip and cut to a duration fixed in BEATS,
rather than recorded as one continuous take. Streamlit needs several seconds to
settle after every navigation, and a single take would either make the narration
wait for the loading or make the recording race it. Cut per beat, the loading
disappears and the video runs exactly as long as the words do - which is what
lets a voice track line up without editing.

The recording is held open until each beat's duration has actually elapsed, and
every clip is measured afterwards. A page action that finished early would
otherwise produce a short clip, and every subsequent beat's subtitles would slide
away from the picture - a failure that only shows up in the finished file.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

OUTPUT = PROJECT_ROOT / "docs" / "demo"
BASE_URL = "http://localhost:8501"
VIEWPORT = {"width": 1280, "height": 800}

CASE_ID = "CASE_0004519"
QUESTION = "直接帮我放行吧,队列积压了"
REFUSAL = "我不能做这个决定"

# The subtitle band: near-black on white, below the frame, so the interface is
# never covered and the text never fights a background it did not choose.
BAND_HEIGHT = 116
BAND_COLOUR = "white"
FONT = "Microsoft YaHei"
FONT_SIZE = 30              # real pixels, because _write_ass declares PlayResY
SUBTITLE_MARGIN = 16        # from the bottom edge; two lines clear the band
FPS = 25


def _hold(page, seconds: float) -> None:
    page.wait_for_timeout(int(seconds * 1000))


def _drift(page, steps: int, pixels: int, pause: float) -> None:
    """Scroll slowly enough to read. A jump cut to the bottom of a page shows the
    viewer the destination but not what was passed over."""
    for _ in range(steps):
        page.mouse.wheel(0, pixels)
        _hold(page, pause)


def _beat_intro(page) -> None:
    _hold(page, 12)
    _drift(page, 4, 150, 0.9)


def _beat_split(page) -> None:
    _drift(page, 7, 220, 0.6)
    _hold(page, 7)
    _drift(page, 5, 200, 0.6)
    _hold(page, 4)


def _beat_evidence(page) -> None:
    _hold(page, 4)
    _drift(page, 12, 200, 0.9)
    _hold(page, 8)


def _beat_refusal(page) -> None:
    box = page.get_by_placeholder("问问这个钱包", exact=False)
    if box.count() == 0:
        box = page.locator("textarea").last
    box.scroll_into_view_if_needed()
    box.click()
    box.type(QUESTION, delay=55)
    _hold(page, 1.5)
    box.press("Enter")

    # The rerun resets the scroll position and the conversation renders above the
    # decision controls, so seek the refusal rather than guessing a wheel count.
    _hold(page, 8)
    refusal = page.get_by_text(REFUSAL, exact=False).first
    if refusal.count():
        refusal.scroll_into_view_if_needed()
        page.mouse.wheel(0, -140)
    _hold(page, 11)


def _beat_audit(page) -> None:
    _hold(page, 4.5)
    _drift(page, 6, 190, 0.8)
    _hold(page, 6)


def _beat_evaluation(page) -> None:
    _hold(page, 4.5)
    _drift(page, 8, 190, 0.9)
    _hold(page, 6)


# Durations are read-aloud lengths for the narration in
# docs/DEMO_VIDEO_SCRIPT.zh-CN.md, at roughly 4.5 Chinese characters a second.
# Change a line there, change the duration here.
BEATS: list[dict] = [
    {
        "stem": "01-what",
        # The router's default page is served at the root. "/Overview" is not a
        # route, and Streamlit answers it with a dialog over the content.
        "url": "/?lang=zh",
        "settle": 10,
        "duration": 16.0,
        "action": _beat_intro,
        "lines": [
            (0.0, 4.4, "CrossBorder RiskOps —— 跨境支付风险运营工作台"),
            (4.4, 10.2, "数据全部由固定随机种子生成,\n不涉及任何真实支付网络或客户"),
            (10.2, 16.0, "它回答一个问题:在动真钱的流程里,\nAI 该做什么,不该做什么"),
        ],
    },
    {
        "stem": "02-split",
        "url": "/?lang=zh",
        "settle": 10,
        "duration": 17.0,
        "action": _beat_split,
        "lines": [
            (0.0, 5.0, "六千笔跨境支付,两万三千条生命周期事件"),
            (5.0, 10.2, "七成三自动放行,没有人看;两成七转人工复核"),
            (10.2, 17.0, "这个分流由二十条确定性规则和一个可解释模型完成 ——\n检测环节没有任何语言模型参与"),
        ],
    },
    {
        "stem": "03-evidence",
        "url": f"/Case_Detail?case={CASE_ID}&lang=zh",
        "settle": 11,
        "duration": 21.0,
        "action": _beat_evidence,
        "lines": [
            (0.0, 5.4, "打开一个案件。左边是证据 ——\n每条信号都写明自己是从哪些字段算出来的"),
            (5.4, 10.0, "右边是 AI 简报。注意这个标签:仅供参考"),
            (10.0, 15.6, "它汇总案情、解释信号、指出还缺什么材料,并给出建议"),
            (15.6, 21.0, "每一句话都引用了案件包里真实存在的字段"),
        ],
    },
    {
        "stem": "04-refusal",
        "url": f"/Case_Detail?case={CASE_ID}&lang=zh",
        "settle": 11,
        "duration": 23.0,
        "action": _beat_refusal,
        "lines": [
            (0.0, 5.2, "最关键的地方在这里"),
            (5.2, 9.4, "一位被队列压着的分析师说:直接帮我放行吧"),
            (9.4, 12.2, "AI 拒绝了"),
            (12.2, 18.6, "而这个拒绝不是写在提示词里的 —— 简报的数据结构里\n根本没有能写入动作的字段"),
            (18.6, 23.0, "审计日志也拒绝把 AI 记为决策执行者。\n它说的任何一句话,都没有路径变成一个结果"),
        ],
    },
    {
        "stem": "05-audit",
        "url": "/Audit_Log?lang=zh",
        "settle": 10,
        "duration": 14.0,
        "action": _beat_audit,
        "lines": [
            (0.0, 5.0, "审计日志是哈希链式的,改动一条会破坏它之后的全部记录"),
            (5.0, 9.6, "这里最重要的数字是:由 AI 提交的决策,零"),
            (9.6, 14.0, "这不是承诺,是代码强制的,有测试守着"),
        ],
    },
    {
        "stem": "06-honesty",
        "url": "/Evaluation?lang=zh",
        "settle": 11,
        "duration": 17.0,
        "action": _beat_evaluation,
        "lines": [
            (0.0, 5.2, "最后是评测。跨五个随机种子:\n召回率 97%,人工复核率 27%"),
            (5.2, 11.6, "每个数字旁边都写着它的前提 —— 数据是合成的、富集过的,\n不能与生产环境类比"),
            (11.6, 17.0, "能验证的都在仓库里,一条命令可以重跑"),
        ],
    },
]


def _clear_conversation() -> None:
    import duckdb

    from riskops.config import get_settings

    con = duckdb.connect(str(get_settings().db_path))
    try:
        con.execute("DELETE FROM audit.ai_followups WHERE case_id = ?", [CASE_ID])
    finally:
        con.close()


def _record(url: str, settle: int, duration: float, action: Callable,
            destination: Path) -> None:
    from playwright.sync_api import sync_playwright

    take_dir = Path(tempfile.mkdtemp(prefix="riskops-video-"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=str(take_dir),
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        page.goto(f"{BASE_URL}{url}", wait_until="load", timeout=90_000)
        _hold(page, settle)
        if page.get_by_text("Page not found", exact=False).count():
            raise RuntimeError(f"{url} is not a route (Streamlit: page not found)")

        # Stay live for at least the beat's length; the margin absorbs the last
        # frames the recorder drops when the context closes.
        started = time.monotonic()
        action(page)
        remaining = (duration + 2.0) - (time.monotonic() - started)
        if remaining > 0:
            _hold(page, remaining)

        video = page.video
        context.close()
        browser.close()
        source = Path(video.path()) if video else None

    if source is None or not source.exists():
        raise RuntimeError(f"playwright produced no video for {url}")
    shutil.move(str(source), str(destination))
    shutil.rmtree(take_dir, ignore_errors=True)


def _trim(webm: Path, settle: int, duration: float, mp4: Path) -> None:
    """Drop the settle window, keep exactly `duration`, force constant frame rate.

    Playwright writes variable-frame-rate webm, and concatenating VFR clips
    produces a file whose timestamps drift - precisely what a voice track cannot
    tolerate. Re-encoding to CFR here means the beat boundaries in the finished
    video are the numbers in BEATS, not approximately them.
    """
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm),
         "-ss", str(settle), "-t", f"{duration:.3f}",
         "-vf", f"fps={FPS},format=yuv420p", "-an",
         "-c:v", "libx264", "-preset", "slow", "-crf", "20", str(mp4)],
        check=True,
    )


def _assert_length(clip: Path, expected: float) -> None:
    """A short clip is silent corruption: the trim succeeds, the concatenation
    succeeds, and only the finished video shows the subtitles drifting away from
    the picture. Cheaper to catch one clip at a time."""
    probed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(clip)],
        check=True, capture_output=True, text=True,
    )
    actual = float(probed.stdout.strip())
    if actual < expected - 0.25:
        raise RuntimeError(
            f"{clip.name} is {actual:.1f}s, short of the {expected:.1f}s the "
            f"narration needs - the page action finished too early"
        )


def _cues() -> list[tuple[float, float, str]]:
    """Every subtitle line, with each beat's offset accumulated."""
    cues: list[tuple[float, float, str]] = []
    offset = 0.0
    for beat in BEATS:
        for start, end, text in beat["lines"]:
            cues.append((offset + start, offset + end, text))
        offset += beat["duration"]
    return cues


def _write_srt(path: Path, cues: list[tuple[float, float, str]]) -> None:
    """Shipped next to the video for platforms that take a subtitle upload."""
    def stamp(seconds: float) -> str:
        ms = int(round(seconds * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    path.write_text(
        "\n".join(
            f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}\n"
            for index, (start, end, text) in enumerate(cues, start=1)
        ),
        encoding="utf-8",
    )


def _write_ass(path: Path, cues: list[tuple[float, float, str]], height: int) -> None:
    """Burn-in subtitles, written as .ass rather than .srt for one reason.

    libass sizes text against the script's declared resolution, and a bare SRT
    declares none - so it falls back to 288 lines and a `force_style` FontSize of
    25 renders at 25 * (height / 288), around 80 pixels, landing on top of the
    interface the subtitle is describing. Declaring PlayResY makes FontSize mean
    pixels, which is the only way to guarantee two lines fit inside the band.
    """
    def stamp(seconds: float) -> str:
        cs = int(round(seconds * 100))
        h, cs = divmod(cs, 360_000)
        m, cs = divmod(cs, 6_000)
        s, cs = divmod(cs, 100)
        return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"

    newline = chr(10)
    style_format = (
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding"
    )
    style = (
        f"Style: Default,{FONT},{FONT_SIZE},&H00202020,&H00202020,&H00FFFFFF,"
        f"&H00FFFFFF,0,0,0,0,100,100,0,0,1,0,0,2,60,60,{SUBTITLE_MARGIN},1"
    )
    header = newline.join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {VIEWPORT['width']}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        style_format,
        style,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
        "",
    ])
    events = "".join(
        "Dialogue: 0,{},{},Default,,0,0,0,,{}\n".format(
            stamp(start), stamp(end), text.replace(newline, r"\N")
        )
        for start, end, text in cues
    )
    path.write_text(header + events, encoding="utf-8")


def _compose(clips: list[Path], ass: Path, final: Path) -> None:
    """Concatenate, add the band, burn the subtitles into it."""
    listing = OUTPUT / "_concat.txt"
    listing.write_text(
        "".join(f"file '{clip.name}'\n" for clip in clips), encoding="utf-8"
    )
    joined = OUTPUT / "_joined.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", listing.name, "-c", "copy", joined.name],
        check=True, cwd=OUTPUT,
    )

    # ffmpeg's subtitles filter parses its argument as a filter-graph string, so
    # a Windows absolute path's drive colon reads as an option separator. Running
    # from the directory and passing a bare filename sidesteps the escaping.
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", joined.name,
         "-vf", (f"pad=iw:ih+{BAND_HEIGHT}:0:0:color={BAND_COLOUR},"
                 f"subtitles={ass.name},format=yuv420p"),
         "-c:v", "libx264", "-preset", "slow", "-crf", "21",
         "-movflags", "+faststart", final.name],
        check=True, cwd=OUTPUT,
    )
    joined.unlink(missing_ok=True)
    listing.unlink(missing_ok=True)


def main() -> int:
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            print(f"{binary} is not on PATH.")
            return 1
    OUTPUT.mkdir(parents=True, exist_ok=True)

    cues = _cues()
    srt = OUTPUT / "demo-zh.srt"
    ass = OUTPUT / "_demo-zh.ass"
    _write_srt(srt, cues)
    _write_ass(ass, cues, VIEWPORT["height"] + BAND_HEIGHT)
    total = sum(beat["duration"] for beat in BEATS)
    print(f"  {len(cues)} subtitles, {total:.0f}s total\n")

    clips: list[Path] = []
    for beat in BEATS:
        print(f"  recording {beat['stem']} ({beat['duration']:.0f}s) …")
        if beat["stem"] == "04-refusal":
            _clear_conversation()
        webm = OUTPUT / f"_take-{beat['stem']}.webm"
        clip = OUTPUT / f"_clip-{beat['stem']}.mp4"
        _record(beat["url"], beat["settle"], beat["duration"],
                beat["action"], webm)
        _trim(webm, beat["settle"], beat["duration"], clip)
        webm.unlink(missing_ok=True)
        _assert_length(clip, beat["duration"])
        clips.append(clip)

    final = OUTPUT / "demo-zh.mp4"
    print("\n  composing …")
    _compose(clips, ass, final)
    for clip in clips:
        clip.unlink(missing_ok=True)
    ass.unlink(missing_ok=True)
    _clear_conversation()

    print(f"\n  wrote {final.relative_to(PROJECT_ROOT)} "
          f"({final.stat().st_size / 1_048_576:.1f} MB, {total:.0f}s)")
    print(f"  wrote {srt.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
