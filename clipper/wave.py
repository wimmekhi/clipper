"""切点条用的波形：只取片段附近几秒的音频。"""
from __future__ import annotations

import subprocess
import numpy as np

from . import tools


def peaks(video, t0: float, t1: float, bins: int = 600, sr: int = 8000) -> np.ndarray:
    t0 = max(0.0, t0)
    cmd = [tools.ffmpeg(), "-nostdin", "-v", "error", "-ss", f"{t0:.3f}", "-i", str(video), "-t",
           f"{max(0.1, t1 - t0):.3f}", "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, **tools.no_window()).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    if not len(a):
        return np.zeros(bins, np.float32)
    n = max(1, len(a) // bins)
    a = a[: n * bins] if len(a) >= n * bins else np.pad(a, (0, n * bins - len(a)))
    p = np.abs(a.reshape(bins, n)).max(axis=1)
    m = float(p.max()) or 1.0
    return (p / m).astype(np.float32)
