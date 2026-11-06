"""字幕解析：SRT / ASS，双语/多语字幕拆成英文主文本 + 中文译文。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Cue:
    idx: int
    start: float
    end: float
    en: str
    zh: str = ""
    speakers: int = 1          # 行首「-」个数推算的说话人数（>=2 表示多人同行）
    extra: dict = field(default_factory=dict)


# ---------- 读文件 ----------

def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "gb18030", "big5", "latin-1"):
        try:
            txt = raw.decode(enc)
            if enc == "utf-16" and not raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return txt
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ---------- 语言判断 ----------

_CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_LATIN = re.compile(r"[A-Za-z]")
_CYR = re.compile(r"[\u0400-\u04ff]")


def line_lang(s: str) -> str:
    """粗分：zh / en / other（日、韩、俄等）/ empty。"""
    s2 = re.sub(r"\s+", "", s)
    if not s2:
        return "empty"
    if _KANA.search(s2):
        return "other"
    if _HANGUL.search(s2):
        return "other"
    cjk = len(_CJK.findall(s2))
    lat = len(_LATIN.findall(s2))
    if cjk and cjk >= lat * 0.3:
        return "zh"
    if _CYR.search(s2) and len(_CYR.findall(s2)) > lat:
        return "other"
    if lat:
        # 法德西等也是拉丁字母；只用重音字母比例粗判
        accents = len(re.findall(r"[À-ÿ]", s2))
        if accents > max(2, lat * 0.08):
            return "other"
        return "en"
    return "other"


def split_langs(lines: list[str]) -> tuple[str, str]:
    en, zh = [], []
    for ln in lines:
        lang = line_lang(ln)
        if lang == "en":
            en.append(ln)
        elif lang == "zh":
            zh.append(ln)
    return " ".join(en).strip(), " ".join(zh).strip()


def count_speakers(en: str, lines: list[str]) -> int:
    dash = sum(1 for ln in lines if re.match(r"^\s*[-–—]\s*\S", ln) and line_lang(ln) == "en")
    return max(1, dash)


_HI = re.compile(r"\[[^\]]*\]|\([^)]*\)|♪+|#")


def clean_en(s: str) -> str:
    s = _HI.sub(" ", s)
    s = re.sub(r"^\s*[-–—]\s*", "", s)
    s = re.sub(r"\s[-–—]\s*(?=[A-Z])", " ", s)
    s = re.sub(r"^[A-Z][A-Z .']{1,20}:\s*", "", s)   # 说话人前缀 MITCHELL:
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------- SRT ----------

_TS = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def _ts(s: str) -> float:
    m = _TS.search(s)
    if not m:
        raise ValueError(s)
    h, mi, se, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000


def _strip_srt_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\{\\[^}]*\}", "", s)
    return s


def parse_srt(text: str) -> list[tuple[float, float, list[str]]]:
    out = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for b in blocks:
        lines = [l for l in b.strip("\n").split("\n")]
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        a, _, z = lines[ti].partition("-->")
        try:
            st, en = _ts(a), _ts(z)
        except ValueError:
            continue
        body = [_strip_srt_tags(l).strip() for l in lines[ti + 1:]]
        body = [l for l in body if l]
        if body:
            out.append((st, en, body))
    return out


# ---------- ASS / SSA ----------

def _ass_ts(s: str) -> float:
    h, m, rest = s.strip().split(":")
    sec, _, cs = rest.partition(".")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int((cs or "0").ljust(2, "0")[:2]) / 100


_SKIP_STYLE = re.compile(r"(^|[^a-z])(signs?|titles?|op|ed|songs?|lyrics?|karaoke|kara|notes?|staff|logo|comment|screen|ruby)([^a-z]|$)", re.I)


def parse_ass(text: str) -> list[tuple[float, float, list[str]]]:
    out = []
    fmt = None
    in_events = False
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("["):
            in_events = line.lower() == "[events]"
            continue
        if not in_events:
            continue
        if line.lower().startswith("format:"):
            fmt = [f.strip().lower() for f in line[7:].split(",")]
            continue
        if not line.lower().startswith("dialogue:"):
            continue          # Comment: 等行跳过
        if not fmt:
            fmt = ["layer", "start", "end", "style", "name", "marginl", "marginr", "marginv", "effect", "text"]
        parts = line[9:].split(",", len(fmt) - 1)
        if len(parts) < len(fmt):
            continue
        d = dict(zip(fmt, parts))
        style = d.get("style", "")
        if _SKIP_STYLE.search(style) or d.get("effect", "").strip():
            continue
        txt = d.get("text", "")
        if re.search(r"\\(move|kf|k\d|K\d)", txt):
            continue          # 移动/卡拉OK 一般是特效
        txt = re.sub(r"\{[^}]*\}", "", txt)
        txt = txt.replace("\\h", " ")
        lines = [l.strip() for l in re.split(r"\\[Nn]", txt) if l.strip()]
        if not lines:
            continue
        try:
            out.append((_ass_ts(d["start"]), _ass_ts(d["end"]), lines))
        except (ValueError, KeyError):
            continue
    return out


# ---------- 合并 ----------

def _merge_same_time(raw: list[tuple[float, float, list[str]]]):
    """ASS 双语常把中英放成同时间两条 Dialogue，按起止时间合并。"""
    raw = sorted(raw, key=lambda r: (r[0], r[1]))
    merged: list[tuple[float, float, list[str]]] = []
    for st, en, lines in raw:
        if merged and abs(merged[-1][0] - st) < 0.05 and abs(merged[-1][1] - en) < 0.05:
            merged[-1][2].extend(lines)
        else:
            merged.append((st, en, list(lines)))
    return merged


def load_cues(path: Path, zh_path: Path | None = None) -> list[Cue]:
    text = read_text(path)
    raw = parse_ass(text) if path.suffix.lower() in (".ass", ".ssa") else parse_srt(text)
    raw = _merge_same_time(raw)
    cues: list[Cue] = []
    for st, en_t, lines in raw:
        en, zh = split_langs(lines)
        en_lines = [l for l in lines if line_lang(l) == "en"]
        sp = count_speakers(en, en_lines)
        en = clean_en(en)
        if not en and not zh:
            continue
        cues.append(Cue(len(cues), st, en_t, en, zh, sp))
    if zh_path and zh_path.exists():
        _attach_zh(cues, zh_path)
    return [c for c in cues if c.en]


def _attach_zh(cues: list[Cue], zh_path: Path):
    """单独的中文字幕：按时间重叠最大的条目挂到英文条上（只补没有中文的）。"""
    text = read_text(zh_path)
    raw = parse_ass(text) if zh_path.suffix.lower() in (".ass", ".ssa") else parse_srt(text)
    zh_items = []
    for st, en, lines in raw:
        _, zh = split_langs(lines)
        if zh:
            zh_items.append((st, en, zh))
    j = 0
    for c in cues:
        if c.zh:
            continue
        best, best_ov = "", 0.0
        while j < len(zh_items) and zh_items[j][1] < c.start - 5:
            j += 1
        k = j
        while k < len(zh_items) and zh_items[k][0] < c.end + 5:
            ov = min(c.end, zh_items[k][1]) - max(c.start, zh_items[k][0])
            if ov > best_ov:
                best, best_ov = zh_items[k][2], ov
            k += 1
        c.zh = best


def write_srt(cues, path: Path):
    def f(t):
        ms = int(round(t * 1000))
        return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}"
    with open(path, "w", encoding="utf-8") as fh:
        for i, c in enumerate(cues, 1):
            fh.write(f"{i}\n{f(c.start)} --> {f(c.end)}\n{c.en}\n" + (f"{c.zh}\n" if c.zh else "") + "\n")
