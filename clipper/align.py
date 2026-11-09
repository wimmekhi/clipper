"""整集对齐：整集音频跑一次带词级时间的语音识别，再把每条字幕映射到真实语音起止。

- 每集只跑一次，结果写到 媒体库/.clipper/align/<集>.json（在移动硬盘上，换电脑直接用）。
- 字幕时间只当「大概在哪」的锚点；偏移会随集内漂移，用最近若干条的偏移中位数动态跟踪。
- 引擎：Mac(Apple 芯片) 优先 mlx-whisper（Metal GPU，打包版已内置）；不可用或出错自动退回 faster-whisper（CPU）。
"""
from __future__ import annotations

import json
import os
import platform
import statistics
import subprocess
import threading
import time
import traceback
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

_MLX_BROKEN: str | None = None     # 本次运行里 mlx 出过错就记下原因，后面直接走 CPU
LAST_ENGINE = ""                    # 最近一次实际用的引擎（写进日志/对齐结果）


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def default_engine() -> str:
    if is_apple_silicon() and not _MLX_BROKEN:
        try:
            import mlx_whisper  # noqa: F401
            return "mlx"
        except Exception as ex:  # noqa
            _mark_mlx_broken(f"导入失败 {ex!r}")
    return "faster"


def _mark_mlx_broken(why: str):
    global _MLX_BROKEN
    _MLX_BROKEN = why[:200]
    print(f"[clipper] mlx-whisper 不可用，改用 faster-whisper(CPU)：{_MLX_BROKEN}", flush=True)


def mlx_device() -> str:
    """'GPU（Apple M5）' 或 'CPU（没有 Metal）'。"""
    import mlx.core as mx
    if not mx.metal.is_available():
        return "CPU（没有 Metal）"
    name = ""
    for fn in (getattr(mx, "device_info", None), getattr(mx.metal, "device_info", None)):
        try:
            if fn:
                name = fn().get("device_name", "")
                if name:
                    break
        except Exception:
            pass
    return f"GPU（{name or 'Metal'}）"


_MODEL_CACHE: dict = {}
_TQDM_LOCK = threading.Lock()

MIRROR = "https://hf-mirror.com"
# 模型大概多大（下载进度用；实际以下载到的为准）
APPROX_MB = {"tiny": 75, "base": 145, "small": 485, "medium": 1530, "large-v3-turbo": 1620, "large-v3": 3100}


def _use_mirror(on: bool):
    """国内镜像。huggingface_hub 可能已被导入（地址在导入时就定死），所以连它的常量一起改。"""
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "20")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    url = MIRROR if on else "https://huggingface.co"
    os.environ["HF_ENDPOINT"] = url
    if on:
        os.environ["HF_HUB_DISABLE_XET"] = "1"     # 镜像不走 xet 协议
    try:
        import huggingface_hub.constants as C
        if on and hasattr(C, "HF_HUB_DISABLE_XET"):
            C.HF_HUB_DISABLE_XET = True
        C.ENDPOINT = url
        C.HUGGINGFACE_CO_URL_TEMPLATE = url + "/{repo_id}/resolve/{revision}/{filename}"
    except Exception:
        pass


def mlx_repo(model: str) -> str:
    if "/" in model:
        return model
    if model.startswith("large-v3-turbo"):
        return "mlx-community/whisper-large-v3-turbo"
    return f"mlx-community/whisper-{model}-mlx"


def faster_repo(model: str) -> str:
    if "/" in model:
        return model
    try:
        from faster_whisper.utils import _MODELS
        return _MODELS.get(model, f"Systran/faster-whisper-{model}")
    except Exception:
        return f"Systran/faster-whisper-{model}"


def _dir_mb(d: Path) -> float:
    n = 0
    try:
        for f in d.rglob("*"):
            if f.is_file() and not f.is_symlink():
                n += f.stat().st_size
    except OSError:
        pass
    return n / 1e6


def ensure_model(engine: str, model: str, mirror=True, progress=None) -> str:
    """下载（或找到已下载的）模型，返回本地目录。下载时每秒报一次已下载多少 MB。"""
    _use_mirror(mirror)
    from huggingface_hub import snapshot_download
    repo = mlx_repo(model) if engine == "mlx" else faster_repo(model)
    allow = None if engine == "mlx" else ["config.json", "preprocessor_config.json", "model.bin",
                                         "tokenizer.json", "vocabulary.*"]
    try:   # 已经下过：不联网直接用
        return snapshot_download(repo, allow_patterns=allow, local_files_only=True)
    except Exception:
        pass
    from huggingface_hub import constants as C
    cache = Path(C.HF_HUB_CACHE) / ("models--" + repo.replace("/", "--"))
    total = APPROX_MB.get(model, 0)
    base = _dir_mb(cache)
    stop = threading.Event()

    def poll():
        while not stop.wait(1.0):
            got = _dir_mb(cache) - base
            if progress:
                frac = min(0.99, got / total) if total else 0.0
                progress(frac, f"下载模型 {repo}：{got:.0f}" + (f" / 约 {total} MB" if total else " MB")
                         + f"（{os.environ.get('HF_ENDPOINT')}）")
    th = threading.Thread(target=poll, daemon=True)
    th.start()
    try:
        path = snapshot_download(repo, allow_patterns=allow)
    except Exception as ex:
        raise RuntimeError(f"模型下载失败（{os.environ.get('HF_ENDPOINT')}）：{ex}") from ex
    finally:
        stop.set()
    return path


class _TqdmShim:
    """替换 mlx_whisper 里的 tqdm，用它的帧进度回报识别进度。"""
    def __init__(self, cb):
        import tqdm as _t
        outer = cb

        class Bar(_t.tqdm):
            def __init__(self, *a, **k):
                k["disable"] = False
                k["file"] = open(os.devnull, "w")
                super().__init__(*a, **k)

            def update(self, n=1):
                r = super().update(n)
                if self.total:
                    outer(min(1.0, self.n / self.total))
                return r
        self.tqdm = Bar


def transcribe_mlx(audio: np.ndarray, model: str, model_path: str | None = None, progress=None) -> list[dict]:
    import sys
    import mlx_whisper
    T = sys.modules["mlx_whisper.transcribe"]     # mlx_whisper.transcribe 这个名字被同名函数占了
    kw = dict(path_or_hf_repo=model_path or mlx_repo(model), language="en", word_timestamps=True,
              condition_on_previous_text=False)
    with _TQDM_LOCK:
        old = T.tqdm
        if progress:
            T.tqdm = _TqdmShim(progress)
            kw["verbose"] = False          # verbose=False 才会走进度条
        else:
            kw["verbose"] = None
        try:
            res = mlx_whisper.transcribe(audio, **kw)
        finally:
            T.tqdm = old
    words = []
    for seg in res.get("segments", []):
        for w in seg.get("words", []) or []:
            words.append(dict(w=w["word"].strip(), start=round(float(w["start"]), 3),
                              end=round(float(w["end"]), 3), p=round(float(w.get("probability", 1)), 3)))
    return words


def transcribe(audio: np.ndarray, model: str = "small", engine: str | None = None, mirror=True,
               progress=None) -> list[dict]:
    """返回 [{w,start,end,p}]。Apple 芯片优先 mlx-whisper（GPU），出错自动退回 faster-whisper（CPU）。
    progress(frac, 文字)。"""
    global LAST_ENGINE
    _use_mirror(mirror)
    say = progress or (lambda f, t="": None)
    engine = engine or default_engine()
    if engine == "mlx" and _MLX_BROKEN:
        engine = "faster"
    total = len(audio) / SR
    words: list[dict] = []
    if engine == "mlx":
        try:
            dev = mlx_device()
            path = ensure_model("mlx", model, mirror, lambda f, t: say(0.0, t))
            LAST_ENGINE = f"mlx-whisper · {dev}"
            say(0.0, f"识别中（{LAST_ENGINE}）…")
            t0 = time.time()
            words = transcribe_mlx(audio, model, path, lambda f: say(
                f, f"识别 {f*100:.0f}%（{LAST_ENGINE}，已用 {time.time()-t0:.0f}s）"))
            log(f"引擎 {LAST_ENGINE}")
            say(1.0, "识别完成")
            return words
        except Exception as ex:  # noqa
            if "模型下载失败" in str(ex):
                raise
            _mark_mlx_broken(f"运行失败 {ex!r}")
            log("mlx 失败，退回 CPU：" + traceback.format_exc())
            words = []
    LAST_ENGINE = "faster-whisper · CPU" + (f"（mlx 不可用：{_MLX_BROKEN[:60]}）" if _MLX_BROKEN and is_apple_silicon() else "")
    log(f"引擎 {LAST_ENGINE}")
    from faster_whisper import WhisperModel
    key = (model,)
    if key not in _MODEL_CACHE:
        path = ensure_model("faster", model, mirror, lambda f, t: say(0.0, t))
        say(0.0, "加载模型…")
        threads = max(1, (os.cpu_count() or 4) - 1)
        _MODEL_CACHE[key] = WhisperModel(path, device="cpu", compute_type="int8", cpu_threads=threads)
    m = _MODEL_CACHE[key]
    say(0.0, f"识别中（{LAST_ENGINE}）…")
    t0 = time.time()
    segs, _info = m.transcribe(audio, language="en", word_timestamps=True, vad_filter=True,
                               condition_on_previous_text=False, beam_size=1)
    for seg in segs:
        for w in seg.words or []:
            words.append(dict(w=w.word.strip(), start=round(w.start, 3), end=round(w.end, 3),
                              p=round(w.probability, 3)))
        if total:
            f = min(0.99, seg.end / total)
            say(f, f"识别 {f*100:.0f}%（{LAST_ENGINE}，已用 {time.time()-t0:.0f}s）")
    say(1.0, "识别完成")
    return words


# ---------------- 日志 ----------------
LOG_FILE: Path | None = None


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print("[clipper] " + line, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


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
    """progress(总进度 0~1, 当前步骤文字)。"""
    global LOG_FILE
    LOG_FILE = lib.data / "align.log" if hasattr(lib, "data") else None
    say = progress or (lambda f, t="": None)
    key = media_row["key"]
    video = lib.abs(media_row["rel_video"])
    if not video or not video.exists():
        raise FileNotFoundError(f"没有视频文件：{media_row['rel_video']}")
    t0 = time.time()
    log(f"开始 {key}  视频 {media_row['rel_video']}  字幕 {media_row['rel_sub']}")
    say(0.01, "读取音频（ffmpeg 解码整集）…")
    try:
        audio = load_audio(video)
    except subprocess.CalledProcessError as ex:
        raise RuntimeError("ffmpeg 读音频失败：" + (ex.stderr or b"").decode("utf-8", "ignore")[-300:]) from ex
    dur = len(audio) / SR
    if dur < 5:
        raise RuntimeError(f"音频只有 {dur:.1f}s，视频可能坏了")
    say(0.05, f"音频 {dur/60:.1f} 分钟，准备识别…")
    words = transcribe(audio, model=model, engine=engine, mirror=mirror,
                       progress=lambda f, t="": say(0.05 + 0.9 * f, t))
    say(0.96, f"匹配字幕（识别出 {len(words)} 个词）…")
    rows = lib.db.execute("SELECT idx,start,end,en FROM cues WHERE media_id=? ORDER BY idx",
                          (media_row["id"],)).fetchall()
    cues = [dict(r) for r in rows]
    mapped = map_cues(cues, words)
    ok = [m for m in mapped if m["a_start"] is not None and m["score"] >= 0.5]
    offs = [m["a_start"] - c["start"] for m, c in zip(mapped, cues) if m["a_start"] is not None]
    summary = (f"对上 {len(ok)}/{len(cues)} 条"
               + (f"，偏移 {min(offs):+.1f}～{max(offs):+.1f}s" if offs else "")
               + f"，用时 {time.time()-t0:.0f}s · {LAST_ENGINE}")
    data = dict(version=1, key=key, engine=LAST_ENGINE, model=model, duration=dur,
                created=time.time(), summary=summary, words=words, cues=mapped)
    f = lib.align_file(key)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    tmp.replace(f)
    lib.db.execute("UPDATE media SET duration=? WHERE id=?", (dur, media_row["id"]))
    lib.apply_alignment(media_row["id"], data)
    log(f"完成 {key}  {summary}")
    say(1.0, "完成：" + summary)
    return data
