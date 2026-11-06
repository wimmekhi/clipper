"""外部工具：ffmpeg / ffprobe 路径（可在设置里指定），Windows 下不弹黑窗。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

_paths = {"ffmpeg": None, "ffprobe": None}


def set_paths(ffmpeg: str | None = None, ffprobe: str | None = None):
    if ffmpeg:
        _paths["ffmpeg"] = ffmpeg
        if not ffprobe:
            cand = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if os.name == "nt" else ""))
            if os.path.exists(cand):
                _paths["ffprobe"] = cand
    if ffprobe:
        _paths["ffprobe"] = ffprobe


def _find(name):
    if _paths[name]:
        return _paths[name]
    from .paths import bin_dir
    exe = name + (".exe" if os.name == "nt" else "")
    bundled = bin_dir() / exe          # 打包版自带的 ffmpeg
    if bundled.exists():
        return str(bundled)
    p = shutil.which(name)
    if p:
        return p
    for d in ("/opt/homebrew/bin", "/usr/local/bin", r"C:\\ffmpeg\\bin"):
        cand = os.path.join(d, name + (".exe" if os.name == "nt" else ""))
        if os.path.exists(cand):
            return cand
    return name


def ffmpeg():
    return _find("ffmpeg")


def ffprobe():
    return _find("ffprobe")


def no_window():
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}
    return {}


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **no_window(), **kw)


def probe(path) -> dict:
    r = run([ffprobe(), "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:300])
    return json.loads(r.stdout)


def check() -> str | None:
    """返回错误说明；正常返回 None。"""
    try:
        r = run([ffmpeg(), "-hide_banner", "-filters"])
    except FileNotFoundError:
        return "找不到 ffmpeg，请先安装（见说明），或在设置里指定路径"
    if r.returncode:
        return "ffmpeg 无法运行"
    if " subtitles " not in r.stdout:
        return "这个 ffmpeg 不带 libass，烧字幕导出会失败，请换完整版 ffmpeg"
    return None


def video_size(path) -> tuple[int, int]:
    """只用 ffmpeg（不依赖 ffprobe）读视频的显示宽高（已按 SAR 换算）。"""
    import re
    r = run([ffmpeg(), "-hide_banner", "-i", str(path)])
    for line in r.stderr.splitlines():
        if "Video:" in line:
            m = re.search(r", (\d{2,5})x(\d{2,5})(?:[, ]|$)", line)
            if not m:
                continue
            w, h = int(m.group(1)), int(m.group(2))
            sar = re.search(r"SAR (\d+):(\d+)", line)
            if sar and int(sar.group(1)) and int(sar.group(2)):
                w = int(round(w * int(sar.group(1)) / int(sar.group(2))))
            return w, h
    raise RuntimeError("读不出视频尺寸：" + r.stderr.strip()[-200:])
