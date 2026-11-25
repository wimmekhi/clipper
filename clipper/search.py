"""台词搜索：默认模糊（词形 + 乱序），可切精确 / 严格顺序；结果分三级并按质量排序、去重。

查询里可以用 * 或 ... 或 … 表示「这里有几个词」（空位），例如：whose * is this、is this your ...、… or …

1.3.4 排名规则：
- 省略号 = 必填空位（首尾和中间都算），每个空位 SLOT_MIN–SLOT_MAX 个词；空位填不上的不算原句型。
- 按句子（. ? ! 切开）而不是按字幕行算：句型（含空位）不能跨句，长度 / 占比 / 开头都只看命中的那一句。
- 原句型 = 原词连续命中且空位填满；近似变体 = 每个查询词都能被本软件的 stem() 对上（不是 porter：his≠hi、is≠i）。
- 等级进分数：原句型 +4，近似 0，功能等价 −1。
- 固定搭配排除表 EXCLUDE：命中的（例如 or so / or not）降为近似变体。

1.3.5 缩写互通：搜 this is 也能搜到 this's，搜 it's 也能搜到 it is / it has……
- 用户搜的写法 = 原句型；另一种写法（展开 / 缩写）连续命中 = 近似变体（'s 也可能是 has 或所有格，不消歧）。
- 's(is/has) 're(are) 'm(am) 've(have) 'll(will) 'd(would/had) n't(not)，cannot=can't=can not、won't=will not、shan't；
  ain't → am/is/are/have/has not、let's → let us 只单向（搜 ain't / let's 时才展开）。精确模式不互通。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cached_property, lru_cache
from itertools import product

TIER_ORIG = "原句型"
TIER_NEAR = "近似变体"
TIER_FUNC = "功能等价"

_WILD = {"*", "...", "…", "…?"}

SLOT_MIN = 1             # 每个空位至少几个词
SLOT_MAX = 5             # 每个空位最多几个词
SCORE_FULL = 18.0        # pct() 的满分基准：有视频 + 已对齐 + 单人 + 整句就是句型 ≈ 95%

TIER_BONUS = {TIER_ORIG: 4.0, TIER_NEAR: 0.0, TIER_FUNC: -1.0}

# 句首语气词：后面紧接句型也算「句型开头」
INTERJ = {"no", "oh", "so", "yeah", "yes", "well", "okay", "ok", "hey", "and", "but", "look", "listen", "wait",
          "now", "right", "ah", "um", "uh", "hmm", "alright", "god", "man", "yo", "please", "sorry", "then", "anyway"}
# 空位里只有一个虚词：减分
FUNC_WORDS = {"the", "a", "an", "it", "that", "this", "these", "those", "so", "not", "what", "something", "anything",
              "nothing", "there", "here", "then", "too", "him", "her", "them", "me", "you", "one", "else", "is", "be",
              "to", "of", "and", "or", "but", "do", "does", "did", "yes", "no", "now", "us", "we", "i", "he", "she",
              "they", "his", "its", "my", "your", "our", "their", "whatever", "anyway", "though", "all"}

# 固定搭配排除表：键 = 规范化后的句型（parts 用空格连起来，空位写 …）；… 在搭配里表示「中间隔几个词」。
# 命中的那一处降为近似变体。也可在媒体库 .clipper/excludes.json 里追加同样格式的条目。
EXCLUDE: dict[str, list[str]] = {
    "… or …": ["or so", "or something", "or not", "or else", "or what", "whether or not", "either … or",
               "or anything", "or whatever", "or anything else", "or two"],
}


_TOK = re.compile(r"[a-z0-9]+(?:'[a-z]+)?|…|\*")


def norm_tokens(s: str) -> list[str]:
    s = s.lower().replace("’", "'").replace("‘", "'")
    s = s.replace("…", " … ").replace("...", " … ")
    return _TOK.findall(s)


def words_of(s: str) -> list[str]:
    return [t for t in norm_tokens(s) if t not in ("…", "*")]


@lru_cache(maxsize=65536)
def stem(w: str) -> str:
    """很粗的词干：判断近似变体和顺序用（不用 porter：porter 会把 his 削成 hi、is 削成 i）。"""
    if w.endswith("'s"):
        w = w[:-2]
    for suf, rep in (("ies", "y"), ("ing", ""), ("ied", "y"), ("ed", ""), ("es", ""), ("s", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)] + rep
            break
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiou":
        w = w[:-1]
    return w


# ---------------- 缩写互通（1.3.5） ----------------
# 能和后面的 are/am/have/will/would/had 缩写的词（'s 不限，任何词都能 X's）
PRON = {"i", "you", "he", "she", "it", "we", "they", "this", "that", "these", "those", "there", "here", "what", "who",
        "where", "when", "why", "how", "everyone", "everybody", "someone", "somebody", "nobody", "everything",
        "something", "nothing", "y'all"}
# 助动词 + not → n't
NEG = {"do": "don't", "does": "doesn't", "did": "didn't", "is": "isn't", "are": "aren't", "was": "wasn't",
       "were": "weren't", "have": "haven't", "has": "hasn't", "had": "hadn't", "would": "wouldn't",
       "could": "couldn't", "should": "shouldn't", "must": "mustn't", "need": "needn't", "might": "mightn't",
       "will": "won't", "shall": "shan't", "can": "can't"}
NEG_REV = {v: k for k, v in NEG.items()}
_SUFFIX = {"'re": ["are"], "'m": ["am"], "'ve": ["have"], "'ll": ["will"], "'d": ["would", "had"], "'s": ["is", "has"]}
_PAIR = {"is": "'s", "has": "'s", "are": "'re", "am": "'m", "have": "'ve", "will": "'ll", "would": "'d", "had": "'d"}
MAX_ALTS = 8


def _expand_tok(t: str) -> list[list[str]]:
    """一个词的展开写法（不是缩写就原样）。"""
    if t == "cannot":
        return [["can", "not"]]
    if t == "let's":
        return [["let's"], ["let", "us"]]                  # 单向：let's → let us
    if t == "ain't":
        return [["ain't"]] + [[a, "not"] for a in ("am", "is", "are", "have", "has")]   # 单向
    if t in NEG_REV:
        return [[NEG_REV[t], "not"]]
    if t.endswith("n't") and len(t) > 3:                    # 表外的 xxxn't
        return [[t[:-3], "not"]]
    for suf, full in _SUFFIX.items():
        if t.endswith(suf) and len(t) > len(suf):
            base = t[: -len(suf)]
            if suf == "'s":
                return [[base, f] for f in full]
            if suf == "'m" and base != "i":
                return [[t]]
            return [[base, f] for f in full]
    return [[t]]


def _contract_forms(seq: list[str], limit=32) -> list[list[str]]:
    """展开写法 → 所有缩写组合（相邻两词可缩就分缩 / 不缩两路）。"""
    out: list[list[str]] = []

    def rec(i, acc):
        if len(out) >= limit:
            return
        if i >= len(seq):
            out.append(acc)
            return
        a = seq[i]
        rec(i + 1, acc + [a])
        if i + 1 >= len(seq) or a == "…" or "'" in a:
            return
        b = seq[i + 1]
        if b == "not" and a in NEG:
            rec(i + 2, acc + [NEG[a]])
            if a == "can":
                rec(i + 2, acc + ["cannot"])
        elif b in _PAIR and (b in ("is", "has") or a in PRON) and (b != "am" or a == "i") and b != a:
            rec(i + 2, acc + [a + _PAIR[b]])

    rec(0, [])
    return out


def contraction_variants(parts: list[str]) -> list[list[str]]:
    """句型的其他写法（不含原写法本身），最多 MAX_ALTS 个，改动少的在前。"""
    opts = [_expand_tok(t) if t != "…" else [["…"]] for t in parts]
    if all(len(o) == 1 and o[0] == [t] for o, t in zip(opts, parts)) and not any(
            parts[i] in NEG or parts[i + 1] in _PAIR or parts[i + 1] == "not" for i in range(len(parts) - 1)):
        return []
    seen = {tuple(parts)}
    res = []
    for combo in list(product(*opts))[:16]:
        canon = [w for grp in combo for w in grp]
        for f in _contract_forms(canon):
            k = tuple(f)
            if k not in seen:
                seen.add(k)
                res.append(f)
    orig = set(parts)
    res.sort(key=lambda f: (len(set(f) ^ orig), len(f)))
    return res[:MAX_ALTS]


@dataclass
class Query:
    raw: str
    parts: list[str]           # 词或 "…"（空位）
    question: bool = False     # 句型带问号

    @property
    def words(self):
        return [p for p in self.parts if p not in ("…", "*")]

    @property
    def key(self) -> str:
        return " ".join(self.parts)

    @cached_property
    def alts(self) -> list["Query"]:
        """缩写互通：另一种写法（this is ⇄ this's、it's → it is / it has、can't ⇄ cannot ……）。"""
        return [Query(" ".join(p), p, self.question) for p in contraction_variants(self.parts)]


def parse_query(q: str) -> Query:
    toks = norm_tokens(q)
    toks = ["…" if t in ("*", "…") else t for t in toks]
    out = []
    for t in toks:                     # 连着的几个 … 合成一个空位
        if t == "…" and out and out[-1] == "…":
            continue
        out.append(t)
    return Query(q, out, "?" in q or "？" in q)


def _regex(parts: list[str], fn=lambda x: x) -> re.Pattern:
    """旧接口（只看原词顺序，首尾空位不要求）：保留给外部调用。"""
    parts = list(parts)
    while parts and parts[0] == "…":
        parts.pop(0)
    while parts and parts[-1] == "…":
        parts.pop()
    out = []
    for p in parts:
        if p == "…":
            out.append(r"(?:\S+ ){1,5}")
        else:
            out.append(re.escape(fn(p)) + " ")
    return re.compile(r"(?:^| )" + "".join(out))


def _fts_expr(words: list[str]) -> str:
    return " AND ".join('"%s"' % w.replace('"', "") for w in dict.fromkeys(words))


def _phrase_expr(parts: list[str]) -> str:
    """连续的词合成一个短语（缩写互通的候选用）："this's" 在 FTS unicode61 里会切成 this + s，按短语查正好对上。"""
    runs, cur = [], []
    for p in parts + ["…"]:
        if p == "…":
            if cur:
                runs.append(" ".join(cur))
            cur = []
        else:
            cur.append(p.replace('"', ""))
    return " AND ".join('"%s"' % r for r in dict.fromkeys(runs))


# ---------------- 句子 ----------------
_ABBR = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "mt", "prof", "lt", "sgt", "capt", "col", "gen"}
_UNFIN = re.compile(r"(\.\.\.|…|--|—|–|-)\s*$")     # 话没说完（结尾省略号 / 破折号）
_SENT_END = re.compile(r"""[.?!]+["'”’)\]]*(?=\s|$)""")


def sentences(en: str) -> list[tuple[list[str], bool]]:
    """一行字幕按 . ? ! 切成句子：[(词表, 是否问句)]。省略号（... / …）当停顿，不切；Mr. 之类缩写不切。"""
    s = (en or "").replace("…", "...")
    out, start = [], 0
    for m in _SENT_END.finditer(s):
        core = m.group(0).rstrip("\"'”’)]")
        if core.startswith("..") and "?" not in core and "!" not in core:
            continue
        if core == ".":
            pw = re.search(r"([A-Za-z]+)$", s[start:m.start()])
            if pw and pw.group(1).lower() in _ABBR:
                continue
        toks = words_of(s[start:m.end()])
        if toks:
            out.append((toks, "?" in core))
        elif out and "?" in core:
            out[-1] = (out[-1][0], True)
        start = m.end()
    toks = words_of(s[start:])
    if toks:
        out.append((toks, False))
    return out


# ---------------- 匹配 ----------------
def _eq_exact(p, t):
    return p == t


def _match_all(parts: list[str], T: list[str], eq) -> list[tuple[int, int, list[int], list[int]]]:
    """句型（含空位）在一句话里的所有命中：(起, 止, 原词位置, 各空位词数)。尾部空位尽量吃满（到句末，最多 SLOT_MAX）。"""
    res = []
    n = len(T)
    last = len(parts) - 1

    def rec(pi, ti, s0, fixed, slots):
        if len(res) > 200:
            return
        if pi > last:
            res.append((s0, ti, fixed, slots))
            return
        p = parts[pi]
        if p == "…":
            if pi == last:
                k = min(SLOT_MAX, n - ti)
                if k >= SLOT_MIN:
                    rec(pi + 1, ti + k, s0, fixed, slots + [k])
                return
            for k in range(SLOT_MIN, SLOT_MAX + 1):
                if ti + k < n:
                    rec(pi + 1, ti + k, s0, fixed, slots + [k])
        elif ti < n and eq(p, T[ti]):
            rec(pi + 1, ti + 1, s0, fixed + [ti], slots)

    fixed_words = [p for p in parts if p != "…"]
    if fixed_words and eq is _eq_exact:
        ts = set(T)                     # 快速排除：有原词不在这句里
        if any(w not in ts for w in fixed_words):
            return res
    if parts and parts[0] != "…":
        starts = [i for i in range(n) if eq(parts[0], T[i])]
    else:
        starts = range(n)
    for s0 in starts:
        rec(0, s0, s0, [], [])
    if parts and parts[0] == "…":
        # 开头的空位也尽量吃满：要么从句首开始，要么正好 SLOT_MAX 个词
        res = [m for m in res if m[0] == 0 or m[3][0] == SLOT_MAX]
    return res


def _excl_spans(key: str, T: list[str], table: dict) -> list[tuple[int, int]]:
    """排除表里的搭配在这句话里占的词位置区间（闭区间）。"""
    pats = table.get(key)
    if pats is None:
        k2 = key.strip("… ").strip()
        pats = next((v for kk, v in table.items() if kk.strip("… ").strip() == k2), [])
    spans = []
    for p in pats:
        pp = parse_query(p).parts
        if not pp:
            continue
        L = len(T)
        for s0 in range(L):
            ti, ok = s0, True
            for k, w in enumerate(pp):
                if w == "…":
                    nxt = pp[k + 1] if k + 1 < len(pp) else None
                    if nxt is None:
                        ti = L
                        break
                    j = ti
                    while j < L and T[j] != nxt:
                        j += 1
                    if j >= L:
                        ok = False
                        break
                    ti = j
                    continue
                if ti < L and T[ti] == w:
                    ti += 1
                else:
                    ok = False
                    break
            if ok:
                spans.append((s0, ti - 1))
    return spans


def _sym_score(slots: list[int], slot_words: list[list[str]]) -> float:
    """空位对称：两侧词数相近加分；某个空位只有一个虚词减分。"""
    s = 0.0
    if len(slots) >= 2:
        d = max(slots) - min(slots)
        s += 1.0 if d == 0 else 0.6 if d == 1 else 0.2 if d == 2 else -0.4
    for ws in slot_words:
        if len(ws) == 1 and ws[0] in FUNC_WORDS:
            s -= 1.0
    return s


def _start_ok(T: list[str], start: int) -> bool:
    """句型从句首开始，或前面只有一两个语气词（No / Oh / So / Yeah / Well …）。"""
    if start == 0:
        return True
    return start <= 2 and all(t in INTERJ for t in T[:start])


def _orig_best(qu: Query, sents, table) -> tuple:
    """原句型：原词连续、空位填满、不跨句、不命中排除表。返回 (局部分, 细节, 是否有被排除表拦下的)；对不上细节为 None。"""
    has_slot = "…" in qu.parts
    eq_exact = _eq_exact
    best = None
    excluded_any = False
    for si, (T, isq) in enumerate(sents):
        ms = _match_all(qu.parts, T, eq_exact)
        if not ms:
            continue
        ex = _excl_spans(qu.key, T, table) if has_slot or qu.key in table else []
        for (a, b, fixed, slots) in ms:
            if ex and any(lo <= f <= hi for f in fixed for (lo, hi) in ex):
                excluded_any = True
                continue
            slot_words, k = [], a
            for p in qu.parts:            # 把每个空位的词取出来
                if p == "…":
                    n = slots[len(slot_words)]
                    slot_words.append(T[k:k + n]); k += n
                else:
                    k += 1
            d = dict(tier=TIER_ORIG, sent=si, coverage=(b - a) / len(T), full=(a == 0 and b == len(T)),
                     start=_start_ok(T, a), question=isq, sym=_sym_score(slots, slot_words),
                     nsent=len(sents), slen=len(T), matched=" ".join(T[fixed[0]:fixed[-1] + 1]))
            local = d["coverage"] * 3 + d["start"] + d["sym"] + (1 if d["full"] else 0)
            if best is None or local > best[0]:
                best = (local, d)
    return (best[0], best[1], excluded_any) if best else (None, None, excluded_any)


def analyse(qu: Query, en: str, exact=False, table=None, alt_only=False) -> dict | None:
    """一行字幕对一个句型：返回等级和各项细节（给 _score 用），对不上返回 None。
    alt_only：这行是按另一种写法（缩写互通）取来的候选，只看另一种写法。"""
    table = EXCLUDE if table is None else table
    sents = sentences(en)
    if not sents:
        return None
    words = qu.words
    if not words:
        return None
    has_slot = "…" in qu.parts
    # ---------- 原句型：原词连续、空位填满、不跨句 ----------
    _l, d, excluded_any = (None, None, False) if alt_only else _orig_best(qu, sents, table)
    if d:
        d["unfinished"] = d["sent"] == len(sents) - 1 and _UNFIN.search(en or "") is not None
        return d
    # ---------- 缩写互通：另一种写法（this's / it is / don't ……）按原句型规则命中 → 近似变体 ----------
    if not exact and qu.alts:
        alt = None
        for aq in qu.alts:
            la, da, _x = _orig_best(aq, sents, table)
            if da and (alt is None or la > alt[0]):
                alt = (la, da)
        if alt:
            da = alt[1]
            return dict(tier=TIER_NEAR, alt=True, sent=da["sent"], coverage=da["coverage"], start=da["start"],
                        question=da["question"], order=True, compact=1.0, unfilled=0, cross=False,
                        excluded=False, nsent=da["nsent"], sym=da["sym"], matched=da["matched"])
    if alt_only:
        return None
    # ---------- 近似变体：每个查询词都能用本软件的 stem() 对上 ----------
    sw = [w if exact else stem(w) for w in words]
    cand = None
    for si, (T, isq) in enumerate(sents):
        st = [t if exact else stem(t) for t in T]
        pos = [[i for i, t in enumerate(st) if t == w] for w in sw]
        if not all(pos):
            continue
        # 先找词序正确、跨度最小的一组；没有就各取最近的
        order, span_best, pick = False, None, None
        for i0 in pos[0]:
            cur, ok = [i0], True
            for pl in pos[1:]:
                nx = next((i for i in pl if i > cur[-1]), None)
                if nx is None:
                    ok = False
                    break
                cur.append(nx)
            if ok:
                span = cur[-1] - cur[0] + 1
                if span_best is None or span < span_best:
                    span_best, pick, order = span, cur, True
        if pick is None:
            pick = sorted({p[0] for p in pos})
            span_best = pick[-1] - pick[0] + 1
        lo, hi = min(pick), max(pick)
        # 空位：句型要求的空位在这里有没有词
        unfilled = 0
        if order and has_slot:
            fixed_idx = [i for i, p in enumerate(qu.parts) if p != "…"]
            for k, p in enumerate(qu.parts):
                if p != "…":
                    continue
                prev_f = max((j for j in fixed_idx if j < k), default=None)
                next_f = min((j for j in fixed_idx if j > k), default=None)
                lpos = pick[fixed_idx.index(prev_f)] if prev_f is not None else -1
                rpos = pick[fixed_idx.index(next_f)] if next_f is not None else len(T)
                if rpos - lpos - 1 < SLOT_MIN:
                    unfilled += 1
        d = dict(tier=TIER_NEAR, sent=si, coverage=span_best / len(T), start=_start_ok(T, lo), question=isq,
                 order=order, compact=len(words) / span_best, unfilled=unfilled, cross=False,
                 excluded=excluded_any, nsent=len(sents),
                 matched=" ".join(T[lo:hi + 1]) if order and span_best <= len(words) + 6 else "")
        local = (1 if order else 0) + d["compact"] - unfilled * 1.5
        if cand is None or local > cand[0]:
            cand = (local, d)
    if cand:
        return cand[1]
    # 跨句才凑齐：也算近似变体，但扣分
    allw = [w for T, _ in sents for w in T]
    sall = set(t if exact else stem(t) for t in allw)
    if all(w in sall for w in sw):
        return dict(tier=TIER_NEAR, sent=-1, coverage=0.0, start=False, question=False, order=False,
                    compact=0.0, unfilled=0, cross=True, excluded=excluded_any, nsent=len(sents), matched="")
    return None


def match_span(pattern: str, en: str) -> str:
    """句型在这行字幕里命中的那段原文（给高亮用）。"""
    qu = parse_query(pattern or "")
    if not qu.words:
        return ""
    d = analyse(qu, en)
    return d["matched"] if d else ""


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
    src: str = "sub"         # sub 字幕 / join 跨两行拼起来 / auto 语音识别补出

    @property
    def pct(self) -> int:
        """给人看的匹配度（0–100）。"""
        return max(1, min(100, int(round(self.score / SCORE_FULL * 100))))

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

    def excludes(self) -> dict[str, list[str]]:
        table = {parse_query(k).key: list(v) for k, v in EXCLUDE.items()}
        f = self.lib.data / "excludes.json"
        if f.exists():
            try:
                for k, v in json.loads(f.read_text("utf-8")).items():
                    table.setdefault(parse_query(k).key, [])
                    table[parse_query(k).key] += [x for x in v if x not in table[parse_query(k).key]]
            except Exception:
                pass
        return table

    def _candidates(self, words, exact: bool, filters: dict, limit=20000, expr: str | None = None):
        if not words and not expr:
            return []
        table = "fts_exact" if exact else "fts_stem"
        sql = f"""SELECT c.*, m.key media_key, m.show, m.season, m.episode, m.kind,
                         m.rel_video IS NOT NULL has_video
                  FROM {table} f JOIN cues c ON c.id=f.rowid JOIN media m ON m.id=c.media_id
                  WHERE {table} MATCH ?"""
        args = [("en : (" + (expr or _fts_expr(words)) + ")")]
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
        if not filters.get("machine"):
            sql += " AND COALESCE(c.src,'sub')!='auto'"       # 识别补出的台词：勾了「含识别台词」才搜
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
        table = self.excludes()
        hits: dict[tuple, Hit] = {}

        def consider(row, q_=qu, func=False, alt_only=False):
            en = row["en"]
            src = row["src"] if "src" in row.keys() else "sub"
            d = analyse(q_, en, exact, table, alt_only)
            if d is None:
                return
            if src == "join":
                # 拼起来的句子只在「句型正好跨过两行」时算：任何一行单独就能命中的，交给那一行；
                # 近似变体要词序对、挨得近才算（不然两行随便凑齐几个词也会进来）
                if d["tier"] != TIER_ORIG and not (d.get("order") and d.get("compact", 0) >= 0.5):
                    return
                cut = row["split"] or 0
                for part in (en[:cut], en[cut:]):
                    dp = analyse(q_, part, exact, table)
                    if dp is not None and not dp.get("cross"):
                        return
            if func:
                # 功能等价：替代说法要原样出现，或词序对且挨得近（不然「His case is that he followed your advice」也算）
                if d["tier"] != TIER_ORIG and not (d.get("order") and d.get("compact", 0) >= 0.6):
                    return
                d = dict(d, tier=TIER_FUNC)
            if ordered and d["tier"] == TIER_NEAR and not d.get("order"):
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
            s = self._score(row, d, dur, q_)
            h = Hit(row["id"], row["media_id"], row["media_key"], row["show"], row["season"], row["episode"],
                    row["kind"], row["idx"], row["start"], row["end"], en, row["zh"] or "",
                    row["speakers"], row["a_start"], row["a_end"], row["a_score"], bool(row["has_video"]),
                    d["tier"], s, d.get("matched", ""), src or "sub")
            dk = (row["media_id"], " ".join(norm_tokens(en)))           # 同集同句只留一条
            old = hits.get(dk)
            if old is None or (_tier_rank(h.tier), -h.score) < (_tier_rank(old.tier), -old.score):
                hits[dk] = h

        seen = set()
        for row in self._candidates(qu.words, exact, filters):
            seen.add(row["id"])
            consider(row)
        if not exact:
            # 缩写互通：另一种写法按短语取候选，analyse 里命中的定为近似变体
            for aq in qu.alts:
                for row in self._candidates(aq.words, False, filters, expr=_phrase_expr(aq.parts)):
                    if row["id"] in seen:
                        continue
                    seen.add(row["id"])
                    old = hits.get((row["media_id"], " ".join(norm_tokens(row["en"]))))
                    if old is not None and old.tier == TIER_ORIG:
                        continue
                    consider(row, alt_only=True)

        if include_func and not exact:
            eqs = self.equivalents()
            k1 = qu.key.replace("…", "*")
            k2 = " ".join(p for p in qu.parts).strip("… ").replace("…", "*")
            alts = eqs.get(k1) or eqs.get(k2) or eqs.get(k2 + " *") or []
            for alt in alts:
                aq = parse_query(alt)
                if not aq.words:
                    continue
                for row in self._candidates(aq.words, False, filters):
                    key = (row["media_id"], " ".join(norm_tokens(row["en"])))
                    if key in hits:
                        continue
                    consider(row, aq, True)

        out = sorted(hits.values(), key=lambda h: (_tier_rank(h.tier), -h.score))
        return out

    @staticmethod
    def _score(row, d: dict, dur: float, qu: Query | None = None) -> float:
        s = TIER_BONUS.get(d["tier"], 0.0)
        if row["speakers"] <= 1:
            s += 2.0
        # 时长 1.2–6 秒最好用
        if 1.2 <= dur <= 6:
            s += 2.0
        elif dur < 1.2:
            s += 0.5
        else:
            s -= min(3.0, (dur - 6) * 0.5)
        if d["tier"] == TIER_NEAR:
            # 近似变体：词序对、词挨得近加分；句型要求的空位空着扣分；跨句才凑齐扣分
            s += 1.0 if d.get("order") else 0.0
            s += 1.0 * d.get("compact", 0.0)
            s += 1.5 * d.get("coverage", 0.0)
            s -= 1.5 * d.get("unfilled", 0)
            if d.get("cross"):
                s -= 2.0
            if d.get("excluded"):
                s -= 1.0
            if d.get("alt"):
                s += 0.5 + max(0.0, d.get("sym", 0.0))   # 缩写互通：就是同一句型换个写法，排在一般近似前面
        else:
            # 原句型 / 功能等价：句型及其空位占所在句子的比例，占满最好
            s += 3.0 * d.get("coverage", 0.0)
            if d.get("full"):
                s += 1.0
            s += d.get("sym", 0.0)
            # 句子短一点更好用（7 个词以上每多一个词 −0.1，最多 −0.8）
            s -= min(0.8, 0.1 * max(0, d.get("slen", 0) - 6))
            if d.get("unfinished"):
                s -= 0.5          # 句型后面话没说完
        if d.get("start"):
            s += 1.0
        if qu is not None and qu.question and d.get("sent", -1) >= 0:
            s += 1.0 if d.get("question") else -0.3
        extra_sent = max(0, d.get("nsent", 1) - 1)
        s -= min(0.75, 0.25 * extra_sent)
        en = row["en"]
        if re.search(r"\b(\w+)\b[ ,.-]+\1\b", en.lower()):   # 结巴、重复
            s -= 1.5
        if row["a_start"] is not None and (row["a_score"] or 0) >= 0.5:
            s += 0.5 + 0.5 * (row["a_score"] or 0)
        if row["zh"]:
            s += 0.3
        if row["has_video"]:
            s += 2.5          # 有视频、能直接预览的排前面
        return round(s, 3)


def _tier_rank(t):
    return {TIER_ORIG: 0, TIER_NEAR: 1, TIER_FUNC: 2}.get(t, 3)
