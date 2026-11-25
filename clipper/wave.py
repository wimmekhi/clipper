"""切点条用的波形：只取片段附近几秒的音频。"""
from __future__ import annotations

import subprocess
import numpy as np

from . import tools


def peaks(video, t0: float, t1: float, bins: int = 600, sr: int = 8000) -> np.ndarray:
    t0 = max(0.0, t0)
    cmd = [tools.ffmpeg(), "-nostdin", "-v", "error", "-ss", f"{t0:.3f}", "-i", str(video), "-t",
           f"{max(0.1, t1 - t0):.3f}", "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    # close_fds=False 让 macOS / Linux 走 posix_spawn 而不是 fork：波形在后台线程里拉 ffmpeg，
    # fork 时若别的线程正持有锁（Qt 建控件、import），子进程可能卡死（1.3.1 Mac CI 遇到过）
    raw = subprocess.run(cmd, capture_output=True, close_fds=False, **tools.no_window()).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    if not len(a):
        return np.zeros(bins, np.float32)
    n = max(1, len(a) // bins)
    a = a[: n * bins] if len(a) >= n * bins else np.pad(a, (0, n * bins - len(a)))
    p = np.abs(a.reshape(bins, n)).max(axis=1)
    m = float(p.max()) or 1.0
    return (p / m).astype(np.float32)


def samples(video, t0: float, t1: float, sr: int = 8000) -> np.ndarray:
    """片段附近的原始单声道采样（-1~1）：工作台波形可放大、吸附静音点 / 过零点都用它。"""
    t0 = max(0.0, t0)
    cmd = [tools.ffmpeg(), "-nostdin", "-v", "error", "-ss", f"{t0:.3f}", "-i", str(video), "-t",
           f"{max(0.1, t1 - t0):.3f}", "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, close_fds=False, **tools.no_window()).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


OV_RATE = 200            # 整集缩略音轨：每秒几个点
_OV_SR = 2000            # 读整集音频用的采样率（单声道），每 10 个采样取一个峰值


def overview(video, rate: int = OV_RATE) -> np.ndarray:
    """整集包络：ffmpeg 一次读出整条音轨（单声道低采样率），每 1/rate 秒取峰值，0..1，uint8 存。"""
    cmd = [tools.ffmpeg(), "-nostdin", "-v", "error", "-i", str(video), "-vn", "-sn", "-dn",
           "-ac", "1", "-ar", str(_OV_SR), "-f", "s16le", "-"]
    # close_fds=False：posix_spawn，不 fork（后台线程里拉子进程，Mac 上 fork 会卡死，见 1.3.1）
    raw = subprocess.run(cmd, capture_output=True, close_fds=False, **tools.no_window()).stdout
    a = np.abs(np.frombuffer(raw, np.int16).astype(np.int32))
    if not len(a):
        return np.zeros(0, np.uint8)
    n = _OV_SR // rate
    k = len(a) // n
    if k == 0:
        return np.zeros(0, np.uint8)
    p = a[: k * n].reshape(k, n).max(axis=1).astype(np.float32)
    m = float(np.percentile(p, 99.5)) or float(p.max()) or 1.0
    return np.clip(p / m * 255.0, 0, 255).astype(np.uint8)


def overview_cached(cache_dir, key: str, video, rate: int = OV_RATE) -> np.ndarray:
    """整集包络，缓存到 媒体库/.clipper/wave/：文件名带视频大小 + 修改时间，视频换了自动重读。"""
    from pathlib import Path
    video = Path(video)
    st = video.stat()
    base = key.replace("/", "__").replace("\\", "__")
    d = Path(cache_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{base}.ov{rate}.{st.st_size}_{int(st.st_mtime)}.u8"
    if f.exists():
        return np.fromfile(f, dtype=np.uint8)
    env = overview(video, rate)
    for old in d.glob(f"{base}.ov{rate}.*.u8"):
        try:
            old.unlink()
        except OSError:
            pass
    if len(env):
        tmp = f.with_suffix(".part")
        env.tofile(tmp)
        tmp.replace(f)
    return env
