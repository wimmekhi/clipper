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
  id INTEGER PRIMARY KEY, name TEXT, created REAL,
  folder_id INTEGER,            -- 1.3.4：所在文件夹，NULL = 最外层（未分类）
  sort INTEGER DEFAULT 0
);
-- 1.3.4：采集包文件夹，可以无限套娃（常用三级，例如 NCE1/L1-L2/is this your…）
CREATE TABLE IF NOT EXISTS folders(
  id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT, sort INTEGER DEFAULT 0, created REAL
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
        self._migrate_packs(db)
        db.commit()
        return db

    @staticmethod
    def _migrate_packs(db):
        """1.3.4：包名不再全局唯一（同名包可以在不同文件夹里），加 folder_id / sort。老包全部放最外层。"""
        sql = (db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='packs'").fetchone() or [""])[0]
        cols = {r[1] for r in db.execute("PRAGMA table_info(packs)")}
        if "UNIQUE" in sql.upper() or "folder_id" not in cols:
            db.execute("CREATE TABLE packs_v134(id INTEGER PRIMARY KEY, name TEXT, created REAL, "
                       "folder_id INTEGER, sort INTEGER DEFAULT 0)")
            db.execute("INSERT INTO packs_v134(id,name,created,folder_id,sort) "
                       "SELECT id,name,created,NULL,0 FROM packs ORDER BY created, id")
            db.execute("DROP TABLE packs")
            db.execute("ALTER TABLE packs_v134 RENAME TO packs")
            n = 0
            for r in db.execute("SELECT id FROM packs ORDER BY created, id").fetchall():
                n += 1
                db.execute("UPDATE packs SET sort=? WHERE id=?", (n, r[0]))
        db.execute("CREATE INDEX IF NOT EXISTS packs_folder ON packs(folder_id)")
        db.execute("CREATE INDEX IF NOT EXISTS folders_parent ON folders(parent_id)")
        db.execute("CREATE INDEX IF NOT EXISTS pack_items_pack ON pack_items(pack_id, pos)")
        db.execute("CREATE INDEX IF NOT EXISTS pack_items_key ON pack_items(media_key, idx)")

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
    # ---------- 扫描范围（1.3.2：导出文件夹不算媒体） ----------
    def export_dirs(self) -> list[Path]:
        """设置里的导出文件夹 + 默认的 根目录/导出（在媒体库里面的才有意义）。"""
        out = [self.root / "导出"]
        d = (self.get_setting("export_dir", "") or "").strip()
        if d:
            out.append(Path(d))
        # 1.3.4：导出只出视频（没有「说明」/ manifest 可认），导出过的文件夹记在库里
        for m in self.get_setting("export_marks", []) or []:
            out.append(Path(m) if Path(m).is_absolute() else self.root / m)
        res = []
        for x in out:
            try:
                x = x.resolve()
            except Exception:
                continue
            if x != self.root.resolve():
                res.append(x)
        return res

    def mark_export_dir(self, d) -> None:
        """记下这个导出文件夹：以后扫描媒体库跳过它（在媒体库里面的存相对路径，换盘符也认）。"""
        try:
            p = Path(d).resolve()
            root = self.root.resolve()
        except Exception:
            return
        if p == root:
            return
        try:
            v = p.relative_to(root).as_posix()
        except ValueError:
            return                      # 不在媒体库里：本来就扫不到
        if any(p == x or x in p.parents for x in self.export_dirs()):
            return                      # 已经在导出文件夹里面了
        marks = [m for m in (self.get_setting("export_marks", []) or []) if m != v]
        self.set_setting("export_marks", marks + [v])

    def is_export_dir(self, d: Path, excl=None) -> bool:
        """导出产生的文件夹：设置的导出文件夹、预设名文件夹（横屏原画无损 等）、带「说明」/manifest.json 的导出目录。"""
        from .export import PRESETS
        if d.name in PRESETS or d.name == "说明":
            return True
        if (d / "manifest.json").is_file() and (d / "说明").is_dir():
            return True
        if (d / "说明").is_dir() and any((d / p).is_dir() for p in PRESETS):
            return True
        try:
            rd = d.resolve()
        except Exception:
            return False
        return any(rd == x for x in (excl if excl is not None else self.export_dirs()))

    def _walk_media(self):
        import os
        excl = self.export_dirs()
        for cur, dirs, files in os.walk(self.root):
            cp = Path(cur)
            keep = []
            for dn in dirs:
                if dn.startswith(".") or dn == DATA_DIR:
                    continue
                if self.is_export_dir(cp / dn, excl):
                    continue
                keep.append(dn)
            dirs[:] = sorted(keep)
            for fn in files:
                if not fn.startswith("."):
                    yield cp / fn

    def purge_excluded(self) -> int:
        """把以前误扫进来的导出文件夹里的「集」去掉（不碰文件）。返回去掉了几集。"""
        excl = self.export_dirs()
        n = 0
        for r in self.db.execute("SELECT id, rel_video, rel_sub FROM media").fetchall():
            rel = r["rel_video"] or r["rel_sub"]
            if not rel:
                continue
            parts = Path(rel).parts[:-1]
            d = self.root
            for part in parts:
                d = d / part
                if self.is_export_dir(d, excl):
                    self._drop_media(r["id"]); n += 1
                    break
        if n:
            self.db.commit()
        return n

    def snapshot(self) -> dict | None:
        return self.get_setting("snapshot", None)

    def discover(self) -> dict[str, dict]:
        """找出所有「一集」。

        剧集：同一个顶层文件夹（=剧名）里，按文件名里的 S01E02 / 1x02 认集，视频和字幕在哪个子目录、
        叫什么名字都行（字幕组发布包、压制版另放一个文件夹也能配上）。一集有多个字幕时按语言挑：
        双语 > 英文 > 不明；纯中文的单独作译文。一集有多个视频时：路径带「压制」优先，再 mp4，再文件小的。
        电影/认不出集数的：同目录同名配对（旧规则）。
        """
        eps: dict[str, dict] = {}
        loose: dict[str, dict] = {}
        for p in self._walk_media():
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
        # 1.3.2：这次扫描的结果就是新快照；以后打开软件直接用它，不再自动扫描
        stats["total"] = self.db.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        self.set_setting("snapshot", dict(t=time.time(), total=stats["total"], new=stats["new"],
                                          removed=stats["removed"]))
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
        """全部采集包（含 folder_id、条数 n），按文件夹内顺序。"""
        return self.db.execute(
            """SELECT p.*, COALESCE(c.n, 0) n FROM packs p
               LEFT JOIN (SELECT pack_id, COUNT(*) n FROM pack_items GROUP BY pack_id) c ON c.pack_id=p.id
               ORDER BY p.sort, p.created, p.id""").fetchall()

    def pack(self, pack_id):
        return self.db.execute("SELECT * FROM packs WHERE id=?", (pack_id,)).fetchone()

    def pack_count(self, pack_id) -> int:
        return self.db.execute("SELECT COUNT(*) FROM pack_items WHERE pack_id=?", (pack_id,)).fetchone()[0]

    @staticmethod
    def _clean_name(name: str) -> str:
        """存进库的名字：去首尾空白、连续空白合一、... 写成 …（/ 是路径分隔符，换成全角）。"""
        s = (name or "").replace("...", "…").replace("/", "／")
        return re.sub(r"\s+", " ", s).strip()

    @staticmethod
    def norm_name(name: str) -> str:
        """1.3.4 包名 / 句型匹配键：去首尾空白、连续空白合一、不分大小写、... ≡ …。"""
        s = (name or "").replace("／", "/").replace("...", "…").replace("。。。", "…")
        s = re.sub(r"\s*…\s*", "…", s)          # 「your…」和「your …」算同一个
        return re.sub(r"\s+", " ", s).strip().lower()

    def packs_named(self, pattern: str) -> list[int]:
        """和句型同名的包（可能在不同文件夹里有好几个）：最近用过的排前面，其次最近收藏过 / 新建的。"""
        k = self.norm_name(pattern)
        if not k:
            return []
        rows = self.db.execute(
            """SELECT p.id, p.name, p.created, (SELECT MAX(added) FROM pack_items i WHERE i.pack_id=p.id) last
               FROM packs p""").fetchall()
        ids = [r for r in rows if self.norm_name(r["name"]) == k]
        rec = self.get_setting("recent_packs", []) or []
        rank = {pid: n for n, pid in enumerate(rec)}
        ids.sort(key=lambda r: (rank.get(r["id"], 1 << 30), -(r["last"] or r["created"] or 0)))
        return [r["id"] for r in ids]

    def set_item_order(self, pack_id: int, item_ids: list[int]):
        """拖动排序后：按给的顺序重写 pos。"""
        self.db.executemany("UPDATE pack_items SET pos=? WHERE id=? AND pack_id=?",
                            [(n + 1, i, pack_id) for n, i in enumerate(item_ids)])
        self.db.commit()

    def _next_sort(self, table: str, col: str, parent) -> int:
        r = self.db.execute(f"SELECT COALESCE(MAX(sort),0)+1 FROM {table} WHERE {col} IS ?", (parent,)).fetchone()
        return r[0]

    def find_pack(self, name: str, folder_id=None):
        """同一文件夹里同名的包（按 norm_name 比：大小写、空白、... ≡ … 都算同名）。"""
        k = self.norm_name(name)
        for r in self.db.execute("SELECT id, name FROM packs WHERE folder_id IS ?", (folder_id,)):
            if self.norm_name(r[1]) == k:
                return r[0]
        return None

    def create_pack(self, name: str, folder_id=None):
        """在文件夹里新建采集包；同一文件夹里已有同名包返回 None。"""
        name = self._clean_name(name)
        if not name or self.find_pack(name, folder_id) is not None:
            return None
        cur = self.db.execute("INSERT INTO packs(name,created,folder_id,sort) VALUES(?,?,?,?)",
                              (name, time.time(), folder_id, self._next_sort("packs", "folder_id", folder_id)))
        self.db.commit()
        return cur.lastrowid

    def pack_id(self, name: str, folder_id=None) -> int:
        """按名字取包（没有就建）；不给文件夹 = 最外层。"""
        name = self._clean_name(name)
        pid = self.find_pack(name, folder_id)
        return pid if pid is not None else self.create_pack(name, folder_id)

    def add_to_pack(self, pack, key: str, idx: int, pattern="", tier="") -> bool:
        """pack：包 id，或包名（最外层，没有就建）。"""
        pid = pack if isinstance(pack, int) else self.pack_id(pack)
        pos = self.db.execute("SELECT COALESCE(MAX(pos),0)+1 FROM pack_items WHERE pack_id=?", (pid,)).fetchone()[0]
        cur = self.db.execute(
            "INSERT OR IGNORE INTO pack_items(pack_id,media_key,idx,pattern,tier,pos,added) VALUES(?,?,?,?,?,?,?)",
            (pid, key, idx, pattern, tier, pos, time.time()))
        self.db.commit()
        return cur.rowcount > 0

    # ---------- 采集包文件夹（1.3.4） ----------
    def folders(self):
        return self.db.execute("SELECT * FROM folders ORDER BY sort, created, id").fetchall()

    def folder(self, fid):
        return self.db.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()

    def find_folder(self, name: str, parent_id=None):
        k = self.norm_name(name)
        for r in self.db.execute("SELECT id, name FROM folders WHERE parent_id IS ?", (parent_id,)):
            if self.norm_name(r[1]) == k:
                return r[0]
        return None

    def create_folder(self, name: str, parent_id=None):
        """新建文件夹；同一层已有同名文件夹返回 None。"""
        name = self._clean_name(name)
        if not name or self.find_folder(name, parent_id) is not None:
            return None
        if parent_id is not None and self.folder(parent_id) is None:
            return None
        cur = self.db.execute("INSERT INTO folders(parent_id,name,sort,created) VALUES(?,?,?,?)",
                              (parent_id, name, self._next_sort("folders", "parent_id", parent_id), time.time()))
        self.db.commit()
        return cur.lastrowid

    def ensure_folder_path(self, path) -> int | None:
        """按路径（"NCE1/L1-L2" 或 ["NCE1","L1-L2"]）取文件夹，没有就逐级建。"""
        parts = [p for p in (path.split("/") if isinstance(path, str) else path) if p.strip()]
        fid = None
        for p in parts:
            nxt = self.find_folder(self._clean_name(p), fid)
            fid = nxt if nxt is not None else self.create_folder(p, fid)
        return fid

    def rename_folder(self, fid: int, name: str) -> bool:
        name = self._clean_name(name)
        f = self.folder(fid)
        if not f or not name:
            return False
        other = self.find_folder(name, f["parent_id"])
        if other is not None and other != fid:
            return False
        self.db.execute("UPDATE folders SET name=? WHERE id=?", (name, fid))
        self.db.commit()
        return True

    def subfolder_ids(self, fid) -> list[int]:
        """fid 自己和它下面所有层的文件夹 id。"""
        out, todo = [], [fid]
        kids: dict = {}
        for r in self.db.execute("SELECT id, parent_id FROM folders"):
            kids.setdefault(r[1], []).append(r[0])
        while todo:
            x = todo.pop()
            out.append(x)
            todo += kids.get(x, [])
        return out

    def folder_stats(self, fid) -> dict:
        """文件夹下（递归）有几个子文件夹、几个包、几条。"""
        ids = self.subfolder_ids(fid)
        q = ",".join("?" * len(ids))
        packs = [r[0] for r in self.db.execute(f"SELECT id FROM packs WHERE folder_id IN ({q})", ids)]
        items = 0
        if packs:
            items = self.db.execute("SELECT COUNT(*) FROM pack_items WHERE pack_id IN (%s)" % ",".join("?" * len(packs)),
                                    packs).fetchone()[0]
        return dict(folders=len(ids) - 1, packs=len(packs), items=items, pack_ids=packs)

    def delete_folder(self, fid: int) -> dict:
        """删除文件夹和下面所有子文件夹、采集包（只删包，视频 / 微调 / 导出的文件都不动）。"""
        st = self.folder_stats(fid)
        ids = self.subfolder_ids(fid)
        for pid in st["pack_ids"]:
            self.db.execute("DELETE FROM pack_items WHERE pack_id=?", (pid,))
            self.db.execute("DELETE FROM packs WHERE id=?", (pid,))
        self.db.executemany("DELETE FROM folders WHERE id=?", [(i,) for i in ids])
        self.db.commit()
        return st

    def _reorder(self, table: str, col: str, parent, row_id: int, index):
        """把 row_id 放到同层第 index 个（None = 最后），重排 sort。"""
        ids = [r[0] for r in self.db.execute(
            f"SELECT id FROM {table} WHERE {col} IS ? AND id!=? ORDER BY sort, created, id", (parent, row_id))]
        if index is None or index > len(ids):
            index = len(ids)
        ids.insert(max(0, index), row_id)
        self.db.executemany(f"UPDATE {table} SET sort=? WHERE id=?", [(n + 1, i) for n, i in enumerate(ids)])

    def move_pack(self, pack_id: int, folder_id=None, index=None) -> bool:
        """把包挪到文件夹（None = 最外层）的第 index 个位置；目标文件夹里有同名包就不挪，返回 False。"""
        p = self.pack(pack_id)
        if not p or (folder_id is not None and self.folder(folder_id) is None):
            return False
        other = self.find_pack(p["name"], folder_id)
        if other is not None and other != pack_id:
            return False
        self.db.execute("UPDATE packs SET folder_id=? WHERE id=?", (folder_id, pack_id))
        self._reorder("packs", "folder_id", folder_id, pack_id, index)
        self.db.commit()
        return True

    def move_folder(self, fid: int, parent_id=None, index=None) -> bool:
        """挪文件夹；不能挪到自己或自己的子孙下面，目标层有同名文件夹也不挪。"""
        f = self.folder(fid)
        if not f:
            return False
        if parent_id is not None and (parent_id in self.subfolder_ids(fid) or self.folder(parent_id) is None):
            return False
        other = self.find_folder(f["name"], parent_id)
        if other is not None and other != fid:
            return False
        self.db.execute("UPDATE folders SET parent_id=? WHERE id=?", (parent_id, fid))
        self._reorder("folders", "parent_id", parent_id, fid, index)
        self.db.commit()
        return True

    def folder_path(self, fid) -> list[str]:
        """文件夹的路径（从最外层起的名字列表）。"""
        names, seen = [], set()
        while fid is not None and fid not in seen:
            seen.add(fid)
            f = self.folder(fid)
            if not f:
                break
            names.append(f["name"])
            fid = f["parent_id"]
        return names[::-1]

    def pack_path(self, pack_id) -> list[str]:
        p = self.pack(pack_id)
        if not p:
            return []
        return self.folder_path(p["folder_id"]) + [p["name"]]

    def pack_by_path(self, path):
        """按路径取包："NCE1/L1-L2/is this your…" 或列表；最后一段是包名。找不到返回 None。"""
        parts = [p for p in (path.split("/") if isinstance(path, str) else path) if p.strip()]
        if not parts:
            return None
        fid = None
        for p in parts[:-1]:
            fid = self.find_folder(self._clean_name(p), fid)
            if fid is None:
                return None
        return self.find_pack(self._clean_name(parts[-1]), fid)

    def packs_under(self, fid) -> list[tuple[int, list[str]]]:
        """文件夹下（递归）所有包：[(包 id, 相对这个文件夹的路径（含包名）)]，按树的顺序。"""
        folders = self.folders()
        kids: dict = {}
        for f in folders:
            kids.setdefault(f["parent_id"], []).append(f)
        packs: dict = {}
        for p in self.packs():
            packs.setdefault(p["folder_id"], []).append(p)
        out = []

        def walk(f_id, rel):
            for f in kids.get(f_id, []):
                walk(f["id"], rel + [f["name"]])
            for p in packs.get(f_id, []):
                out.append((p["id"], rel + [p["name"]]))
        walk(fid, [])
        return out

    def recent_packs(self) -> list[int]:
        ids = self.get_setting("recent_packs", []) or []
        have = {r[0] for r in self.db.execute("SELECT id FROM packs")}
        return [i for i in ids if i in have][:5]

    def touch_recent_pack(self, pack_id: int):
        ids = [i for i in (self.get_setting("recent_packs", []) or []) if i != pack_id]
        self.set_setting("recent_packs", [pack_id] + ids[:9])

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

    def rename_pack(self, pack_id: int, name: str) -> bool:
        """改包名；同一文件夹里新名字已被别的包用了就返回 False。"""
        name = self._clean_name(name)
        p = self.pack(pack_id)
        if not name or not p:
            return False
        other = self.find_pack(name, p["folder_id"])
        if other is not None and other != pack_id:
            return False
        self.db.execute("UPDATE packs SET name=? WHERE id=?", (name, pack_id))
        self.db.commit()
        return True

    def delete_pack(self, pack_id: int):
        """只删包和包里的条目引用；视频、字幕、微调、已导出的文件都不动。"""
        self.db.execute("DELETE FROM pack_items WHERE pack_id=?", (pack_id,))
        self.db.execute("DELETE FROM packs WHERE id=?", (pack_id,))
        self.db.commit()

    def pack_membership(self, media_keys=None) -> dict:
        """1.3.5：{(media_key, idx): {pack_id, …}}，一次查询（结果行星标用，不逐行查库）。
        media_keys 给了且不多时只查这些集，否则整张表一次读出来。"""
        keys = sorted(set(media_keys)) if media_keys is not None else None
        if keys is not None and not keys:
            return {}
        if keys is not None and len(keys) <= 500:
            rows = self.db.execute("SELECT pack_id, media_key, idx FROM pack_items WHERE media_key IN (%s)"
                                   % ",".join("?" * len(keys)), keys).fetchall()
        else:
            rows = self.db.execute("SELECT pack_id, media_key, idx FROM pack_items").fetchall()
        out = {}
        for pid, key, idx in rows:
            out.setdefault((key, idx), set()).add(pid)
        return out

    def remove_from_pack(self, pack_id: int, key: str, idx: int) -> bool:
        """只从这一个包里拿掉这一条（别的包不动）。"""
        cur = self.db.execute("DELETE FROM pack_items WHERE pack_id=? AND media_key=? AND idx=?", (pack_id, key, idx))
        self.db.commit()
        return cur.rowcount > 0

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
