"""后台线程：扫描、对齐队列、导出、波形。每个线程用自己的数据库连接。"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from ..library import Library
from .. import align, jobs, wave
from ..export import Style


class ScanWorker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, root):
        super().__init__()
        self.root = root

    def run(self):
        try:
            lib = Library(self.root)
            st = lib.scan(progress=lambda n, t, k: self.progress.emit(n, t, k))
            lib.db.close()
            self.finished_ok.emit(st)
        except Exception as ex:
            self.failed.emit(f"{ex}\n{traceback.format_exc()[-800:]}")


class AlignWorker(QThread):
    """一直在后台：有没对齐的集就一集一集跑。'queued' 的（对齐中心里点了「先跑这些」）优先。"""
    status = Signal(str, float, str, int, int)   # 当前集, 进度, 步骤文字, 已完成, 待处理
    episode_done = Signal(str, str)              # key, 摘要/错误
    idle = Signal()
    crashed = Signal(str)

    def __init__(self, root, settings: dict):
        super().__init__()
        self.root = root
        self.settings = settings
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._kick = threading.Event()
        self.current = ""

    def pause(self, on: bool):
        if on:
            self._pause.set()
        else:
            self._pause.clear()
            self._kick.set()

    @property
    def paused(self):
        return self._pause.is_set()

    def kick(self):
        self._kick.set()

    def stop(self):
        self._stop.set()
        self._kick.set()

    def run(self):
        try:
            self._run()
        except Exception as ex:  # noqa  线程不能悄悄死掉
            msg = f"{ex!r}\n{traceback.format_exc()[-1500:]}"
            try:
                align.log("对齐线程出错：" + msg)
            except Exception:
                pass
            self.crashed.emit(msg)

    def _run(self):
        lib = Library(self.root)
        align.LOG_FILE = lib.data / "align.log"
        lib.db.execute("UPDATE media SET align_status='none' WHERE align_status='running'")
        lib.db.commit()
        align.log(f"对齐线程启动，媒体库 {self.root}，设置 {self.settings}")
        while not self._stop.is_set():
            if self._pause.is_set():
                self._kick.wait(2)
                self._kick.clear()
                continue
            row = lib.db.execute(
                "SELECT * FROM media WHERE align_status IN ('none','queued') AND rel_video IS NOT NULL "
                "AND rel_sub IS NOT NULL ORDER BY (align_status='queued') DESC, show, season, episode LIMIT 1"
            ).fetchone()
            if not row:
                self.current = ""
                self.idle.emit()
                self._kick.wait(30)
                self._kick.clear()
                continue
            done = lib.db.execute("SELECT COUNT(*) FROM media WHERE align_status='done'").fetchone()[0]
            todo = lib.db.execute("SELECT COUNT(*) FROM media WHERE align_status IN ('none','queued') "
                                  "AND rel_video IS NOT NULL AND rel_sub IS NOT NULL").fetchone()[0]
            key = self.current = row["key"]
            lib.db.execute("UPDATE media SET align_status='running', align_msg=NULL WHERE id=?", (row["id"],))
            lib.db.commit()
            self.status.emit(key, 0.0, "开始…", done, todo)
            last = [0.0]

            def prog(f, text="", key=key, done=done, todo=todo):
                now = time.time()
                if now - last[0] >= 0.3 or f >= 1.0:
                    last[0] = now
                    self.status.emit(key, float(f), text, done, todo)
            try:
                d = align.align_media(
                    lib, row, model=self.settings.get("align_model", "small"),
                    engine=self.settings.get("align_engine") or None,
                    mirror=self.settings.get("hf_mirror", True), progress=prog)
                self.episode_done.emit(key, d["summary"])
            except Exception as ex:
                align.log(f"失败 {key}：{ex!r}\n{traceback.format_exc()[-1500:]}")
                lib.db.execute("UPDATE media SET align_status='error', align_msg=? WHERE id=?",
                               (str(ex)[:500], row["id"]))
                lib.db.commit()
                self.episode_done.emit(key, "失败：" + str(ex)[:300])
                time.sleep(1)
        self.current = ""


class ExportWorker(QThread):
    progress = Signal(int, int, str)
    finished_ok = Signal(dict)

    def __init__(self, root, items_spec, out_dir, presets, style: Style, make_comp=True, pack_id=None):
        super().__init__()
        self.root, self.items_spec, self.out_dir = root, items_spec, Path(out_dir)
        self.presets, self.style, self.make_comp, self.pack_id = presets, style, make_comp, pack_id
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            lib = Library(self.root)
            if self.pack_id is not None:
                items = jobs.pack_to_items(lib, self.pack_id)
            else:
                items = []
                for spec in self.items_spec:
                    row = lib.db.execute(
                        "SELECT c.*, m.key media_key, m.show, m.season, m.episode, m.kind, m.rel_video "
                        "FROM cues c JOIN media m ON m.id=c.media_id WHERE m.key=? AND c.idx=?",
                        (spec["media_key"], spec["idx"])).fetchone()
                    if row:
                        items.append(dict(row=row, pattern=spec.get("pattern", ""), tier=spec.get("tier", ""),
                                          matched=spec.get("matched", "")))
            r = jobs.export_items(lib, items, self.out_dir, self.presets, self.style,
                                  progress=lambda n, t, s: self.progress.emit(n, t, s),
                                  make_compilation=self.make_comp, cancel=lambda: self._cancel)
            self.finished_ok.emit(r)
        except Exception as ex:
            self.finished_ok.emit(dict(ok=False, errors=[str(ex)], out=str(self.out_dir)))


class WaveWorker(QThread):
    done = Signal(int, object)

    def __init__(self, token, video, t0, t1):
        super().__init__()
        self.token, self.video, self.t0, self.t1 = token, video, t0, t1

    def run(self):
        try:
            p = wave.peaks(self.video, self.t0, self.t1)
        except Exception:
            p = None
        self.done.emit(self.token, p)


class StillWorker(QThread):
    """样式预览：用当前片段导出一帧竖屏烧字幕画面。"""
    done = Signal(str)

    def __init__(self, video, t, out_png, style, en, zh, matched, title, source):
        super().__init__()
        self.args = (video, t, out_png, style, en, zh, matched, title, source)

    def run(self):
        from .. import export, tools
        import shutil, tempfile
        video, t, out_png, style, en, zh, matched, title, source = self.args
        tmp = Path(tempfile.mkdtemp())
        try:
            mp4 = tmp / "a.mp4"
            export.export_clip(Path(video), t, t + 0.5, mp4, "竖屏烧字幕", style, en, zh, matched, title, source)
            r = tools.run([tools.ffmpeg(), "-v", "error", "-y", "-i", str(mp4), "-frames:v", "1", str(out_png)])
            self.done.emit(str(out_png) if r.returncode == 0 else "")
        except Exception:
            self.done.emit("")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
