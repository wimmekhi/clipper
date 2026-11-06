"""台词搜索：默认模糊（词形 + 乱序），可切精确 / 严格顺序；结果分三级并按质量排序、去重。

查询里可以用 * 或 ... 或 … 表示「这里有几个词」，例如：whose * is this、is this your ...
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

TIER_ORIG = "原句型"
TIER_NEAR = "近似变体"
TIER_FUNC = "功能等价"

_WILD = {"*", "...", "…", "…?"}


def norm_tokens(s: str) -> list[str]:
    s = s.lower().replace("’", "'").replace("‘", "'")
    s = s.replace("…", " … ").replace("...", " … ")
    toks = re.findall(r"[a-z0-9]+(?:'[a-z]+)?|…|\*", s)
    return toks


def stem(w: str) -> str:
    """很粗的词干，只用于判断顺序；召回靠 SQLite 的 porter 分词。"""
    w = re.sub(r"'s$", "", w)
    for suf, rep in (("ies", "y"), ("ing", ""), ("ied", "y"), ("ed", ""), ("es", ""), ("s", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)] + rep
            break
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiou":
        w = w[:-1]
    return w


@dataclass
class Query:
    raw: str
    parts: list[str]           # 词或 "…"
    @property
    def words(self):
        return [p for p in self.parts if p not in ("…", "*")]


def parse_query(q: str) -> Query:
    toks = norm_tokens(q)
    toks = ["…" if t in ("*", "…") else t for t in toks]
    while toks and toks[0] == "…":
        toks.pop(0)
    while toks and toks[-1] == "…":
        toks.pop()
    return Query(q, toks)


def _regex(parts: list[str], fn=lambda x: x) -> re.Pattern:
    out = []
    for p in parts:
        if p == "…":
            out.append(r"(?:\S+ ){1,5}")
        else:
            out.append(re.escape(fn(p)) + " ")
    return re.compile(r"(?:^| )" + "".join(out))


def _fts_expr(words: list[str]) -> str:
    return " AND ".join('"%s"' % w.replace('"', "") for w in dict.fromkeys(words))


@dataclass
class Hit:
    cue_id: int
    media_id: int
    media_key: str
    show: str
    season: int
    episode: int
    kind: str
    idx: int
    start: float
    end: float
    en: str
    zh: str
    speakers: int
    a_start: float | None
    a_end: float | None
    a_score: float | None
    has_video: bool
    tier: str
    score: float
    matched: str = ""        # 命中的那段原文，用于高亮

    @property
    def label(self):
        if self.kind == "tv":
            return f"{self.show} S{self.season:02d}E{self.episode:02d}"
        return self.show


class Searcher:
    def __init__(self, lib):
        self.lib = lib

    def equivalents(self) -> dict[str, list[str]]:
        f = self.lib.data / "patterns.json"
        if not f.exists():
            f.write_text(json.dumps({
                "what are you doing": ["what're you doing", "what you doing", "what are you up to"],
                "whose * is this": ["whose is this", "who does this * belong to", "who owns this"],
                "is this your *": ["is that your", "this is your * right"],
            }, ensure_ascii=False, indent=2), "utf-8")
        try:
            d = json.loads(f.read_text("utf-8"))
        except Exception:
            return {}
        return {" ".join(norm_tokens(k)): v for k, v in d.items()}

    def _candidates(self, words, exact: bool, filters: dict, limit=20000):
        if not words:
            return []
        table = "fts_exact" if exact else "fts_stem"
        sql = f"""SELECT c.*, m.key media_key, m.show, m.season, m.episode, m.kind,
                         m.rel_video IS NOT NULL has_video
                  FROM {table} f JOIN cues c ON c.id=f.rowid JOIN media m ON m.id=c.media_id
                  WHERE {table} MATCH ?"""
        args = [("en : (" + _fts_expr(words) + ")")]
        if filters.get("shows"):
            sql += " AND m.show IN (%s)" % ",".join("?" * len(filters["shows"]))
            args += list(filters["shows"])
        if filters.get("kind"):
            sql += " AND m.kind=?"
            args.append(filters["kind"])
        if filters.get("season"):
            sql += " AND m.season=?"
            args.append(int(filters["season"]))
        if filters.get("aligned_only"):
            sql += " AND m.align_status='done'"
        if filters.get("video_only"):
            sql += " AND m.rel_video IS NOT NULL"
        sql += " LIMIT %d" % limit
        try:
            return self.lib.db.execute(sql, args).fetchall()
        except Exception:
            return []

    def search(self, q: str, exact=False, ordered=False, single_only=False,
               include_func=True, filters=None, max_dur=None, min_dur=None) -> list[Hit]:
        filters = filters or {}
        qu = parse_query(q)
        if not qu.words:
            return []
        rx_exact = _regex(qu.parts)
        rx_stem = _regex(qu.parts, stem)
        hits: dict[tuple, Hit] = {}

        def consider(row, tier_override=None, rx_e=rx_exact, rx_s=rx_stem, need_order=ordered, nwords=len(qu.words)):
            toks = norm_tokens(row["en"])
            line = " ".join(toks) + " "
            sline = " ".join(stem(t) for t in toks) + " "
            m_e = rx_e.search(" " + line)
            m_s = rx_s.search(" " + sline)
            if need_order:
                if exact and not m_e:
                    return
                if not exact and not (m_e or m_s):
                    return
            if exact and not all(w in toks for w in [p for p in qu.parts if p != "…"]) and tier_override is None:
                return
            if single_only and row["speakers"] > 1:
                return
            dur = row["end"] - row["start"]
            if row["a_start"] is not None and (row["a_score"] or 0) >= 0.5:
                dur = row["a_end"] - row["a_start"]
            if max_dur and dur > max_dur:
                return
            if min_dur and dur < min_dur:
                return
            tier = tier_override or (TIER_ORIG if m_e else TIER_NEAR)
            s = self._score(row, toks, tier, m_e or m_s, nwords, dur)
            matched = ""
            if m_e or m_s:
                m = m_e or m_s
                a = len((" " + (line if m_e else sline))[: m.start()].split())
                b = a + len(m.group(0).split())
                matched = " ".join(toks[a:b])
            h = Hit(row["id"], row["media_id"], row["media_key"], row["show"], row["season"], row["episode"],
                    row["kind"], row["idx"], row["start"], row["end"], row["en"], row["zh"] or "",
                    row["speakers"], row["a_start"], row["a_end"], row["a_score"], bool(row["has_video"]),
                    tier, s, matched)
            dk = (row["media_id"], " ".join(toks))           # 同集同句只留一条
            old = hits.get(dk)
            if old is None or (_tier_rank(h.tier), -h.score) < (_tier_rank(old.tier), -old.score):
                hits[dk] = h

        for row in self._candidates(qu.words, exact, filters):
            consider(row)

        if include_func and not exact:
            eq = self.equivalents().get(" ".join(qu.parts).replace("…", "*"), [])
            for alt in eq:
                aq = parse_query(alt)
                if not aq.words:
                    continue
                for row in self._candidates(aq.words, False, filters):
                    key = (row["media_id"], " ".join(norm_tokens(row["en"])))
                    if key in hits:
                        continue
                    consider(row, TIER_FUNC, _regex(aq.parts), _regex(aq.parts, stem), True, len(aq.words))

        out = sorted(hits.values(), key=lambda h: (_tier_rank(h.tier), -h.score))
        return out

    @staticmethod
    def _score(row, toks, tier, m, nwords, dur) -> float:
        s = 0.0
        if row["speakers"] <= 1:
            s += 2.0
        # 时长 1.2–6 秒最好用
        if 1.2 <= dur <= 6:
            s += 2.0
        elif dur < 1.2:
            s += 0.5
        else:
            s -= min(3.0, (dur - 6) * 0.5)
        # 整句就是目标句 / 句子从目标开始
        extra = max(0, len(toks) - nwords)
        s += max(0.0, 2.0 - extra * 0.25)
        if extra == 0:
            s += 1.5          # 整句恰好就是目标句
        if m is not None and m.start() == 0:
            s += 1.0
        en = row["en"]
        if re.search(r"\b(\w+)\b[ ,.-]+\1\b", en.lower()):   # 结巴、重复
            s -= 1.5
        if en.count("?") + en.count(".") + en.count("!") > 2:
            s -= 0.5
        if row["a_start"] is not None and (row["a_score"] or 0) >= 0.5:
            s += 0.5 + 0.5 * (row["a_score"] or 0)
        if row["zh"]:
            s += 0.3
        if row["has_video"]:
            s += 2.5          # 有视频、能直接预览的排前面
        return round(s, 3)


def _tier_rank(t):
    return {TIER_ORIG: 0, TIER_NEAR: 1, TIER_FUNC: 2}.get(t, 3)
