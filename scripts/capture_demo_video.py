"""Record the narrated demo video: Chinese interface, Chinese subtitles.

The README's GIF is for someone scrolling past. This is for someone who has
decided to look, and it is written for a recruiter rather than an engineer -
which changes what belongs in it. A recruiter is deciding whether to pass this
to someone technical, so the two minutes carry the problem, the scale, the
product judgement, one thing worth remembering, and a way to check the claims.
The import graph and the threshold calibration are in the repository for whoever
they hand it to.

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

CASE_ID_HINT = "the highest-priority case, whichever the seed produces"

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
    viewer the destination but not what was passed over.

    The cursor is parked over the main column first. Playwright starts it at
    (0, 0), which on this layout is inside the sidebar - so the wheel scrolled
    the navigation instead of the page, and the recording sat at the top of the
    workbench while the narration described the bottom of it.
    """
    page.mouse.move(900, 420)
    for _ in range(steps):
        page.mouse.wheel(0, pixels)
        _hold(page, pause)


def _beat_what(page) -> None:
    _hold(page, 11)
    _drift(page, 4, 150, 0.9)


def _beat_funnel(page) -> None:
    _drift(page, 6, 210, 0.6)
    _hold(page, 8)
    _drift(page, 3, 180, 0.6)
    _hold(page, 4)


def _beat_queue(page) -> None:
    _hold(page, 5)
    _drift(page, 5, 200, 0.8)
    _hold(page, 7)


def _beat_workbench(page) -> None:
    _hold(page, 4)
    _drift(page, 6, 200, 0.9)
    _hold(page, 6)
    _drift(page, 8, 200, 0.9)
    _hold(page, 6)


def _beat_boundary(page) -> None:
    # Seek the disposition control rather than guessing a wheel count: the page
    # length changes with the case the seed produced.
    for marker in ("记录调查结论", "Record an investigation decision"):
        target = page.get_by_text(marker, exact=False).first
        if target.count():
            target.scroll_into_view_if_needed()
            page.mouse.wheel(0, -160)
            break
    _hold(page, 20)


def _beat_numbers(page) -> None:
    _hold(page, 5)
    _drift(page, 5, 200, 0.9)
    _hold(page, 8)
    _drift(page, 4, 180, 0.8)
    _hold(page, 5)


# Durations are read-aloud lengths for the narration in
# docs/DEMO_VIDEO_SCRIPT.zh-CN.md, at roughly 4.5 Chinese characters a second.
# Change a line there, change the duration here.
#
# Written for a recruiter rather than an engineer. What survives that audience is
# the problem, the scale, the product judgement, one thing worth remembering, and
# a way to check it - not the import graph or the calibration method.
BEATS: list[dict] = [
    {
        "stem": "01-what",
        # The router's default page is served at the root. Its own url_path is
        # not a route, and Streamlit answers it with a dialog over the content.
        "url": "/?lang=zh",
        "settle": 11,
        "duration": 17.0,
        "action": _beat_what,
        "lines": [
            (0.0, 4.6, "跨境支付反洗钱预警调查与风险运营平台"),
            (4.6, 10.4, "数据全部由固定随机种子生成,\n不涉及任何真实机构、客户或交易"),
            (10.4, 17.0, "它要解决的问题很具体:\n预警远多于人手,今天该看哪一批?"),
        ],
    },
    {
        "stem": "02-funnel",
        "url": "/?lang=zh",
        "settle": 11,
        "duration": 19.0,
        "action": _beat_funnel,
        "lines": [
            (0.0, 5.4, "四万笔跨境转账,一千六百多次原始预警触发"),
            (5.4, 11.0, "去重后九百五十七条,聚合成六百九十三个案件"),
            (11.0, 19.0, "而一个这个规模的团队,今天只打得开两百四十二个。\n这个漏斗就是产品本身"),
        ],
    },
    {
        "stem": "03-queue",
        "url": "/AML_Alert_Queue?lang=zh",
        "settle": 11,
        "duration": 18.0,
        "action": _beat_queue,
        "lines": [
            (0.0, 5.0, "队列按调查优先级排序,在复核容量处切一刀"),
            (5.0, 12.0, "注意这句话:容量线以下的案件不等于已排除风险,\n它只是没有被人看过"),
            (12.0, 18.0, "评测报告里有专门一行,\n报告有多少风险模式正躺在这个积压里"),
        ],
    },
    {
        "stem": "04-workbench",
        "url": "/Investigation_Workbench?lang=zh",
        "settle": 12,
        "duration": 25.0,
        "action": _beat_workbench,
        "lines": [
            (0.0, 6.2, "打开优先级最高的案件。\n三条相互独立的风险模式同时指向这一个账户"),
            (6.2, 12.4, "优先级由八个因子构成,每个因子的贡献都摊开写着 ——\n不认同排序的人,能看到是哪一项把它顶上来的"),
            (12.4, 19.0, "左边是证据。右边是:有什么会推翻它"),
            (19.0, 25.0, "并排,不是上下。\n因为被队列压着的复核人自上而下读,而且会提前停"),
        ],
    },
    {
        "stem": "05-boundary",
        "url": "/Investigation_Workbench?lang=zh",
        "settle": 12,
        "duration": 20.0,
        "action": _beat_boundary,
        "lines": [
            (0.0, 5.6, "处置只有五种:结案、监测、补材料、强化复核、升级"),
            (5.6, 12.4, "没有任何一项叫「认定洗钱」「已上报」或「冻结账户」——\n不是灰掉,是根本不存在"),
            (12.4, 20.0, "AI 不能记录任何处置结论。\n而且整个检测闭环里,一次模型调用都没有"),
        ],
    },
    {
        "stem": "06-numbers",
        "url": "/AML_Evaluation?lang=zh",
        "settle": 12,
        "duration": 23.0,
        "action": _beat_numbers,
        "lines": [
            (0.0, 6.4, "原始预警精确率百分之二十二,\n复核容量内百分之八十七"),
            (6.4, 11.0, "这个差,就是排序创造的价值"),
            (11.0, 18.4, "而我自己最先去查的是这一行:\n故意造在阈值之外的场景,只召回了百分之十六"),
            (18.4, 23.0, "如果这个数字高,说明检测器在打噪声,\n上面所有指标都不值钱"),
        ],
    },
]


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

    print(f"\n  wrote {final.relative_to(PROJECT_ROOT)} "
          f"({final.stat().st_size / 1_048_576:.1f} MB, {total:.0f}s)")
    print(f"  wrote {srt.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
