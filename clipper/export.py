"""导出：三种预设（横屏原版 / 竖屏烧字幕 / 竖屏无字幕），统一规格，便于合集直接拼接。"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path

from . import tools

FPS = "24000/1001"            # 用户定 24（片源多为 23.976，按原帧率不重采样）
PRESETS = {
    "横屏原版": dict(w=1280, h=720, burn=False, vertical=False),
    "竖屏烧字幕": dict(w=720, h=1280, burn=True, vertical=True),
    "竖屏无字幕": dict(w=720, h=1280, burn=False, vertical=True),
}


def _default_font():
    from .paths import fonts_dir
    if (fonts_dir() / "NotoSansSC-Regular.otf").exists():
        return "Noto Sans SC"          # 软件自带字体，Win/Mac 烧出来一模一样
    sysname = platform.system()
    if sysname == "Darwin":
        return "PingFang SC"
    if sysname == "Windows":
        return "Microsoft YaHei"
    return "WenQuanYi Zen Hei"


@dataclass
class Style:
    font: str = field(default_factory=_default_font)
    en_size: int = 44
    zh_size: int = 38
    en_color: str = "#FFFFFF"
    zh_color: str = "#FFE082"
    highlight: str = "#FFC107"     # 命中句型的词
    highlight_on: bool = True
    outline: int = 2
    outline_color: str = "#000000"
    bold_en: bool = True
    bg_color: str = "#000000"      # 竖屏上下空白的底色
    title_on: bool = True          # 竖屏顶部显示句型标题
    title_size: int = 48
    title_color: str = "#FFFFFF"
    source_on: bool = True         # 标题下小字显示剧名季集
    source_size: int = 26
    source_color: str = "#B0BEC5"
    gap: int = 36                  # 字幕离画面的距离

    @classmethod
    def load(cls, d: dict | None):
        s = cls()
        for k, v in (d or {}).items():
            if hasattr(s, k):
                setattr(s, k, v)
        return s


@dataclass
class Padding:
    pre: float = 0.30
    post: float = 0.35
    fade: float = 0.05


def _ass_color(hex_):
    h = hex_.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def _ass_time(t):
    cs = max(0, int(round(t * 100)))
    return f"{cs//360000}:{cs//6000%60:02d}:{cs//100%60:02d}.{cs%100:02d}"


def _esc(s: str) -> str:
    return s.replace("\\", "＼").replace("{", "（").replace("}", "）").replace("\n", " ")


def _highlight(en: str, matched: str, st: Style) -> str:
    text = _esc(en)
    if not (st.highlight_on and matched):
        return text
    words = matched.split()
    if not words:
        return text
    pat = r"\b" + r"[\W_]+".join(re.escape(w) for w in words) + r"\b"
    m = re.search(pat, text, re.I)
    if not m:
        return text
    hc = _ass_color(st.highlight)
    ec = _ass_color(st.en_color)
    return text[: m.start()] + "{\\c" + hc + "}" + m.group(0) + "{\\c" + ec + "}" + text[m.end():]


def make_ass(path: Path, dur: float, video_h: int, en: str, zh: str, matched: str, title: str,
             source: str, st: Style, burn: bool, W=720, H=1280):
    top = (H - video_h) // 2
    bottom = top + video_h
    oc = _ass_color(st.outline_color)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: EN,{st.font},{st.en_size},{_ass_color(st.en_color)},&H000000FF,{oc},&H00000000,"
        f"{-1 if st.bold_en else 0},0,0,0,100,100,0,0,1,{st.outline},0,8,40,40,0,1",
        f"Style: ZH,{st.font},{st.zh_size},{_ass_color(st.zh_color)},&H000000FF,{oc},&H00000000,0,0,0,0,100,100,0,0,1,"
        f"{st.outline},0,8,40,40,0,1",
        f"Style: TITLE,{st.font},{st.title_size},{_ass_color(st.title_color)},&H000000FF,{oc},&H00000000,-1,0,0,0,"
        f"100,100,0,0,1,{st.outline},0,2,40,40,0,1",
        f"Style: SRC,{st.font},{st.source_size},{_ass_color(st.source_color)},&H000000FF,{oc},&H00000000,0,0,0,0,"
        f"100,100,0,0,1,1,0,2,40,40,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    a, z = _ass_time(0), _ass_time(dur + 1)
    if st.title_on and title:
        y_src = top - st.gap
        if st.source_on and source:
            lines.append(f"Dialogue: 0,{a},{z},SRC,,0,0,0,,{{\\pos({W//2},{y_src})}}{_esc(source)}")
            y_title = y_src - st.source_size - 14
        else:
            y_title = y_src
        lines.append(f"Dialogue: 0,{a},{z},TITLE,,0,0,0,,{{\\pos({W//2},{y_title})}}{_esc(title)}")
    if burn:
        y = bottom + st.gap
        lines.append(f"Dialogue: 0,{a},{z},EN,,0,0,0,,{{\\pos({W//2},{y})}}{_highlight(en, matched, st)}")
        if zh:
            # 英文可能折成两行，中文放在英文下面足够远的位置
            est_lines = max(1, int(len(en) * st.en_size * 0.5 / (W - 80)) + 1)
            y2 = y + est_lines * int(st.en_size * 1.25) + 14
            lines.append(f"Dialogue: 0,{a},{z},ZH,,0,0,0,,{{\\pos({W//2},{y2})}}{_esc(zh)}")
    path.write_text("\n".join(lines) + "\n", "utf-8")


def _fontsdir_arg() -> str:
    from .paths import fonts_dir
    d = fonts_dir()
    if not d.exists():
        return ""
    p = str(d.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "")
    return f":fontsdir='{p}'"


def _even(x):
    return int(x) // 2 * 2


def export_clip(video: Path, start: float, end: float, out: Path, preset: str, style: Style,
                en="", zh="", matched="", title="", source="", pad: Padding | None = None,
                src_w=None, src_h=None) -> Path:
    """start/end 已经是最终切点（含余量）。"""
    p = PRESETS[preset]
    pad = pad or Padding()
    video = Path(video).resolve()
    dur = max(0.2, end - start)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not (src_w and src_h):
        src_w, src_h = tools.video_size(video)
    W, H = p["w"], p["h"]
    if p["vertical"]:
        vw = W
        vh = _even(src_h * W / src_w)
        vh = min(vh, H)
    else:
        scale = min(W / src_w, H / src_h)
        vw, vh = _even(src_w * scale), _even(src_h * scale)
    bg = style.bg_color.replace("#", "0x")
    vf = (f"scale={vw}:{vh}:flags=lanczos,setsar=1,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={bg},"
          f"fps={FPS},format=yuv420p")
    tmp = Path(tempfile.mkdtemp(prefix="clip_"))
    try:
        if p["vertical"] and (p["burn"] or style.title_on):
            make_ass(tmp / "s.ass", dur, vh, en, zh, matched, title, source, style, p["burn"], W, H)
            vf += ",subtitles=s.ass" + _fontsdir_arg()
        fo = max(0.0, dur - pad.fade - 0.03)
        af = (f"aresample=48000,aformat=channel_layouts=stereo,afade=t=in:d={pad.fade},"
              f"afade=t=out:st={fo:.3f}:d={pad.fade + 0.03:.3f},loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000")
        part = tmp / "out.mp4"
        cmd = [tools.ffmpeg(), "-nostdin", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(video),
               "-t", f"{dur:.3f}", "-vf", vf, "-af", af,
               "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high", "-g", "24",
               "-keyint_min", "24", "-sc_threshold", "0", "-r", FPS,
               "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
               "-movflags", "+faststart", "-map_metadata", "-1", str(part)]
        r = tools.run(cmd, cwd=str(tmp))
        if r.returncode:
            raise RuntimeError(r.stderr.strip()[-600:])
        shutil.move(str(part), str(out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def concat(files: list[Path], out: Path) -> Path:
    """同规格片段直接拼接，不重新编码。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    lst = out.with_suffix(".txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for f in files:
            fh.write("file '%s'\n" % str(Path(f).resolve()).replace("'", "'\\''"))
    r = tools.run([tools.ffmpeg(), "-nostdin", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                   "-c", "copy", "-movflags", "+faststart", str(out)])
    lst.unlink(missing_ok=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[-600:])
    return out


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', " ", s).strip()
    return re.sub(r"\s+", "_", s)[:60]
