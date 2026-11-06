"""整集对齐：整集音频跑一次带词级时间的语音识别，再把每条字幕映射到真实语音起止。

- 每集只跑一次，结果写到 媒体库/.clipper/align/<集>.json（在移动硬盘上，换电脑直接用）。
- 字幕时间只当「大概在哪」的锚点；偏移会随集内漂移，用最近若干条的偏移中位数动态跟踪。
- 引擎：Mac(Apple 芯片) 装了 mlx-whisper 就用它（走 GPU）；否则用 faster-whisper（CPU）。
"""
from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import time
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from .search import norm_tokens
from . import tools

SR = 16000


def load_audio(video: Path) -> np.ndarray:
    cmd = [tools.ffmpeg(), "-nostdin", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", str(SR),
           "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True, **tools.no_window()).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


# ---------------- 识别引擎 ----------------

def default_engine() -> str:
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            import mlx_whisper  # noqa: F401
            return "mlx"
        except Exception:
            pass
    return "faster"


_MODEL_CACHE: dict = {}


def _use_mirror(on: bool):
    if on and not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"


def transcribe(audio: np.ndarray, model: str = "small", engine: str | None = None, mirror=True,
               progress=None) -> list[dict]:
    """返回 [{w,start,end,p}]"""
    _use_mirror(mirror)
    engine = engine or default_engine()
    total = len(audio) / SR
    words: list[dict] = []
    if engine == "mlx":
        import mlx_whisper
        if "/" in model:
            repo = model
        elif model.startswith("large-v3-turbo"):
            repo = "mlx-community/whisper-large-v3-turbo"
        else:
            repo = f"mlx-community/whisper-{model}-mlx"
        res = mlx_whisper.transcribe(audio, path_or_hf_repo=repo, language="en", word_timestamps=True,
                                     condition_on_previous_text=False, verbose=None)
        for seg in res.get("segments", []):
            for w in seg.get("words", []) or []:
                words.append(dict(w=w["word"].strip(), start=float(w["start"]), end=float(w["end"]),
                                  p=float(w.get("probability", 1))))
        if progress:
            progress(1.0)
        return words
    from faster_whisper import WhisperModel
    key = (model,)
    if key not in _MODEL_CACHE:
        threads = max(1, (os.cpu_count() or 4) - 1)
        _MODEL_CACHE[key] = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)
    m = _MODEL_CACHE[key]
    segs, _info = m.transcribe(audio, language="en", word_timestamps=True, vad_filter=True,
                               condition_on_previous_text=False, beam_size=1)
    for seg in segs:
        for w in seg.words or []:
            words.append(dict(w=w.word.strip(), start=round(w.start, 3), end=round(w.end, 3),
                              p=round(w.probability, 3)))
        if progress and total:
            progress(min(0.99, seg.end / total))
    if progress:
        progress(1.0)
    return words


# ---------------- 字幕 → 语音 映射 ----------------

def _tokens(words):
    tw = []
    for i, w in enumerate(words):
        for t in norm_tokens(w["w"]):
            if t not in ("…", "*"):
                tw.append((t, w["start"], w["end"], i))
    return tw


def _best_in(ct, tw, starts, t_lo, t_hi, expect, dist_w=0.0):
    """在 [t_lo,t_hi] 时间窗内找与 ct 最像的连续词段，返回按得分排序的候选列表。"""
    import bisect
    lo = bisect.bisect_left(starts, t_lo)
    hi = bisect.bisect_right(starts, t_hi)
    L = len(ct)
    cset = set(ct)
    cands = []
    for j in range(lo, hi):
        if tw[j][0] not in cset:
            continue
        # 段起点可以是 ct 中间的词（首词没听出来时），往前退几格
        for back in range(0, min(3, L)):
            j0 = j - back
            if j0 < 0:
                continue
            for span_len in {max(1, L - 1), L, L + 1}:
                span = tw[j0:j0 + span_len]
                sm = SequenceMatcher(None, ct, [x[0] for x in span], autojunk=False)
                r = sm.ratio()
                if r < 0.5:
                    continue
                blocks = [bk for bk in sm.get_matching_blocks() if bk.size]
                w0 = j0 + blocks[0].b
                w1 = j0 + blocks[-1].b + blocks[-1].size - 1
                adj = r - dist_w * abs(tw[w0][1] - expect)
                lead = blocks[0].a
                tail = L - (blocks[-1].a + blocks[-1].size)
                cands.append((adj, r, w0, w1, lead, tail))
    cands.sort(reverse=True)
    # 同一位置只留最好的一个
    seen, uniq = set(), []
    for c in cands:
        if c[2] in seen:
            continue
        seen.add(c[2])
        uniq.append(c)
    return uniq


def map_cues(cues: list[dict], words: list[dict]) -> list[dict]:
    """cues: [{idx,start,end,en}]，返回 [{idx,a_start,a_end,score,w0,w1}]

    1) 先找「锚点」：4 词以上、在 ±40s 内唯一高分匹配的字幕条，得到随时间变化的偏移曲线；
    2) 每条字幕在 曲线预测位置 ±2.5s 内找匹配，离预测越远扣分越多；
    3) 抢同一段语音的，得分高者留；时间顺序冲突的，去掉得分低者；
    4) 剩下没对上的按前后插值，标为估算。
    """
    tw = _tokens(words)
    starts = [t[1] for t in tw]
    toks = {c["idx"]: [t for t in norm_tokens(c["en"]) if t not in ("…", "*")] for c in cues}

    anchors = []
    for c in cues:
        ct = toks[c["idx"]]
        if len(ct) < 4:
            continue
        cands = _best_in(ct, tw, starts, c["start"] - 40, c["end"] + 40, c["start"])
        if not cands or cands[0][1] < 0.8:
            continue
        if len(cands) > 1 and cands[1][1] > cands[0][1] - 0.15 and abs(cands[1][2] - cands[0][2]) > len(ct):
            continue   # 不唯一
        anchors.append((c["start"], tw[cands[0][2]][1] - c["start"]))

    def offset_at(t):
        if not anchors:
            return 0.0
        near = sorted(anchors, key=lambda a: abs(a[0] - t))[:5]
        return statistics.median(o for _, o in near)

    picks = []
    for c in cues:
        ct = toks[c["idx"]]
        if not ct:
            continue
        off = offset_at(c["start"])
        W = 2.5 if anchors else 30.0
        cands = _best_in(ct, tw, starts, c["start"] + off - W, c["end"] + off + W, c["start"] + off, 0.08)
        need = 0.99 if len(ct) <= 2 else 0.7 if len(ct) <= 4 else 0.6
        cands = [x for x in cands if x[1] >= need][:5]
        for adj, r, w0, w1, lead, tail in cands:
            # 长句优先：同等相似度下，词多的匹配更可信
            picks.append(dict(idx=c["idx"], adj=adj + 0.02 * min(len(ct), 10), r=r, w0=w0, w1=w1,
                              lead=lead, tail=tail))

    # 3) 冲突处理：全局贪心，得分高的先拿；每条字幕只拿一段，每个词只给一条
    used: set[int] = set()
    done: set[int] = set()
    kept = []
    for p in sorted(picks, key=lambda p: -p["adj"]):
        if p["idx"] in done:
            continue
        rng = set(range(p["w0"], p["w1"] + 1))
        if rng & used:
            continue
        used |= rng
        done.add(p["idx"])
        kept.append(p)
    kept.sort(key=lambda p: p["idx"])
    # 顺序冲突：字幕序号递增但语音位置倒退 → 去掉较低分的
    changed = True
    while changed:
        changed = False
        for a, b in zip(kept, kept[1:]):
            if b["w0"] < a["w0"]:
                kept.remove(a if a["adj"] < b["adj"] else b)
                changed = True
                break

    got = {p["idx"]: p for p in kept}
    out = []
    for c in cues:
        if not toks[c["idx"]]:
            continue
        p = got.get(c["idx"])
        if not p:
            out.append(dict(idx=c["idx"], a_start=None, a_end=None, score=0.0))
            continue
        a_s, a_e = tw[p["w0"]][1], tw[p["w1"]][2]
        # 字幕开头/结尾有词没识别出来（比如 Whose 被吞）：按每词约 0.28 秒向外补，但不越过相邻的词
        if p["lead"]:
            prev_end = tw[p["w0"] - 1][2] if p["w0"] > 0 else 0.0
            a_s = max(prev_end + 0.02, a_s - 0.28 * p["lead"])
        if p["tail"]:
            next_start = tw[p["w1"] + 1][1] if p["w1"] + 1 < len(tw) else a_e + 5
            a_e = min(next_start - 0.02, a_e + 0.28 * p["tail"])
        if a_e - a_s < 0.15:
            a_e = a_s + 0.3
        out.append(dict(idx=c["idx"], a_start=round(a_s, 3), a_end=round(a_e, 3), score=round(p["r"], 3),
                        w0=tw[p["w0"]][3], w1=tw[p["w1"]][3]))
    _interpolate(cues, out)
    return out


def _interpolate(cues, out):
    """没对上的条目：用前后已对上条目的偏移按时间线性插值，标为「估算」（score=0.3）。"""
    by_idx = {c["idx"]: c for c in cues}
    anchors = [(by_idx[o["idx"]]["start"], o["a_start"] - by_idx[o["idx"]]["start"],
                o["a_end"] - by_idx[o["idx"]]["end"]) for o in out if o["a_start"] is not None]
    if not anchors:
        return
    import bisect
    ts = [a[0] for a in anchors]
    for o in out:
        if o["a_start"] is not None:
            continue
        c = by_idx[o["idx"]]
        k = bisect.bisect_left(ts, c["start"])
        if k == 0:
            os_, oe = anchors[0][1], anchors[0][2]
        elif k >= len(anchors):
            os_, oe = anchors[-1][1], anchors[-1][2]
        else:
            (t0, s0, e0), (t1, s1, e1) = anchors[k - 1], anchors[k]
            f = (c["start"] - t0) / (t1 - t0) if t1 > t0 else 0.5
            os_, oe = s0 + (s1 - s0) * f, e0 + (e1 - e0) * f
        o["a_start"] = round(c["start"] + os_, 3)
        o["a_end"] = round(max(c["end"] + oe, o["a_start"] + max(0.4, c["end"] - c["start"])), 3)
        o["score"] = 0.3
        o["estimated"] = True


def align_media(lib, media_row, model="small", engine=None, mirror=True, progress=None) -> dict:
    key = media_row["key"]
    video = lib.abs(media_row["rel_video"])
    if not video or not video.exists():
        raise FileNotFoundError("没有视频文件")
    t0 = time.time()
    audio = load_audio(video)
    dur = len(audio) / SR
    words = transcribe(audio, model=model, engine=engine, mirror=mirror,
                       progress=(lambda f: progress(0.05 + 0.9 * f)) if progress else None)
    rows = lib.db.execute("SELECT idx,start,end,en FROM cues WHERE media_id=? ORDER BY idx",
                          (media_row["id"],)).fetchall()
    cues = [dict(r) for r in rows]
    mapped = map_cues(cues, words)
    ok = [m for m in mapped if m["a_start"] is not None and m["score"] >= 0.5]
    offs = [m["a_start"] - c["start"] for m, c in zip(mapped, cues) if m["a_start"] is not None]
    summary = (f"对上 {len(ok)}/{len(cues)} 条"
               + (f"，偏移 {min(offs):+.1f}～{max(offs):+.1f}s" if offs else "")
               + f"，用时 {time.time()-t0:.0f}s")
    data = dict(version=1, key=key, engine=engine or default_engine(), model=model, duration=dur,
                created=time.time(), summary=summary, words=words, cues=mapped)
    f = lib.align_file(key)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tmp.replace(f)
    lib.db.execute("UPDATE media SET duration=? WHERE id=?", (dur, media_row["id"]))
    lib.apply_alignment(media_row["id"], data)
    if progress:
        progress(1.0)
    return data
