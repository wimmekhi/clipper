"""媒体库：扫描固定文件夹（剧名/S01/S01E01.mp4 + 同名字幕），建 SQLite 库与全文索引。

库数据放在媒体库根目录下的 .clipper/ 里，路径一律相对根目录存，换电脑/盘符照样能用。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from . import subs

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm"}
SUB_EXT = (".srt", ".ass", ".ssa")
DATA_DIR = ".clipper"
INDEX_VER = 3            # 1.3.0：全大写字幕改成正常大小写、&#x27; 这类转义还原（重建索引，对齐结果原样接回）
JOIN_BASE = 1_000_000    # 跨两条字幕拼起来的句子：idx = JOIN_BASE + 前一条的 idx
AUTO_BASE = 2_000_000    # 字幕里没有、语音识别补出来的台词：idx = AUTO_BASE + 序号
_END_PUNCT = re.compile(r"""[.?!…♪]["'”’)\]]*\s*$""")

SCHEMA = """
CREATE TABLE IF NOT EXISTS media(
  id INTEGER PRIMARY KEY,
  key TEXT UNIQUE,              -- 相对路径去扩展名，例如 Modern Family/S02/S02E05
  show TEXT, season INTEGER, episode INTEGER, kind TEXT, title TEXT,
  rel_video TEXT, rel_sub TEXT, rel_sub_zh TEXT,
  sub_sig TEXT, video_sig TEXT, duration REAL,
  align_status TEXT DEFAULT 'none',  -- none/queued/running/done/error
  align_msg TEXT, aligned_at REAL
);
CREATE TABLE IF NOT EXISTS cues(
  id INTEGER PRIMARY KEY,
  media_id INTEGER, idx INTEGER,
  start REAL, end REAL, en TEXT, zh TEXT, speakers INTEGER,
  a_start REAL, a_end REAL, a_score REAL,
  UNIQUE(media_id, idx)
);
CREATE INDEX IF NOT EXISTS cues_media ON cues(media_id, idx);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_exact USING fts5(en, zh, tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS fts_stem USING fts5(en, tokenize='porter unicode61');
-- 人工微调：按 媒体key + 字幕序号 存，重新对齐/重建索引都不覆盖
CREATE TABLE IF NOT EXISTS adjust(
  media_key TEXT, idx INTEGER, start REAL, end REAL, updated REAL,
  PRIMARY KEY(media_key, idx)
);
CREATE TABLE IF NOT EXISTS packs(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, created REAL
);
CREATE TABLE IF NOT EXISTS pack_items(
  id INTEGER PRIMARY KEY, pack_id INTEGER, media_key TEXT, idx INTEGER,
  pattern TEXT, tier TEXT, pos INTEGER, added REAL,
  UNIQUE(pack_id, media_key, idx)
);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""

_EP = re.compile(r"[Ss](\d{1,2})[ ._-]*[Ee](\d{1,3})")
_EPX = re.compile(r"(?<![\dA-Za-z])(\d{1,2})x(\d{2,3})(?!\d)")
_YEAR = re.compile(r"\((\d{4})\)")
_JUNK = re.compile(r"(?i)(^|[ ._-])(sample|trailer|featurette)([ ._-]|$)")
_TAGS_BI = re.compile(r"(?i)双语|中英|英中|简英|繁英|英简|英繁|chs[&+._-]?eng|cht[&+._-]?eng|eng[&+._-]?ch[st]|zh[&+_-]en|en[&+_-]zh|chi[&+._-]?eng")
_TAGS_EN = re.compile(r"(?i)(^|[ ._\-\[(])(en|eng|english|英文|英语|英)([ ._\-\])]|$)")
_TAGS_ZH = re.compile(r"(?i)(^|[ ._\-\[(])(zh|chs|cht|chi|zho|sc|tc|gb|big5|简体|繁体|中文|简|繁|zh-cn|zh-tw|zh-hans|zh-hant)([ ._\-\])]|$)")
_TAGS_TRAD = re.compile(r"(?i)cht|繁|big5|\btc\b|zh-tw|hant")
_LANG_TAIL = re.compile(r"(?i)(\.(zh|chs|cht|chi|zho|sc|tc|en|eng|default|forced|sdh|zh-cn|zh-tw|chs&eng|cht&eng|简体|繁体|英文|双语))+$")


def _episode(name: str):
    m = _EP.search(name) or _EPX.search(name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _strip_lang_tail(stem: str) -> str:
    return _LANG_TAIL.sub("", stem)


def _video_rank(p: Path):
    s = str(p)
    try:
        size = p.stat().st_size
    except OSError:
        size = 1 << 62
    return (0 if "压制" in s else 1, 0 if p.suffix.lower() == ".mp4" else 1, size)


def _sniff(p: Path) -> str:
    """看字幕内容：bi / en / zh。只读前 64 KB。"""
    try:
        raw = p.open("rb").read(65536)
    except OSError:
        return "?"
    for enc in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            t = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        t = raw.decode("latin-1")
    cjk = len(re.findall(r"[\u4e00-\u9fff]", t))
    lat = len(re.findall(r"[A-Za-z]{3,}", re.sub(r"\{[^}]*\}|Dialogue:|Style:|Format:|\\N", " ", t)))
    if cjk > 50 and lat > 50:
        return "bi"
    if cjk > 50:
        return "zh"
    return "en" if lat > 20 else "?"


def _sub_lang(p: Path) -> str:
    n = p.name
    if _TAGS_BI.search(n):
        return "bi"
    en, zh = bool(_TAGS_EN.search(n)), bool(_TAGS_ZH.search(n))
    if en and zh:
        return "bi"
    if en:
        return "en"
    if zh:
        return "zh"
    return _sniff(p)


def _pick_subs(cands: list[Path]):
    """返回 (主字幕, 中文字幕)。主字幕：双语 > 英文 > 不明；同级 .srt 优先，简体优先，路径短的优先。"""
    if not cands:
        return None, None
    tagged = [(p, _sub_lang(p)) for p in cands]
    def rk(x):
        p, _ = x
        return (1 if _TAGS_TRAD.search(p.name) else 0, 0 if p.suffix.lower() == ".srt" else 1, len(str(p)))
    main = None
    for want in ("bi", "en", "?"):
        pool = [x for x in tagged if x[1] == want]
        if pool:
            main = min(pool, key=rk)[0]
            break
    zh = None
    zpool = [x for x in tagged if x[1] == "zh"]
    if zpool and (main is None or _sub_lang(main) != "bi"):
        zh = min(zpool, key=rk)[0]
    if main is None and zh is not None:   # 只有中文字幕：也收进来，能搜中文
        main, zh = zh, None
    return main, zh


class Library:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.data = self.root / DATA_DIR
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "align").mkdir(exist_ok=True)
        (self.data / "wave").mkdir(exist_ok=True)
        self.db_path = self.data / "library.db"
        self.db = self.connect()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        # exFAT 移动硬盘上不用 WAL，用传统日志更稳
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=FULL")
        db.executescript(SCHEMA)
        # 1.0.7 新列（老库直接补上）
        have = {r[1] for r in db.execute("PRAGMA table_info(cues)")}
        if "src" not in have:
            db.execute("ALTER TABLE cues ADD COLUMN src TEXT DEFAULT 'sub'")   # sub / join / auto
        if "split" not in have:
            db.execute("ALTER TABLE cues ADD COLUMN split INTEGER")            # join：前半句的字符数
        have = {r[1] for r in db.execute("PRAGMA table_info(media)")}
        if "sub_changed" not in have:
            db.execute("ALTER TABLE media ADD COLUMN sub_changed INTEGER DEFAULT 0")
        db.commit()
        return db

    # ---------- 路径 ----------
    def abs(self, rel: str | None) -> Path | None:
        return (self.root / rel) if rel else None

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def align_file(self, key: str) -> Path:
        return self.data / "align" / (key.replace("/", "__") + ".json")

    # ---------- 设置 ----------
    def get_setting(self, k, default=None):
        r = self.db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))
        self.db.commit()

    # ---------- 扫描 ----------
    def discover(self) -> dict[str, dict]:
        """找出所有「一集」。

        剧集：同一个顶层文件夹（=剧名）里，按文件名里的 S01E02 / 1x02 认集，视频和字幕在哪个子目录、
        叫什么名字都行（字幕组发布包、压制版另放一个文件夹也能配上）。一集有多个字幕时按语言挑：
        双语 > 英文 > 不明；纯中文的单独作译文。一集有多个视频时：路径带「压制」优先，再 mp4，再文件小的。
        电影/认不出集数的：同目录同名配对（旧规则）。
        """
        eps: dict[str, dict] = {}
        loose: dict[str, dict] = {}
        for p in self.root.rglob("*"):
            if DATA_DIR in p.parts or p.name.startswith(".") or not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext not in VIDEO_EXT and ext not in SUB_EXT:
                continue
            if ext in VIDEO_EXT and _JUNK.search(p.name):
                continue
            rel = p.relative_to(self.root)
            ep = _episode(p.name)
            if ep and len(rel.parts) >= 2:
                key = f"{rel.parts[0]}/S{ep[0]:02d}E{ep[1]:02d}"
                bucket = eps.setdefault(key, {"v": [], "s": []})
            else:
                stem = p.name[: -len(p.suffix)]
                if ext in SUB_EXT:
                    stem = _strip_lang_tail(stem)
                key = (p.parent / stem).relative_to(self.root).as_posix()
                bucket = loose.setdefault(key, {"v": [], "s": []})
            bucket["v" if ext in VIDEO_EXT else "s"].append(p)
        items = {}
        for key, b in list(eps.items()) + list(loose.items()):
            it = {}
            if b["v"]:
                it["video"] = min(b["v"], key=_video_rank)
            main, zh = _pick_subs(b["s"])
            if main:
                it["sub"] = main
            if zh:
                it["sub_zh"] = zh
            if it:
                items[key] = it
        return items

    @staticmethod
    def parse_key(key: str):
        parts = key.split("/")
        name = parts[-1]
        m = _EP.search(name)
        if m:
            show = parts[0] if len(parts) > 1 else _EP.split(name)[0].strip(" ._-") or name
            return dict(kind="tv", show=show, season=int(m.group(1)), episode=int(m.group(2)),
                        title=name)
        y = _YEAR.search(name)
        show = parts[-2] if len(parts) > 1 and parts[0] not in ("电影", "Movies", "movies") else name
        if len(parts) >= 2 and parts[0] in ("电影", "Movies", "movies"):
            show = parts[1]
        return dict(kind="movie", show=show, season=0, episode=0, title=name if not y else name)

    @staticmethod
    def _sig(p: Path | None) -> str:
        if not p:
            return ""
        st = p.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"

    def scan(self, progress=None) -> dict:
        if int(self.get_setting("index_ver", 1) or 1) < INDEX_VER:
            self.reindex_all(progress)
        found = self.discover()
        stats = dict(new=0, updated=0, removed=0, total=len(found), new_keys=[])
        existing = {r["key"]: r for r in self.db.execute("SELECT * FROM media")}
        for n, (key, it) in enumerate(sorted(found.items())):
            if progress:
                progress(n, len(found), key)
            meta = self.parse_key(key)
            sub, zh, vid = it.get("sub"), it.get("sub_zh"), it.get("video")
            sub_sig = self._sig(sub) + "|" + self._sig(zh)
            vid_sig = self._sig(vid)
            row = existing.get(key)
            rel = lambda p: self.rel(p) if p else None
            if row is None:
                cur = self.db.execute(
                    "INSERT INTO media(key,show,season,episode,kind,title,rel_video,rel_sub,rel_sub_zh,sub_sig,video_sig)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (key, meta["show"], meta["season"], meta["episode"], meta["kind"], meta["title"],
                     rel(vid), rel(sub), rel(zh), "", vid_sig))
                mid = cur.lastrowid
                stats["new"] += 1
                stats["new_keys"].append(key)
            else:
                mid = row["id"]
                if row["video_sig"] != vid_sig and vid:
                    # 换了视频文件 → 对齐作废
                    self.db.execute("UPDATE media SET align_status='none', sub_changed=? WHERE id=?",
                                    (1 if row["align_status"] == "done" else 0, mid))
                self.db.execute("UPDATE media SET rel_video=?,rel_sub=?,rel_sub_zh=?,video_sig=? WHERE id=?",
                                (rel(vid), rel(sub), rel(zh), vid_sig, mid))
            if sub and (row is None or row["sub_sig"] != sub_sig):
                self._index_subs(mid, sub, zh)
                self.db.execute("UPDATE media SET sub_sig=? WHERE id=?", (sub_sig, mid))
                if row is not None:
                    stats["updated"] += 1
                    # 字幕换了：之前的对齐不再对得上，标出来让用户一键重新对齐
                    self.db.execute("UPDATE media SET align_status='none', sub_changed=? WHERE id=?",
                                    (1 if row["align_status"] == "done" or row["sub_changed"] else 0, mid))
            self.db.commit()
        for key, row in existing.items():
            if key not in found:
                self._drop_media(row["id"])
                stats["removed"] += 1
        self.db.commit()
        # 对齐文件已在（比如另一台电脑跑完的），直接认领
        for r in self.db.execute("SELECT id,key,align_status FROM media WHERE align_status!='done'").fetchall():
            f = self.align_file(r["key"])
            if f.exists():
                try:
                    self.apply_alignment(r["id"], json.loads(f.read_text("utf-8")))
                except Exception:
                    pass
        return stats

    def reindex_all(self, progress=None):
        """索引格式升级：按新规则重建全部字幕索引，已有的对齐结果原样接回去（不用重新对齐）。"""
        rows = self.db.execute("SELECT * FROM media WHERE rel_sub IS NOT NULL").fetchall()
        for n, r in enumerate(rows):
            if progress:
                progress(n, len(rows), "升级索引 " + r["key"])
            sub = self.abs(r["rel_sub"])
            if not sub or not sub.exists():
                continue
            zh = self.abs(r["rel_sub_zh"])
            try:
                self._index_subs(r["id"], sub, zh if zh and zh.exists() else None)
            except Exception:
                continue
            f = self.align_file(r["key"])
            if r["align_status"] == "done" and f.exists():
                try:
                    self.apply_alignment(r["id"], json.loads(f.read_text("utf-8")), commit=False)
                except Exception:
                    pass
            if n % 50 == 0:
                self.db.commit()
        self.db.commit()
        self.set_setting("index_ver", INDEX_VER)

    def changed_keys(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT key FROM media WHERE sub_changed=1 ORDER BY key")]

    def _drop_media(self, mid: int):
        ids = [r[0] for r in self.db.execute("SELECT id FROM cues WHERE media_id=?", (mid,))]
        self.db.executemany("DELETE FROM fts_exact WHERE rowid=?", [(i,) for i in ids])
        self.db.executemany("DELETE FROM fts_stem WHERE rowid=?", [(i,) for i in ids])
        self.db.execute("DELETE FROM cues WHERE media_id=?", (mid,))
        self.db.execute("DELETE FROM media WHERE id=?", (mid,))

    def _index_subs(self, mid: int, sub: Path, zh: Path | None):
        ids = [r[0] for r in self.db.execute("SELECT id FROM cues WHERE media_id=?", (mid,))]
        self.db.executemany("DELETE FROM fts_exact WHERE rowid=?", [(i,) for i in ids])
        self.db.executemany("DELETE FROM fts_stem WHERE rowid=?", [(i,) for i in ids])
        self.db.execute("DELETE FROM cues WHERE media_id=?", (mid,))
        cues = subs.load_cues(sub, zh)
        for c in cues:
            self._add_cue(mid, c.idx, c.start, c.end, c.en, c.zh, c.speakers)
        # 一句话被字幕拆成两行（前一行没有句末标点、两行挨着）：另存一条拼起来的，搜整句也能命中
        for a, b in zip(cues, cues[1:]):
            if not a.en or not b.en or b.start - a.end > 1.0 or _END_PUNCT.search(a.en):
                continue
            self._add_cue(mid, JOIN_BASE + a.idx, a.start, b.end, a.en + " " + b.en,
                          " ".join(x for x in (a.zh, b.zh) if x), max(a.speakers, b.speakers), "join", len(a.en))

    def _add_cue(self, mid, idx, start, end, en, zh, speakers, src="sub", split=None, a=None):
        a = a or (None, None, None)
        cur = self.db.execute(
            "INSERT OR REPLACE INTO cues(media_id,idx,start,end,en,zh,speakers,src,split,a_start,a_end,a_score)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (mid, idx, start, end, en, zh, speakers, src, split, *a))
        rid = cur.lastrowid
        self.db.execute("INSERT INTO fts_exact(rowid,en,zh) VALUES(?,?,?)", (rid, en, zh))
        self.db.execute("INSERT INTO fts_stem(rowid,en) VALUES(?,?)", (rid, en))
        return rid

    # ---------- 对齐结果 ----------
    def apply_alignment(self, mid: int, data: dict, commit=True):
        rows = data.get("cues", [])
        self.db.executemany(
            "UPDATE cues SET a_start=?,a_end=?,a_score=? WHERE media_id=? AND idx=?",
            [(r["a_start"], r["a_end"], r["score"], mid, r["idx"]) for r in rows])
        # 拼起来的句子：前半句的起点 → 后半句的终点
        by = {r["idx"]: r for r in rows}
        upd = []
        for j in self.db.execute("SELECT idx FROM cues WHERE media_id=? AND src='join'", (mid,)).fetchall():
            a, b = by.get(j[0] - JOIN_BASE), None
            if a is not None:
                nxt = self.db.execute("SELECT idx FROM cues WHERE media_id=? AND src='sub' AND idx>? ORDER BY idx "
                                      "LIMIT 1", (mid, a["idx"])).fetchone()
                b = by.get(nxt[0]) if nxt else None
            if a and b and a["a_start"] is not None and b["a_end"] is not None:
                upd.append((a["a_start"], b["a_end"], min(a["score"], b["score"]), mid, j[0]))
        self.db.executemany("UPDATE cues SET a_start=?,a_end=?,a_score=? WHERE media_id=? AND idx=?", upd)
        self._add_auto_lines(mid, data.get("words", []))
        self.db.execute("UPDATE media SET align_status='done',align_msg=?,aligned_at=?,sub_changed=0 WHERE id=?",
                        (data.get("summary", ""), time.time(), mid))
        if commit:
            self.db.commit()

    def _add_auto_lines(self, mid: int, words: list[dict]):
        """字幕没写到的说话（漏翻、歌词、字幕删掉的口头禅）：识别出来的词里不在任何字幕时间段内的，连成一句一句。"""
        old = [r[0] for r in self.db.execute("SELECT id FROM cues WHERE media_id=? AND src='auto'", (mid,))]
        self.db.executemany("DELETE FROM fts_exact WHERE rowid=?", [(i,) for i in old])
        self.db.executemany("DELETE FROM fts_stem WHERE rowid=?", [(i,) for i in old])
        self.db.execute("DELETE FROM cues WHERE media_id=? AND src='auto'", (mid,))
        if not words:
            return
        spans = []
        for r in self.db.execute("SELECT start,end,a_start,a_end FROM cues WHERE media_id=? AND src='sub'", (mid,)):
            spans.append((r["start"], r["end"]))
            if r["a_start"] is not None:
                spans.append((r["a_start"], r["a_end"]))
        spans.sort()
        import bisect
        starts = [s for s, _ in spans]
        reach, mx = [], -1.0          # 前缀最远终点：判断某时刻有没有被字幕盖住
        for _, e in spans:
            mx = max(mx, e); reach.append(mx)

        def covered(t0, t1):
            k = bisect.bisect_right(starts, t1 + 0.25) - 1
            return k >= 0 and reach[k] >= t0 - 0.25
        groups, cur = [], []
        for w in words:
            if not w.get("w") or covered(w["start"], w["end"]):
                if cur:
                    groups.append(cur); cur = []
                continue
            if cur and w["start"] - cur[-1]["end"] > 0.9:
                groups.append(cur); cur = []
            cur.append(w)
        if cur:
            groups.append(cur)
        n = 0
        for g in groups:
            text = " ".join(x["w"] for x in g).strip()
            if len(g) < 3 or len(re.findall(r"[A-Za-z]+", text)) < 3:
                continue
            n += 1
            self._add_cue(mid, AUTO_BASE + n, g[0]["start"], g[-1]["end"], text, "", 1, "auto",
                          a=(g[0]["start"], g[-1]["end"], 0.9))

    def words(self, key: str) -> list[dict]:
        f = self.align_file(key)
        if not f.exists():
            return []
        st = f.stat().st_mtime
        c = getattr(self, "_wcache", {})
        if key in c and c[key][0] == st:
            return c[key][1]
        w = json.loads(f.read_text("utf-8")).get("words", [])
        c[key] = (st, w)
        self._wcache = c
        return w

    def final_span(self, cue_row, key: str, pre=None, post=None):
        """最终切点（含前后余量）。余量不越过相邻的语音词；人工微调的原样返回。"""
        if pre is None:
            pre = float(self.get_setting("pad_pre", 0.30))
        if post is None:
            post = float(self.get_setting("pad_post", 0.35))
        s, e, src = self.clip_span(cue_row, key)
        if src == "manual":
            return s, e, src
        if src == "subtitle":
            return max(0.0, s), e, src
        import bisect
        words = self.words(key)
        if words:
            ends = [w["end"] for w in words]
            starts = [w["start"] for w in words]
            k = bisect.bisect_right(ends, s + 0.02) - 1
            prev_end = ends[k] if k >= 0 else None
            k2 = bisect.bisect_left(starts, e - 0.02)
            next_start = starts[k2] if k2 < len(starts) else None
        else:
            prev_end = next_start = None
        s2 = s - pre
        if prev_end is not None and prev_end > s2:
            s2 = min(s, max(s2, (prev_end + s) / 2))
        e2 = e + post
        if next_start is not None and next_start < e2:
            e2 = max(e, min(e2, (next_start + e) / 2))
        return max(0.0, s2), e2, src

    # ---------- 片段时间 ----------
    def clip_span(self, cue_row, key: str) -> tuple[float, float, str]:
        """返回 (起, 止, 来源)：人工微调 > 对齐 > 字幕原时间。"""
        adj = self.db.execute("SELECT start,end FROM adjust WHERE media_key=? AND idx=?",
                              (key, cue_row["idx"])).fetchone()
        if adj:
            return adj["start"], adj["end"], "manual"
        if cue_row["a_start"] is not None and (cue_row["a_score"] or 0) >= 0.5:
            return cue_row["a_start"], cue_row["a_end"], "aligned"
        if cue_row["a_start"] is not None:
            return cue_row["a_start"], cue_row["a_end"], "estimated"
        return cue_row["start"], cue_row["end"], "subtitle"

    def save_adjust(self, key: str, idx: int, start: float, end: float):
        self.db.execute("INSERT OR REPLACE INTO adjust VALUES(?,?,?,?,?)", (key, idx, start, end, time.time()))
        self.db.commit()

    def clear_adjust(self, key: str, idx: int):
        self.db.execute("DELETE FROM adjust WHERE media_key=? AND idx=?", (key, idx))
        self.db.commit()

    # ---------- 采集包 ----------
    def packs(self):
        return self.db.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM pack_items i WHERE i.pack_id=p.id) n FROM packs p ORDER BY created").fetchall()

    def pack_id(self, name: str) -> int:
        r = self.db.execute("SELECT id FROM packs WHERE name=?", (name,)).fetchone()
        if r:
            return r[0]
        cur = self.db.execute("INSERT INTO packs(name,created) VALUES(?,?)", (name, time.time()))
        self.db.commit()
        return cur.lastrowid

    def add_to_pack(self, pack: str, key: str, idx: int, pattern="", tier="") -> bool:
        pid = self.pack_id(pack)
        pos = self.db.execute("SELECT COALESCE(MAX(pos),0)+1 FROM pack_items WHERE pack_id=?", (pid,)).fetchone()[0]
        cur = self.db.execute(
            "INSERT OR IGNORE INTO pack_items(pack_id,media_key,idx,pattern,tier,pos,added) VALUES(?,?,?,?,?,?,?)",
            (pid, key, idx, pattern, tier, pos, time.time()))
        self.db.commit()
        return cur.rowcount > 0

    def pack_items(self, pack_id: int):
        return self.db.execute(
            """SELECT i.*, m.id media_id, m.show, m.season, m.episode, m.kind, m.title, m.rel_video,
                      c.id cue_id, c.idx, c.start, c.end, c.en, c.zh, c.speakers, c.a_start, c.a_end, c.a_score
               FROM pack_items i JOIN media m ON m.key=i.media_key
               JOIN cues c ON c.media_id=m.id AND c.idx=i.idx
               WHERE i.pack_id=? ORDER BY i.pos""", (pack_id,)).fetchall()

    def move_item(self, item_id: int, delta: int):
        it = self.db.execute("SELECT pack_id,pos FROM pack_items WHERE id=?", (item_id,)).fetchone()
        if not it:
            return
        other = self.db.execute(
            "SELECT id,pos FROM pack_items WHERE pack_id=? AND pos %s ? ORDER BY pos %s LIMIT 1"
            % ((">", "ASC") if delta > 0 else ("<", "DESC")), (it["pack_id"], it["pos"])).fetchone()
        if other:
            self.db.execute("UPDATE pack_items SET pos=? WHERE id=?", (other["pos"], item_id))
            self.db.execute("UPDATE pack_items SET pos=? WHERE id=?", (it["pos"], other["id"]))
            self.db.commit()

    def remove_item(self, item_id: int):
        self.db.execute("DELETE FROM pack_items WHERE id=?", (item_id,))
        self.db.commit()

    # ---------- 统计 ----------
    def shows(self):
        return self.db.execute(
            """SELECT show, kind, COUNT(*) n,
                      SUM(align_status='done') aligned,
                      SUM(rel_video IS NOT NULL) has_video
               FROM media GROUP BY show ORDER BY show""").fetchall()

    def media_by_key(self, key):
        return self.db.execute("SELECT * FROM media WHERE key=?", (key,)).fetchone()

    def script(self, media_id: int):
        """整集台词本：字幕 + 识别补出的，按时间排（不含拼接出来的句子）。"""
        return self.db.execute("SELECT * FROM cues WHERE media_id=? AND COALESCE(src,'sub')!='join' "
                               "ORDER BY COALESCE(a_start,start)", (media_id,)).fetchall()

    def context(self, media_id: int, idx: int, n=1):
        rows = self.script(media_id)
        if idx >= JOIN_BASE and idx < AUTO_BASE:
            idx -= JOIN_BASE
        k = next((i for i, r in enumerate(rows) if r["idx"] == idx), None)
        if k is None:
            return []
        return rows[max(0, k - n): k + n + 1]

    def episodes(self, show: str | None = None):
        sql = ("SELECT m.*, (SELECT COUNT(*) FROM cues c WHERE c.media_id=m.id AND c.src='sub') n_cues, "
               "(SELECT COUNT(*) FROM cues c WHERE c.media_id=m.id AND c.src='auto') n_auto FROM media m")
        if show:
            return self.db.execute(sql + " WHERE m.show=? ORDER BY m.season, m.episode, m.key", (show,)).fetchall()
        return self.db.execute(sql + " ORDER BY m.show, m.season, m.episode, m.key").fetchall()
