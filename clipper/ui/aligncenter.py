"""对齐中心：专门跑对齐的窗口。看得见每集状态、当前步骤（读音频 / 下载模型 / 识别 xx% / 匹配字幕）、
引擎（GPU 还是 CPU）、失败原因；能开始、暂停、先跑选中、重试失败、重新对齐、打开日志。"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QColor
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, QCheckBox,
                               QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, QPlainTextEdit,
                               QAbstractItemView, QSplitter, QWidget, QMessageBox)

from .. import align

MODELS = ["small", "base", "tiny", "medium", "large-v3-turbo"]
STATUS_TEXT = {"none": "未对齐", "queued": "优先排队", "running": "对齐中", "done": "完成", "error": "失败"}
STATUS_COLOR = {"done": "#059669", "error": "#DC2626", "running": "#2563EB", "queued": "#7C3AED"}


class EngineProbe(QThread):
    done = Signal(str)

    def __init__(self, engine_pref: str):
        super().__init__()
        self.pref = engine_pref

    def run(self):
        try:
            if self.pref == "faster":
                self.done.emit("faster-whisper · CPU（设置里手动选的）")
                return
            eng = align.default_engine()
            if eng == "mlx":
                self.done.emit("mlx-whisper · " + align.mlx_device())
            else:
                why = f"（mlx 不可用：{align._MLX_BROKEN}）" if align._MLX_BROKEN else ""
                self.done.emit("faster-whisper · CPU" + why)
        except Exception as ex:  # noqa
            self.done.emit(f"检测失败：{ex!r}")


class AlignCenter(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("对齐中心")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self.resize(1100, 760)
        self._worker = None
        self._t_start = None
        self._last_stage = ""
        v = QVBoxLayout(self)

        # 引擎与设置
        top = QHBoxLayout()
        self.engine_lb = QLabel("引擎：检测中…"); self.engine_lb.setStyleSheet("font-weight:600;")
        top.addWidget(self.engine_lb, 1)
        top.addWidget(QLabel("模型"))
        self.model = QComboBox(); self.model.addItems(MODELS)
        top.addWidget(self.model)
        top.addWidget(QLabel("引擎"))
        self.engine = QComboBox()
        self.engine.addItem("自动（Apple 芯片用 GPU）", ""); self.engine.addItem("faster-whisper（CPU）", "faster")
        self.engine.addItem("mlx-whisper（GPU）", "mlx")
        top.addWidget(self.engine)
        self.mirror = QCheckBox("模型走国内镜像"); top.addWidget(self.mirror)
        v.addLayout(top)

        # 当前进度
        box = QWidget(); bl = QVBoxLayout(box); bl.setContentsMargins(0, 6, 0, 6)
        self.cur_lb = QLabel("空闲"); self.cur_lb.setWordWrap(True); self.cur_lb.setStyleSheet("font-size:14px;")
        self.step_lb = QLabel(""); self.step_lb.setWordWrap(True); self.step_lb.setStyleSheet("color:#374151;")
        self.bar = QProgressBar(); self.bar.setRange(0, 1000); self.bar.setFormat("%p%")
        self.count_lb = QLabel(""); self.count_lb.setStyleSheet("color:#6B7280;")
        for w in (self.cur_lb, self.step_lb, self.bar, self.count_lb):
            bl.addWidget(w)
        v.addWidget(box)

        # 按钮
        bh = QHBoxLayout()
        self.b_start = QPushButton("▶ 开始对齐"); self.b_start.setObjectName("primary")
        self.b_pause = QPushButton("暂停")
        self.b_first = QPushButton("先跑选中的")
        self.b_redo = QPushButton("重新对齐选中的")
        self.b_retry = QPushButton("重试全部失败")
        self.b_log = QPushButton("打开日志文件")
        for b in (self.b_start, self.b_pause, self.b_first, self.b_redo, self.b_retry):
            bh.addWidget(b)
        bh.addStretch(1); bh.addWidget(self.b_log)
        v.addLayout(bh)
        self.b_start.clicked.connect(self.start)
        self.b_pause.clicked.connect(self.toggle_pause)
        self.b_first.clicked.connect(lambda: self.queue_selected(redo=False))
        self.b_redo.clicked.connect(lambda: self.queue_selected(redo=True))
        self.b_retry.clicked.connect(self.retry_failed)
        self.b_log.clicked.connect(self.open_log)

        # 筛选
        fh = QHBoxLayout()
        fh.addWidget(QLabel("作品"))
        self.f_show = QComboBox(); self.f_show.setMinimumWidth(260); fh.addWidget(self.f_show)
        fh.addWidget(QLabel("状态"))
        self.f_status = QComboBox()
        for t, d in [("全部", ""), ("未对齐", "none"), ("完成", "done"), ("失败", "error"), ("对齐中/排队", "busy")]:
            self.f_status.addItem(t, d)
        fh.addWidget(self.f_status); fh.addStretch(1)
        v.addLayout(fh)
        self.f_show.currentIndexChanged.connect(self.reload)
        self.f_status.currentIndexChanged.connect(self.reload)

        split = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["作品", "集", "状态", "结果 / 原因"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive); hh.resizeSection(0, 260)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        split.addWidget(self.table)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(3000)
        self.log.setStyleSheet("font-family:Menlo,Consolas,monospace;font-size:11px;background:#fff;")
        split.addWidget(self.log)
        split.setSizes([430, 180])
        v.addWidget(split, 1)

        self.tick = QTimer(self, interval=1000, timeout=self._tick)
        self.load_settings()
        self.model.currentIndexChanged.connect(self.save_settings)
        self.engine.currentIndexChanged.connect(self.save_settings)
        self.mirror.toggled.connect(self.save_settings)
        self.fill_shows()
        self.reload()
        self.probe()
        self.attach(main.align_worker)

    # ---------- 设置 ----------
    def load_settings(self):
        s = self.main.lib_settings()
        i = self.model.findText(s["align_model"]); self.model.setCurrentIndex(max(0, i))
        i = self.engine.findData(s["align_engine"] or ""); self.engine.setCurrentIndex(max(0, i))
        self.mirror.setChecked(bool(s["hf_mirror"]))

    def save_settings(self):
        lib = self.main.lib
        lib.set_setting("align_model", self.model.currentText())
        lib.set_setting("align_engine", self.engine.currentData())
        lib.set_setting("hf_mirror", self.mirror.isChecked())
        if self.main.align_worker:
            self.main.align_worker.settings = self.main.lib_settings()
        self.probe()

    def probe(self):
        self.engine_lb.setText("引擎：检测中…")
        self._probe = EngineProbe(self.engine.currentData())
        self._probe.done.connect(lambda t: self.engine_lb.setText("引擎：" + t))
        self._probe.start()

    # ---------- 列表 ----------
    def fill_shows(self):
        cur = self.f_show.currentData()
        self.f_show.blockSignals(True)
        self.f_show.clear(); self.f_show.addItem("全部作品", "")
        for r in self.main.lib.shows():
            self.f_show.addItem(r["show"], r["show"])
        i = self.f_show.findData(cur); self.f_show.setCurrentIndex(max(0, i))
        self.f_show.blockSignals(False)

    def reload(self):
        db = self.main.lib.db
        where, args = ["rel_video IS NOT NULL", "rel_sub IS NOT NULL"], []
        if self.f_show.currentData():
            where.append("show=?"); args.append(self.f_show.currentData())
        st = self.f_status.currentData()
        if st == "busy":
            where.append("align_status IN ('running','queued')")
        elif st:
            where.append("align_status=?"); args.append(st)
        rows = db.execute(f"SELECT key,show,season,episode,kind,title,align_status,align_msg FROM media "
                          f"WHERE {' AND '.join(where)} ORDER BY show,season,episode,key", args).fetchall()
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            ep = f"S{r['season']:02d}E{r['episode']:02d}" if r["kind"] == "tv" else (r["title"] or "")
            cells = [r["show"], ep, STATUS_TEXT.get(r["align_status"], r["align_status"]), r["align_msg"] or ""]
            for j, c in enumerate(cells):
                it = QTableWidgetItem(str(c))
                if j == 0:
                    it.setData(Qt.UserRole, r["key"])
                if j == 2 and r["align_status"] in STATUS_COLOR:
                    it.setForeground(QColor(STATUS_COLOR[r["align_status"]]))
                if j == 3:
                    it.setToolTip(str(c))
                self.table.setItem(i, j, it)
        self.table.setUpdatesEnabled(True)
        self.update_counts()

    def update_counts(self):
        db = self.main.lib.db
        q = lambda w: db.execute(f"SELECT COUNT(*) FROM media WHERE {w}").fetchone()[0]
        ok = "rel_video IS NOT NULL AND rel_sub IS NOT NULL"
        n_done = q(ok + " AND align_status='done'")
        n_todo = q(ok + " AND align_status IN ('none','queued','running')")
        n_err = q(ok + " AND align_status='error'")
        n_miss = q("NOT (" + ok + ")")
        self.count_lb.setText(f"完成 {n_done} 集 · 待对齐 {n_todo} 集 · 失败 {n_err} 集 · 缺视频或字幕（不能对齐）{n_miss} 集")

    def selected_keys(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).data(Qt.UserRole) for r in rows]

    # ---------- 操作 ----------
    def start(self):
        w = self.main.align_worker
        if w and w.isRunning():
            if w.paused:
                w.pause(False)
            w.settings = self.main.lib_settings()
            w.kick()
        else:
            self.main.start_align(force=True)
        self.attach(self.main.align_worker)
        self.append("开始对齐")
        self.b_pause.setText("暂停")

    def toggle_pause(self):
        w = self.main.align_worker
        if not w:
            return
        on = not w.paused
        w.pause(on)
        self.b_pause.setText("继续" if on else "暂停")
        self.append("已暂停：当前这集跑完后停" if on else "继续")
        self.main.al_btn.setText("继续" if on else "暂停")

    def queue_selected(self, redo: bool):
        keys = self.selected_keys()
        if not keys:
            QMessageBox.information(self, "对齐中心", "先在列表里选中要跑的集（可以多选）。")
            return
        db = self.main.lib.db
        if redo:
            db.executemany("UPDATE media SET align_status='queued', align_msg=NULL WHERE key=? AND align_status!='running'",
                           [(k,) for k in keys])
        else:
            db.executemany("UPDATE media SET align_status='queued' WHERE key=? AND align_status IN ('none','error')",
                           [(k,) for k in keys])
        db.commit()
        self.append(("重新对齐 " if redo else "优先排队 ") + f"{len(keys)} 集")
        self.reload()
        self.start()

    def retry_failed(self):
        db = self.main.lib.db
        n = db.execute("UPDATE media SET align_status='none', align_msg=NULL WHERE align_status='error'").rowcount
        db.commit()
        self.append(f"重试 {n} 集失败的")
        self.reload()
        self.start()

    def open_log(self):
        f = self.main.lib.data / "align.log"
        if not f.exists():
            f.write_text("", "utf-8")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(f)))

    # ---------- 信号 ----------
    def attach(self, worker):
        if worker is None or worker is self._worker:
            return
        self._worker = worker
        worker.status.connect(self.on_status)
        worker.episode_done.connect(self.on_done)
        worker.idle.connect(self.on_idle)
        worker.crashed.connect(self.on_crash)

    def append(self, text):
        self.log.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")

    def on_status(self, key, f, text, done, todo):
        if self._t_start is None or self.cur_lb.property("key") != key:
            self._t_start = time.time()
            self.cur_lb.setProperty("key", key)
            self.append(f"▶ {key}")
            self.tick.start()
        self.cur_lb.setText(f"正在对齐：{key}")
        self.step_lb.setText(text)
        self.bar.setValue(int(f * 1000))
        stage = text.split(" ")[0].split("：")[0].split("（")[0]
        if stage != self._last_stage:
            self.append("   " + text)
        self._last_stage = stage

    def _tick(self):
        if self._t_start is not None:
            el = time.time() - self._t_start
            base = self.cur_lb.text().split("　")[0]
            self.cur_lb.setText(f"{base}　已用 {int(el // 60)}:{int(el % 60):02d}")

    def on_done(self, key, msg):
        self.append(f"■ {key}  {msg}")
        self._t_start = None
        self._last_stage = ""
        self.reload()

    def on_idle(self):
        self.tick.stop()
        self._t_start = None
        self.cur_lb.setText("空闲：没有要对齐的集了")
        self.step_lb.setText("")
        self.bar.setValue(0)
        self.update_counts()

    def on_crash(self, msg):
        self.tick.stop()
        self.cur_lb.setText("对齐线程出错了（详情见下方日志，也写进了日志文件）")
        self.append("对齐线程出错：" + msg)
