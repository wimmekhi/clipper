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
        found = self.discover()
        stats = dict(new=0, updated=0, removed=0, total=len(found))
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
            else:
                mid = row["id"]
                if row["video_sig"] != vid_sig and vid:
                    # 换了视频文件 → 对齐作废
                    self.db.execute("UPDATE media SET align_status='none' WHERE id=?", (mid,))
                self.db.execute("UPDATE media SET rel_video=?,rel_sub=?,rel_sub_zh=?,video_sig=? WHERE id=?",
                                (rel(vid), rel(sub), rel(zh), vid_sig, mid))
            if sub and (row is None or row["sub_sig"] != sub_sig):
                self._index_subs(mid, sub, zh)
                self.db.execute("UPDATE media SET sub_sig=? WHERE id=?", (sub_sig, mid))
                if row is not None:
                    stats["updated"] += 1
                    self.db.execute("UPDATE media SET align_status='none' WHERE id=?", (mid,))
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
            cur = self.db.execute(
                "INSERT INTO cues(media_id,idx,start,end,en,zh,speakers) VALUES(?,?,?,?,?,?,?)",
                (mid, c.idx, c.start, c.end, c.en, c.zh, c.speakers))
            rid = cur.lastrowid
            self.db.execute("INSERT INTO fts_exact(rowid,en,zh) VALUES(?,?,?)", (rid, c.en, c.zh))
            self.db.execute("INSERT INTO fts_stem(rowid,en) VALUES(?,?)", (rid, c.en))

    # ---------- 对齐结果 ----------
    def apply_alignment(self, mid: int, data: dict):
        rows = data.get("cues", [])
        self.db.executemany(
            "UPDATE cues SET a_start=?,a_end=?,a_score=? WHERE media_id=? AND idx=?",
            [(r["a_start"], r["a_end"], r["score"], mid, r["idx"]) for r in rows])
        self.db.execute("UPDATE media SET align_status='done',align_msg=?,aligned_at=? WHERE id=?",
                        (data.get("summary", ""), time.time(), mid))
        self.db.commit()

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

    def context(self, media_id: int, idx: int, n=1):
        return self.db.execute("SELECT * FROM cues WHERE media_id=? AND idx BETWEEN ? AND ? ORDER BY idx",
                               (media_id, idx - n, idx + n)).fetchall()
