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
    kw.setdefault("close_fds", False)   # 走 posix_spawn，后台线程里拉子进程不怕 fork 卡死
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


_info_cache: dict = {}


def media_info(path) -> dict:
    """只用 ffmpeg 读码率：vkbps（视频）、akbps（音频）。读不到的项为 0。按路径+大小+修改时间缓存。"""
    import re
    try:
        st = os.stat(path); key = (str(path), st.st_size, st.st_mtime)
    except OSError:
        key = (str(path), 0, 0)
    if key in _info_cache:
        return _info_cache[key]
    r = run([ffmpeg(), "-hide_banner", "-i", str(path)])
    total = vk = ak = 0
    dur = 0.0
    for line in r.stderr.splitlines():
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", line)
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        m = re.search(r"bitrate: (\d+) kb/s", line)
        if m and "Duration" in line:
            total = int(m.group(1))
        if "Stream #" in line:
            b = re.search(r"(\d+) kb/s", line)
            if "Video:" in line and b and not vk:
                vk = int(b.group(1))
            elif "Audio:" in line and b and not ak:
                ak = int(b.group(1))
    if not total and dur > 0:
        try:
            total = int(os.path.getsize(path) * 8 / dur / 1000)
        except OSError:
            pass
    if not vk and total:
        vk = max(0, total - (ak or 128))
    res = dict(vkbps=vk, akbps=ak, total=total)
    _info_cache[key] = res
    return res
