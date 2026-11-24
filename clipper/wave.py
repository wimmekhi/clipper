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
