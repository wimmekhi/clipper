"""主窗口：33 台词式「搜 → 播 → 采集 → 导出」，所有面板都是可拖动、可调大小、可浮出的停靠窗。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QSettings, QTimer, QUrl, QSize
from PySide6.QtGui import QAction, QKeySequence, QShortcut, QFont
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QMainWindow, QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
                               QLineEdit, QPushButton, QCheckBox, QComboBox, QLabel, QListWidget,
                               QListWidgetItem, QTabBar, QFileDialog, QMessageBox, QProgressBar, QSpinBox,
                               QInputDialog, QTreeWidget, QTreeWidgetItem, QSizePolicy, QFrame, QDoubleSpinBox,
                               QAbstractItemView, QToolBar, QStackedLayout)

from .. import tools
from ..paths import VERSION, icon_path
from ..library import Library
from ..search import Searcher, TIER_ORIG, TIER_NEAR, TIER_FUNC
from ..export import Style
from .widgets import ResultCard, WaveformBar, fmt_ms, highlight_html, TIMING_TEXT
from .workers import ScanWorker, AlignWorker, ExportWorker, WaveWorker
from .dialogs import SettingsDialog, ExportDialog

APP = "台词切片编辑器"
PAGE = 60

QSS = """
QMainWindow, QWidget { font-size: 13px; }
QMainWindow { background: #F3F4F6; }
QDockWidget::title { background: #F9FAFB; padding: 5px 8px; border-bottom: 1px solid #E5E7EB; font-weight: 600; }
QLineEdit { border: 1px solid #D1D5DB; border-radius: 6px; padding: 6px 8px; background: #fff; }
QLineEdit:focus { border-color: #2563EB; }
QPushButton#primary { background: #2563EB; color: #fff; border: none; border-radius: 6px; padding: 6px 16px; }
QPushButton#primary:hover { background: #1D4ED8; }
QListWidget { border: none; background: transparent; }
QTreeWidget { border: none; }
QTabBar::tab { padding: 5px 12px; border: none; color: #6B7280; }
QTabBar::tab:selected { color: #2563EB; border-bottom: 2px solid #2563EB; }
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP} {VERSION}")
        self.resize(1440, 900)
        self.qs = QSettings("local33", "clipper")
        tools.set_paths(self.qs.value("ffmpeg", "") or None)
        self.lib: Library | None = None
        self.searcher: Searcher | None = None
        self.hits = []
        self.shown = 0
        self.cur = None            # 当前预览的 hit
        self.cur_span = None
        self.align_worker = None
        self.export_workers = []
        self._wave_token = 0
        self._wave_workers = []
        self.setDockNestingEnabled(True)
        self.setDockOptions(QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks | QMainWindow.AnimatedDocks)
        self._build_search()
        self._build_docks()
        self._build_menu()
        self.setStyleSheet(QSS)
        st = self.qs.value("state")
        if st is not None:
            self.restoreState(st)
        geo = self.qs.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self.rescan_timer = QTimer(self, interval=5 * 60 * 1000, timeout=self.rescan)
        root = self.qs.value("root", "")
        if root and Path(root).exists():
            QTimer.singleShot(50, lambda: self.open_library(root))
        else:
            QTimer.singleShot(50, self._first_run)

    # ------------------------------------------------------------ 构建界面
    def _build_search(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(14, 12, 14, 8)
        row = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("搜索台词或句型，例如  what are you doing　/　whose * is this　/　is this your ...")
        self.q.returnPressed.connect(self.do_search)
        f = self.q.font(); f.setPointSize(f.pointSize() + 2); self.q.setFont(f)
        row.addWidget(self.q, 1)
        b = QPushButton("搜索"); b.setObjectName("primary"); b.clicked.connect(self.do_search)
        row.addWidget(b)
        v.addLayout(row)
        opts = QHBoxLayout()
        self.c_exact = QCheckBox("精确"); self.c_exact.setToolTip("不做词形变化（loved 不算 love）")
        self.c_order = QCheckBox("严格顺序"); self.c_order.setToolTip("词必须按顺序连在一起；* 或 ... 表示中间可以有几个词")
        self.c_single = QCheckBox("只看单人"); self.c_single.setChecked(True)
        self.c_video = QCheckBox("只看有视频")
        for c in (self.c_exact, self.c_order, self.c_single, self.c_video):
            c.toggled.connect(lambda _=False: self.do_search() if self.q.text().strip() else None)
            opts.addWidget(c)
        opts.addSpacing(16)
        opts.addWidget(QLabel("作品"))
        self.f_show = QComboBox(); self.f_show.setMinimumWidth(180)
        self.f_show.currentIndexChanged.connect(lambda _: self.do_search() if self.q.text().strip() else None)
        opts.addWidget(self.f_show)
        opts.addWidget(QLabel("季"))
        self.f_season = QSpinBox(); self.f_season.setRange(0, 99); self.f_season.setSpecialValueText("全部")
        self.f_season.valueChanged.connect(lambda _: self.do_search() if self.q.text().strip() else None)
        opts.addWidget(self.f_season)
        opts.addWidget(QLabel("最长"))
        self.f_maxdur = QDoubleSpinBox(); self.f_maxdur.setRange(0, 60); self.f_maxdur.setValue(8)
        self.f_maxdur.setSuffix(" 秒"); self.f_maxdur.setSpecialValueText("不限")
        opts.addWidget(self.f_maxdur)
        opts.addStretch(1)
        v.addLayout(opts)
        self.tabs = QTabBar()
        for t in ("全部", TIER_ORIG, TIER_NEAR, TIER_FUNC):
            self.tabs.addTab(t)
        self.tabs.setDrawBase(False)
        self.tabs.currentChanged.connect(lambda _: self.render_results())
        th = QHBoxLayout(); th.addWidget(self.tabs); th.addStretch(1)
        self.count_lb = QLabel(""); self.count_lb.setStyleSheet("color:#6B7280;")
        th.addWidget(self.count_lb)
        v.addLayout(th)
        self.results = QListWidget()
        self.results.setSpacing(4)
        self.results.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.results.verticalScrollBar().valueChanged.connect(self._maybe_more)
        v.addWidget(self.results, 1)
        self.setCentralWidget(w)

    def _dock(self, title, widget, area, name):
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setWidget(widget)
        d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(area, d)
        return d

    def _build_docks(self):
        # 媒体库
        lw = QWidget(); lv = QVBoxLayout(lw); lv.setContentsMargins(8, 6, 8, 6)
        self.root_lb = QLabel("未选择媒体库"); self.root_lb.setWordWrap(True)
        self.root_lb.setStyleSheet("color:#6B7280;font-size:11px;")
        lv.addWidget(self.root_lb)
        hb = QHBoxLayout()
        b1 = QPushButton("选择文件夹"); b1.clicked.connect(self.choose_root)
        b2 = QPushButton("重新扫描"); b2.clicked.connect(self.rescan)
        hb.addWidget(b1); hb.addWidget(b2); lv.addLayout(hb)
        self.tree = QTreeWidget(); self.tree.setHeaderLabels(["作品", "集数", "已对齐"])
        self.tree.setColumnWidth(0, 130); self.tree.setColumnWidth(1, 44)
        self.tree.itemDoubleClicked.connect(self._tree_pick)
        lv.addWidget(self.tree, 1)
        self.d_lib = self._dock("媒体库", lw, Qt.LeftDockWidgetArea, "dock_lib")
        # 后台对齐
        aw = QWidget(); av = QVBoxLayout(aw); av.setContentsMargins(8, 6, 8, 6)
        self.al_lb = QLabel("后台对齐：未开始"); self.al_lb.setWordWrap(True)
        self.al_bar = QProgressBar(); self.al_bar.setRange(0, 1000); self.al_bar.setTextVisible(False)
        self.al_bar.setFixedHeight(6)
        self.al_btn = QPushButton("暂停"); self.al_btn.clicked.connect(self._toggle_align)
        self.al_log = QListWidget(); self.al_log.setMaximumHeight(120)
        self.al_log.setStyleSheet("font-size:11px;color:#6B7280;")
        av.addWidget(self.al_lb); av.addWidget(self.al_bar)
        ah = QHBoxLayout(); ah.addStretch(1); ah.addWidget(self.al_btn); av.addLayout(ah)
        av.addWidget(self.al_log)
        self.d_align = self._dock("后台对齐", aw, Qt.LeftDockWidgetArea, "dock_align")
        # 预览
        pw = QWidget(); pv = QVBoxLayout(pw); pv.setContentsMargins(6, 6, 6, 6)
        self.video = QVideoWidget(); self.video.setMinimumSize(320, 180)
        self.video.setStyleSheet("background:#000;")
        self.player = QMediaPlayer(self); self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio); self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._on_pos)
        self.player.mediaStatusChanged.connect(self._on_status)
        pv.addWidget(self.video, 1)
        self.sub_en = QLabel(""); self.sub_en.setAlignment(Qt.AlignCenter); self.sub_en.setWordWrap(True)
        self.sub_en.setStyleSheet("font-size:15px;color:#111827;")
        self.sub_zh = QLabel(""); self.sub_zh.setAlignment(Qt.AlignCenter); self.sub_zh.setWordWrap(True)
        self.sub_zh.setStyleSheet("font-size:13px;color:#6B7280;")
        pv.addWidget(self.sub_en); pv.addWidget(self.sub_zh)
        ctl = QHBoxLayout()
        self.b_play = QPushButton("播放本句"); self.b_play.clicked.connect(self.play_clip)
        b_prev = QPushButton("上一条"); b_prev.clicked.connect(lambda: self.step(-1))
        b_next = QPushButton("下一条"); b_next.clicked.connect(lambda: self.step(1))
        self.speed = QComboBox()
        for s in ("0.5", "0.75", "1.0", "1.25"):
            self.speed.addItem(s + "×", float(s))
        self.speed.setCurrentIndex(2)
        self.speed.currentIndexChanged.connect(lambda _: self.player.setPlaybackRate(self.speed.currentData()))
        self.c_loop = QCheckBox("连播")
        for x in (b_prev, self.b_play, b_next):
            ctl.addWidget(x)
        ctl.addStretch(1); ctl.addWidget(QLabel("倍速")); ctl.addWidget(self.speed); ctl.addWidget(self.c_loop)
        pv.addLayout(ctl)
        self.d_prev = self._dock("预览", pw, Qt.RightDockWidgetArea, "dock_preview")
        # 切点
        cw = QWidget(); cv = QVBoxLayout(cw); cv.setContentsMargins(8, 6, 8, 6)
        self.wave = WaveformBar()
        self.wave.changed.connect(self._cut_changed)
        self.wave.seek.connect(lambda t: self.player.setPosition(int(t * 1000)))
        cv.addWidget(self.wave, 1)
        self.cut_lb = QLabel(""); self.cut_lb.setStyleSheet("font-size:11px;color:#374151;")
        self.cut_lb.setWordWrap(True)
        cv.addWidget(self.cut_lb)
        ch = QHBoxLayout()
        bt = QPushButton("试听切段"); bt.clicked.connect(self.play_clip)
        bs = QPushButton("保存微调"); bs.clicked.connect(self.save_cut)
        br = QPushButton("恢复自动"); br.clicked.connect(self.reset_cut)
        ch.addWidget(bt); ch.addWidget(bs); ch.addWidget(br); ch.addStretch(1)
        ch.addWidget(QLabel("拖两端调整，吸附词边界；按住 Shift 自由拖"))
        cv.addLayout(ch)
        self.d_cut = self._dock("切点", cw, Qt.RightDockWidgetArea, "dock_cut")
        # 上下文
        self.ctx = QListWidget(); self.ctx.setWordWrap(True)
        self.ctx.itemDoubleClicked.connect(self._ctx_pick)
        self.d_ctx = self._dock("上下文", self.ctx, Qt.RightDockWidgetArea, "dock_ctx")
        # 采集包
        kw = QWidget(); kv = QVBoxLayout(kw); kv.setContentsMargins(8, 6, 8, 6)
        kh = QHBoxLayout()
        self.pack_cb = QComboBox(); self.pack_cb.currentIndexChanged.connect(lambda _: self.load_pack())
        bn = QPushButton("新建"); bn.clicked.connect(self.new_pack)
        kh.addWidget(self.pack_cb, 1); kh.addWidget(bn)
        kv.addLayout(kh)
        self.pack_list = QListWidget(); self.pack_list.setWordWrap(True)
        self.pack_list.itemDoubleClicked.connect(self._pack_pick)
        kv.addWidget(self.pack_list, 1)
        kb = QHBoxLayout()
        for t, fn in (("上移", lambda: self._pack_move(-1)), ("下移", lambda: self._pack_move(1)),
                      ("移除", self._pack_remove)):
            b = QPushButton(t); b.clicked.connect(fn); kb.addWidget(b)
        kb.addStretch(1)
        be = QPushButton("导出此包"); be.setObjectName("primary"); be.clicked.connect(self.export_pack)
        kb.addWidget(be)
        kv.addLayout(kb)
        self.d_pack = self._dock("采集包", kw, Qt.RightDockWidgetArea, "dock_pack")
        # 导出任务
        self.tasks = QListWidget(); self.tasks.setWordWrap(True)
        self.tasks.itemDoubleClicked.connect(self._open_task)
        self.d_tasks = self._dock("导出任务", self.tasks, Qt.BottomDockWidgetArea, "dock_tasks")
        self._default_layout()

    def _default_layout(self):
        for d in (self.d_lib, self.d_align):
            self.addDockWidget(Qt.LeftDockWidgetArea, d); d.show(); d.setFloating(False)
        self.splitDockWidget(self.d_lib, self.d_align, Qt.Vertical)
        for d in (self.d_prev, self.d_cut, self.d_ctx, self.d_pack):
            self.addDockWidget(Qt.RightDockWidgetArea, d); d.show(); d.setFloating(False)
        self.splitDockWidget(self.d_prev, self.d_cut, Qt.Vertical)
        self.splitDockWidget(self.d_cut, self.d_ctx, Qt.Vertical)
        self.tabifyDockWidget(self.d_ctx, self.d_pack)
        self.d_ctx.raise_()
        self.addDockWidget(Qt.BottomDockWidgetArea, self.d_tasks); self.d_tasks.show()
        self.resizeDocks([self.d_lib, self.d_prev], [250, 520], Qt.Horizontal)
        self.resizeDocks([self.d_lib, self.d_align], [520, 220], Qt.Vertical)
        self.resizeDocks([self.d_prev, self.d_cut, self.d_ctx], [400, 170, 220], Qt.Vertical)
        self.resizeDocks([self.d_tasks], [110], Qt.Vertical)

    def _build_menu(self):
        m = self.menuBar()
        fm = m.addMenu("文件")
        a = QAction("选择媒体库文件夹…", self); a.triggered.connect(self.choose_root); fm.addAction(a)
        a = QAction("重新扫描", self); a.setShortcut("F5"); a.triggered.connect(self.rescan); fm.addAction(a)
        fm.addSeparator()
        a = QAction("设置…", self); a.setShortcut(QKeySequence.Preferences); a.triggered.connect(self.open_settings)
        fm.addAction(a)
        vm = m.addMenu("视图")
        for d in (self.d_lib, self.d_align, self.d_prev, self.d_cut, self.d_ctx, self.d_pack, self.d_tasks):
            vm.addAction(d.toggleViewAction())
        vm.addSeparator()
        a = QAction("恢复默认布局", self); a.triggered.connect(self._default_layout); vm.addAction(a)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=lambda: (self.q.setFocus(), self.q.selectAll()))
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.play_clip)
        QShortcut(QKeySequence("Ctrl+Down"), self, activated=lambda: self.step(1))
        QShortcut(QKeySequence("Ctrl+Up"), self, activated=lambda: self.step(-1))
        QShortcut(QKeySequence("Ctrl+D"), self, activated=lambda: self.cur and self.collect(self.cur))

    # ------------------------------------------------------------ 媒体库
    def _first_run(self):
        QMessageBox.information(self, APP, "先选择媒体库文件夹（移动硬盘上那个）。\n\n"
                                "整理方式：剧名/S01/S01E01.mp4，字幕同名放旁边（.srt 或 .ass，单独中文字幕加 .zh）。\n"
                                "电影：电影/片名 (年份)/片名 (年份).mp4")
        self.choose_root()

    def choose_root(self):
        p = QFileDialog.getExistingDirectory(self, "选择媒体库文件夹", self.qs.value("root", ""))
        if p:
            self.open_library(p)

    def open_library(self, root):
        err = tools.check()
        if err:
            QMessageBox.warning(self, APP, err)
        if self.align_worker:
            self.align_worker.stop(); self.align_worker.wait(3000); self.align_worker = None
        self.lib = Library(root)
        self.searcher = Searcher(self.lib)
        self.qs.setValue("root", root)
        self.root_lb.setText(root)
        self.setWindowTitle(f"{APP} {VERSION} — {Path(root).name}")
        self.refresh_tree(); self.refresh_packs()
        self.rescan()
        self.rescan_timer.start()

    def rescan(self):
        if not self.lib:
            return
        if getattr(self, "_scan", None) and self._scan.isRunning():
            return
        self.al_lb.setText("正在扫描媒体库…")
        self._scan = ScanWorker(str(self.lib.root))
        self._scan.progress.connect(lambda n, t, k: self.al_lb.setText(f"扫描 {n}/{t}　{k}"))
        self._scan.finished_ok.connect(self._scan_done)
        self._scan.failed.connect(lambda e: QMessageBox.warning(self, APP, "扫描失败：\n" + e))
        self._scan.start()

    def _scan_done(self, st):
        self.al_lb.setText(f"扫描完成：共 {st['total']} 集，新增 {st['new']}，更新 {st['updated']}，移除 {st['removed']}")
        self.refresh_tree()
        self.start_align()

    def refresh_tree(self):
        self.tree.clear()
        if not self.lib:
            return
        cur = self.f_show.currentText()
        self.f_show.blockSignals(True)
        self.f_show.clear(); self.f_show.addItem("全部作品", "")
        for r in self.lib.shows():
            it = QTreeWidgetItem([r["show"], str(r["n"]), f"{r['aligned'] or 0}/{r['has_video'] or 0}"])
            it.setData(0, Qt.UserRole, r["show"])
            if not r["has_video"]:
                it.setToolTip(0, "只有字幕，没有视频：能搜索，暂时不能播放")
            self.tree.addTopLevelItem(it)
            self.f_show.addItem(r["show"], r["show"])
        i = self.f_show.findText(cur)
        self.f_show.setCurrentIndex(max(0, i))
        self.f_show.blockSignals(False)

    def _tree_pick(self, it, _col):
        show = it.data(0, Qt.UserRole)
        i = self.f_show.findData(show)
        if i >= 0:
            self.f_show.setCurrentIndex(i)

    # ------------------------------------------------------------ 对齐
    def lib_settings(self):
        g = self.lib.get_setting
        return dict(align_model=g("align_model", "small"), align_engine=g("align_engine", ""),
                    hf_mirror=g("hf_mirror", True), auto_align=g("auto_align", True))

    def start_align(self):
        if not self.lib or not self.lib_settings()["auto_align"]:
            self.al_lb.setText("自动对齐已关闭（设置里可打开）")
            return
        if self.align_worker and self.align_worker.isRunning():
            self.align_worker.settings = self.lib_settings()
            self.align_worker.kick()
            return
        self.align_worker = AlignWorker(str(self.lib.root), self.lib_settings())
        self.align_worker.status.connect(self._align_status)
        self.align_worker.episode_done.connect(self._align_done)
        self.align_worker.idle.connect(self._align_idle)
        self.align_worker.start()

    def _align_status(self, key, f, done, todo):
        self.al_lb.setText(f"正在对齐：{key}\n已完成 {done} 集，还剩 {todo} 集")
        self.al_bar.setValue(int(f * 1000))

    def _align_done(self, key, msg):
        self.al_log.insertItem(0, f"{time.strftime('%H:%M')}  {key}  {msg}")
        self.refresh_tree()
        if self.cur and self.cur.media_key == key:
            self.refresh_current()

    def _align_idle(self):
        n = self.lib.db.execute("SELECT COUNT(*) FROM media WHERE align_status='done'").fetchone()[0] if self.lib else 0
        self.al_lb.setText(f"后台对齐：全部完成（{n} 集）")
        self.al_bar.setValue(0)

    def _toggle_align(self):
        if not self.align_worker:
            return
        on = not self.align_worker.paused
        self.align_worker.pause(on)
        self.al_btn.setText("继续" if on else "暂停")
        if on:
            self.al_lb.setText("后台对齐：已暂停（当前这集跑完后停）")

    # ------------------------------------------------------------ 搜索
    def do_search(self):
        if not self.searcher:
            return
        q = self.q.text().strip()
        if not q:
            return
        filters = {}
        if self.f_show.currentData():
            filters["shows"] = [self.f_show.currentData()]
        if self.f_season.value():
            filters["season"] = self.f_season.value()
        if self.c_video.isChecked():
            filters["video_only"] = True
        t = time.time()
        self.hits = self.searcher.search(q, exact=self.c_exact.isChecked(), ordered=self.c_order.isChecked(),
                                         single_only=self.c_single.isChecked(), filters=filters,
                                         max_dur=self.f_maxdur.value() or None)
        self.search_ms = (time.time() - t) * 1000
        counts = {k: 0 for k in (TIER_ORIG, TIER_NEAR, TIER_FUNC)}
        for h in self.hits:
            counts[h.tier] = counts.get(h.tier, 0) + 1
        self.tabs.setTabText(0, f"全部 {len(self.hits)}")
        for i, k in enumerate((TIER_ORIG, TIER_NEAR, TIER_FUNC), 1):
            self.tabs.setTabText(i, f"{k} {counts[k]}")
        self.render_results()

    def filtered(self):
        i = self.tabs.currentIndex()
        if i <= 0:
            return self.hits
        tier = (TIER_ORIG, TIER_NEAR, TIER_FUNC)[i - 1]
        return [h for h in self.hits if h.tier == tier]

    def render_results(self):
        self.results.clear()
        self.shown = 0
        self._more()
        n = len(self.filtered())
        self.count_lb.setText(f"{n} 条 · 同集同句已合并 · {getattr(self, 'search_ms', 0):.0f} ms")

    def _more(self):
        lst = self.filtered()
        for h in lst[self.shown:self.shown + PAGE]:
            row = self._cue_row(h)
            s, e, src = self.lib.final_span(row, h.media_key)
            card = ResultCard(h, src, (s, e))
            card.preview.connect(self.preview)
            card.collect.connect(self.collect)
            card.context.connect(self.show_context)
            card.export_one.connect(self.export_one)
            it = QListWidgetItem()
            it.setSizeHint(card.sizeHint() + QSize(0, 4))
            self.results.addItem(it)
            self.results.setItemWidget(it, card)
        self.shown = min(len(lst), self.shown + PAGE)

    def _maybe_more(self, v):
        sb = self.results.verticalScrollBar()
        if v >= sb.maximum() - 40 and self.shown < len(self.filtered()):
            self._more()

    def _cue_row(self, h):
        return self.lib.db.execute("SELECT * FROM cues WHERE id=?", (h.cue_id,)).fetchone()

    # ------------------------------------------------------------ 预览 / 切点
    def preview(self, h, autoplay=True):
        self.cur = h
        self.refresh_current()
        self.show_context(h)
        if not h.has_video:
            return
        m = self.lib.media_by_key(h.media_key)
        path = self.lib.abs(m["rel_video"])
        url = QUrl.fromLocalFile(str(path))
        self._pending_play = autoplay
        if self.player.source() != url:
            self.player.setSource(url)
        elif autoplay:
            self.play_clip()

    def refresh_current(self):
        h = self.cur
        if not h:
            return
        row = self._cue_row(h)
        s, e, src = self.lib.final_span(row, h.media_key)
        self.cur_span = (s, e)
        self.sub_en.setText(highlight_html(h.en, h.matched))
        self.sub_zh.setText(h.zh)
        words = self.lib.words(h.media_key)
        t0 = min(s, row["start"]) - 1.5
        t1 = max(e, row["end"]) + 1.5
        self.wave.set_data(t0, t1, words, (row["start"], row["end"]), (s, e))
        self._update_cut_label(row, s, e, src)
        if h.has_video:
            m = self.lib.media_by_key(h.media_key)
            self._wave_token += 1
            w = WaveWorker(self._wave_token, self.lib.abs(m["rel_video"]), t0, t1)
            w.done.connect(self._wave_done)
            self._wave_workers.append(w)
            w.finished.connect(lambda w=w: self._wave_workers.remove(w) if w in self._wave_workers else None)
            w.start()

    def _wave_done(self, token, peaks):
        if token == self._wave_token and peaks is not None:
            self.wave.set_peaks(peaks)

    def _update_cut_label(self, row, s, e, src):
        name, color = TIMING_TEXT.get(src, ("", "#000"))
        self.cut_lb.setText(
            f"<span style='color:{color}'>● {name}</span>　切点 {fmt_ms(s)} → {fmt_ms(e)}（{e - s:.2f} 秒）　"
            f"原字幕 {fmt_ms(row['start'])} → {fmt_ms(row['end'])}　修正 {s - row['start']:+.2f} / {e - row['end']:+.2f} 秒")

    def _on_status(self, st):
        if st in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and getattr(self, "_pending_play", False):
            self._pending_play = False
            self.play_clip()

    def play_clip(self):
        if not self.cur_span or not self.cur or not self.cur.has_video:
            return
        self._stop_at = self.cur_span[1]
        self.player.setPosition(int(self.cur_span[0] * 1000))
        self.player.setPlaybackRate(self.speed.currentData())
        self.player.play()

    def _on_pos(self, ms):
        t = ms / 1000
        self.wave.set_playhead(t)
        stop = getattr(self, "_stop_at", None)
        if stop is not None and t >= stop and self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self._stop_at = None
            if self.c_loop.isChecked():
                QTimer.singleShot(400, lambda: self.step(1))

    def step(self, d):
        lst = [h for h in self.filtered() if h.has_video]
        if not lst:
            return
        try:
            i = next(k for k, h in enumerate(lst) if self.cur and h.cue_id == self.cur.cue_id)
        except StopIteration:
            i = -1 if d > 0 else len(lst)
        j = i + d
        if 0 <= j < len(lst):
            self.preview(lst[j])
            for k in range(self.results.count()):
                w = self.results.itemWidget(self.results.item(k))
                if w and w.hit.cue_id == lst[j].cue_id:
                    self.results.setCurrentRow(k)
                    self.results.scrollToItem(self.results.item(k))
                    break

    def _cut_changed(self, s, e):
        self.cur_span = (s, e)
        row = self._cue_row(self.cur)
        self._update_cut_label(row, s, e, "manual")
        self._dirty_cut = True

    def save_cut(self):
        if self.cur and self.cur_span:
            self.lib.save_adjust(self.cur.media_key, self.cur.idx, *self.cur_span)
            self.statusBar().showMessage("已保存微调（重新对齐也不会覆盖）", 3000)
            self.refresh_current()

    def reset_cut(self):
        if self.cur:
            self.lib.clear_adjust(self.cur.media_key, self.cur.idx)
            self.refresh_current()

    # ------------------------------------------------------------ 上下文
    def show_context(self, h):
        self.ctx.clear()
        rows = self.lib.context(h.media_id, h.idx, 2)
        for r in rows:
            mark = "▶ " if r["idx"] == h.idx else "　"
            it = QListWidgetItem(f"{mark}{fmt_ms(r['start'])}  {r['en']}" + (f"\n　　{r['zh']}" if r["zh"] else ""))
            it.setData(Qt.UserRole, r["idx"])
            if r["idx"] == h.idx:
                f = it.font(); f.setBold(True); it.setFont(f)
            self.ctx.addItem(it)
        self.d_ctx.raise_()

    def _ctx_pick(self, it):
        if not self.cur:
            return
        idx = it.data(Qt.UserRole)
        r = self.lib.db.execute("SELECT * FROM cues WHERE media_id=? AND idx=?", (self.cur.media_id, idx)).fetchone()
        s, e, _ = self.lib.final_span(r, self.cur.media_key)
        self._stop_at = e
        self.player.setPosition(int(s * 1000)); self.player.play()

    # ------------------------------------------------------------ 采集包
    def refresh_packs(self, select=None):
        self.pack_cb.blockSignals(True)
        cur = select or self.pack_cb.currentText()
        self.pack_cb.clear()
        for p in self.lib.packs():
            self.pack_cb.addItem(p["name"], p["id"])
        i = self.pack_cb.findText(cur.split("（")[0]) if cur else -1
        self.pack_cb.setCurrentIndex(max(0, i))
        self.pack_cb.blockSignals(False)
        self.load_pack()

    def new_pack(self):
        name, ok = QInputDialog.getText(self, "新建采集包", "包名（一般用句型，例如 What are you doing?）",
                                        text=self.q.text().strip())
        if ok and name.strip():
            self.lib.pack_id(name.strip())
            self.refresh_packs(name.strip())

    def collect(self, h):
        if self.pack_cb.count() == 0:
            self.lib.pack_id(self.q.text().strip() or "未命名")
            self.refresh_packs()
        name = self.pack_cb.currentText()
        ok = self.lib.add_to_pack(name, h.media_key, h.idx, self.q.text().strip(), h.tier)
        self.statusBar().showMessage(f"已采集到「{name}」" if ok else "这条已经在包里了", 2500)
        self.load_pack()
        self.d_pack.raise_()

    def load_pack(self):
        self.pack_list.clear()
        pid = self.pack_cb.currentData()
        if pid is None:
            return
        for r in self.lib.pack_items(pid):
            label = f"{r['show']} S{r['season']:02d}E{r['episode']:02d}" if r["kind"] == "tv" else r["show"]
            it = QListWidgetItem(f"{label}　{r['en']}" + (f"\n{r['zh']}" if r["zh"] else ""))
            it.setData(Qt.UserRole, r["id"])
            it.setData(Qt.UserRole + 1, (r["media_key"], r["idx"]))
            self.pack_list.addItem(it)

    def _pack_move(self, d):
        it = self.pack_list.currentItem()
        if it:
            row = self.pack_list.currentRow()
            self.lib.move_item(it.data(Qt.UserRole), d)
            self.load_pack()
            self.pack_list.setCurrentRow(max(0, min(self.pack_list.count() - 1, row + d)))

    def _pack_remove(self):
        it = self.pack_list.currentItem()
        if it:
            self.lib.remove_item(it.data(Qt.UserRole))
            self.load_pack()

    def _pack_pick(self, it):
        key, idx = it.data(Qt.UserRole + 1)
        from ..search import Hit
        r = self.lib.db.execute("SELECT c.*, m.key media_key, m.show, m.season, m.episode, m.kind, "
                                "m.rel_video IS NOT NULL has_video FROM cues c JOIN media m ON m.id=c.media_id "
                                "WHERE m.key=? AND c.idx=?", (key, idx)).fetchone()
        if r:
            h = Hit(r["id"], r["media_id"], key, r["show"], r["season"], r["episode"], r["kind"], r["idx"],
                    r["start"], r["end"], r["en"], r["zh"] or "", r["speakers"], r["a_start"], r["a_end"],
                    r["a_score"], bool(r["has_video"]), "", 0, "")
            self.preview(h)

    # ------------------------------------------------------------ 导出
    def style(self) -> Style:
        return Style.load(self.lib.get_setting("style"))

    def _export_dir(self):
        d = self.lib.get_setting("export_dir", "") or str(self.lib.root / "导出")
        return d

    def export_pack(self):
        pid = self.pack_cb.currentData()
        if pid is None:
            return
        name = self.pack_cb.currentText()
        n = self.pack_list.count()
        if not n:
            return
        from ..export import safe_name
        dlg = ExportDialog(f"采集包「{name}」", n, str(Path(self._export_dir()) / safe_name(name)), self)
        if dlg.exec():
            self._run_export(f"采集包「{name}」", dlg.dir.text(), dlg.presets(), dlg.comp.isChecked(), pack_id=pid)

    def export_one(self, h):
        from ..export import safe_name
        dlg = ExportDialog(h.label, 1, str(Path(self._export_dir()) / "单条"), self)
        if dlg.exec():
            spec = [dict(media_key=h.media_key, idx=h.idx, pattern=self.q.text().strip(), tier=h.tier,
                         matched=h.matched)]
            self._run_export(f"{h.label} · {h.en[:30]}", dlg.dir.text(), dlg.presets(), False, items=spec)

    def _run_export(self, title, out, presets, comp, pack_id=None, items=None):
        if not presets:
            return
        it = QListWidgetItem(f"⏳ {title}　准备中…")
        it.setData(Qt.UserRole, out)
        self.tasks.insertItem(0, it)
        self.d_tasks.raise_()
        w = ExportWorker(str(self.lib.root), items or [], out, presets, self.style(), comp, pack_id)
        w.progress.connect(lambda n, t, s, it=it: it.setText(f"⏳ {title}　{n}/{t}　{s}"))
        w.finished_ok.connect(lambda r, it=it: self._export_done(it, title, r))
        self.export_workers.append(w)
        w.start()

    def _export_done(self, it, title, r):
        if r.get("ok"):
            comp = "，合集 " + "、".join(r.get("compilations", {}).keys()) if r.get("compilations") else ""
            it.setText(f"✅ {title}　完成 {r.get('count', 0)} 条{comp}　（双击打开文件夹）")
        else:
            it.setText(f"⚠️ {title}　有错误：" + "；".join(r.get("errors", [])[:3]))
        it.setToolTip("\n".join(r.get("errors", [])))

    def _open_task(self, it):
        p = it.data(Qt.UserRole)
        if p and Path(p).exists():
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    # ------------------------------------------------------------ 设置
    def open_settings(self):
        if not self.lib:
            return
        g = self.lib.get_setting
        settings = dict(ffmpeg=self.qs.value("ffmpeg", ""), export_dir=g("export_dir", ""),
                        pad_pre=g("pad_pre", 0.30), pad_post=g("pad_post", 0.35), **self.lib_settings())
        ctx = None
        if self.cur and self.cur.has_video and self.cur_span:
            m = self.lib.media_by_key(self.cur.media_key)
            ctx = dict(video=str(self.lib.abs(m["rel_video"])), t=(self.cur_span[0] + self.cur_span[1]) / 2,
                       en=self.cur.en, zh=self.cur.zh, matched=self.cur.matched, title=self.q.text().strip(),
                       source=self.cur.label)
        dlg = SettingsDialog(settings, self.style(), ctx, self)
        if dlg.exec():
            s = dlg.settings()
            self.qs.setValue("ffmpeg", s.pop("ffmpeg"))
            tools.set_paths(self.qs.value("ffmpeg") or None)
            for k, v in s.items():
                self.lib.set_setting(k, v)
            from dataclasses import asdict
            self.lib.set_setting("style", asdict(dlg.style()))
            self.start_align()
            if self.cur:
                self.refresh_current()

    def closeEvent(self, e):
        self.qs.setValue("state", self.saveState())
        self.qs.setValue("geometry", self.saveGeometry())
        if self.align_worker:
            self.align_worker.stop()
            if not self.align_worker.wait(1500):
                # 正在识别中：直接结束线程。对齐结果是整集跑完才写盘的，不会留下半截文件
                self.align_worker.terminate()
                self.align_worker.wait(1000)
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setOrganizationName("local33")
    from PySide6.QtGui import QIcon
    if icon_path().exists():
        app.setWindowIcon(QIcon(str(icon_path())))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
