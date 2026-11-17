"""主窗口：33 台词式「搜 → 播 → 采集 → 导出」，所有面板都是可拖动、可调大小、可浮出的停靠窗。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QSettings, QTimer, QUrl, QSize, QPoint
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut, QFont, QDesktopServices
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QMainWindow, QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
                               QLineEdit, QPushButton, QCheckBox, QComboBox, QLabel, QListWidget,
                               QListWidgetItem, QTabBar, QFileDialog, QMessageBox, QProgressBar, QSpinBox,
                               QInputDialog, QTreeWidget, QTreeWidgetItem, QSizePolicy, QFrame, QDoubleSpinBox,
                               QAbstractItemView, QToolBar, QStackedLayout, QTabWidget, QToolButton)

from .. import tools
from ..paths import VERSION, icon_path
from ..library import Library
from ..search import Searcher, TIER_ORIG, TIER_NEAR, TIER_FUNC
from ..export import Style
from .widgets import ResultCard, WaveformBar, fmt_ms, highlight_html, TIMING_TEXT, FlowLayout
from .workers import ScanWorker, AlignWorker, ExportWorker, WaveWorker, UpdateCheck, UpdateDownload
from .dialogs import SettingsDialog, ExportDialog
from . import theme
from .theme import icon, GlassTitle, glass_body

APP = "台词切片编辑器"
PAGE = 60



class MainWindow(QMainWindow):
    def __init__(self, autostart=True):
        super().__init__()
        if not QApplication.instance().styleSheet():
            theme.apply_theme(QApplication.instance())
        self.setWindowTitle(f"{APP} {VERSION}")
        self.resize(1440, 900)
        self._autostart = autostart
        self.qs = QSettings("local33", "clipper")
        tools.set_paths(self.qs.value("ffmpeg", "") or None)
        self.lib: Library | None = None
        self.searcher: Searcher | None = None
        self.hits = []
        self.shown = 0
        self.cur = None            # 当前预览的 hit
        self.cur_span = None
        self.align_worker = None
        self.align_center = None
        self.export_workers = []
        self._wave_token = 0
        self._wave_workers = []
        self.setDockNestingEnabled(True)
        self.setDockOptions(QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks | QMainWindow.AnimatedDocks)
        self.setTabPosition(Qt.AllDockWidgetAreas, QTabWidget.North)   # 叠在一起的面板，标签放上面
        self.setContentsMargins(8, 0, 8, 6)
        self._build_search()
        self._build_docks()
        self._build_menu()
        st = self.qs.value("state106")   # 1.0.6 布局重排：不沿用旧版保存的面板位置
        if st is not None:
            self.restoreState(st)
        geo = self.qs.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        self.rescan_timer = QTimer(self, interval=5 * 60 * 1000, timeout=self.rescan)
        root = self.qs.value("root", "")
        if not autostart:
            pass
        elif root and Path(root).exists():
            QTimer.singleShot(50, lambda: self.open_library(root))
        else:
            QTimer.singleShot(50, self._first_run)

    def paintEvent(self, e):
        from PySide6.QtGui import QPainter
        p = QPainter(self)
        theme.paint_backdrop(p, self.rect())
        p.end()

    # ------------------------------------------------------------ 构建界面
    def _build_search(self):
        outer = QWidget()
        ov = QVBoxLayout(outer)
        ov.setContentsMargins(0, 0, 0, 0)
        w = QFrame(); w.setObjectName("glass")
        ov.addWidget(w)
        v = QVBoxLayout(w)
        v.setContentsMargins(16, 14, 16, 8)
        v.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.q = QLineEdit(); self.q.setObjectName("bigsearch")
        self.q.addAction(icon("search", theme.TEXT3, 16), QLineEdit.LeadingPosition)
        self.q.setPlaceholderText("搜索台词或句型，例如  what are you doing　/　whose * is this　/　is this your ...")
        self.q.returnPressed.connect(self.do_search)
        row.addWidget(self.q, 1)
        b = QPushButton("搜索"); b.setObjectName("primary"); b.setProperty("class", "big")
        b.setStyleSheet("border-radius:19px;padding:9px 24px;font-size:14px;")
        b.clicked.connect(self.do_search)
        row.addWidget(b)
        v.addLayout(row)
        opts = FlowLayout(spacing=6)
        # 筛选项做成可点的小胶囊（接口和复选框一样：isChecked / toggled）

        def chip(text, on=False):
            c = QPushButton(text); c.setObjectName("chip"); c.setCheckable(True); c.setChecked(on)
            c.setCursor(Qt.PointingHandCursor)
            return c
        self.c_exact = chip("精确"); self.c_exact.setToolTip("不做词形变化（loved 不算 love）")
        self.c_order = chip("严格顺序"); self.c_order.setToolTip("词必须按顺序连在一起；* 或 ... 表示中间可以有几个词")
        self.c_single = chip("只看单人", True)
        self.c_video = chip("只看有视频")
        for c in (self.c_exact, self.c_order, self.c_single, self.c_video):
            c.toggled.connect(lambda _=False: self.do_search() if self.q.text().strip() else None)
            opts.addWidget(c)
        def pair(text, w):
            box = QWidget(); hl = QHBoxLayout(box); hl.setContentsMargins(0, 0, 0, 0); hl.setSpacing(4)
            lb = QLabel(text); lb.setObjectName("muted")
            hl.addWidget(lb); hl.addWidget(w)
            opts.addWidget(box)
        self.f_show = QComboBox(); self.f_show.setMinimumWidth(140)
        self.f_show.currentIndexChanged.connect(lambda _: self.do_search() if self.q.text().strip() else None)
        pair("作品", self.f_show)
        self.f_season = QSpinBox(); self.f_season.setRange(0, 99); self.f_season.setSpecialValueText("全部")
        self.f_season.valueChanged.connect(lambda _: self.do_search() if self.q.text().strip() else None)
        pair("季", self.f_season)
        self.f_maxdur = QDoubleSpinBox(); self.f_maxdur.setRange(0, 60); self.f_maxdur.setValue(8)
        self.f_maxdur.setSuffix(" 秒"); self.f_maxdur.setSpecialValueText("不限")
        pair("最长", self.f_maxdur)
        v.addLayout(opts)
        self.tabs = QTabBar()
        for t in ("全部", TIER_ORIG, TIER_NEAR, TIER_FUNC):
            self.tabs.addTab(t)
        self.tabs.setDrawBase(False)
        self.tabs.currentChanged.connect(lambda _: self.render_results())
        self.tabs.setExpanding(False)
        th = FlowLayout(spacing=8); th.addWidget(self.tabs)
        self.count_lb = QLabel(""); self.count_lb.setObjectName("muted")
        th.addWidget(self.count_lb)
        self.count_lb.setContentsMargins(4, 6, 0, 0)
        v.addLayout(th)
        self.results = QListWidget(); self.results.setObjectName("results")
        self.results.setSpacing(5)
        self.results.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.results.verticalScrollBar().valueChanged.connect(self._maybe_more)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.results.setMinimumWidth(330)   # 卡片里四个按钮的宽度；再窄就该拖面板了
        self._relayout_timer = QTimer(self, singleShot=True, interval=60, timeout=self._relayout_cards)
        self.results.viewport().installEventFilter(self)
        v.addWidget(self.results, 1)
        self.setCentralWidget(outer)

    def _dock(self, title, widget, area, name):
        d = QDockWidget(title, self)
        d.setObjectName(name)
        d.setTitleBarWidget(GlassTitle(d))
        d.setWidget(glass_body(widget))
        d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable)
        self.addDockWidget(area, d)
        return d

    def _build_docks(self):
        # 媒体库（左上）
        lw = QWidget(); lv = QVBoxLayout(lw); lv.setContentsMargins(0, 0, 0, 0); lv.setSpacing(6)
        self.root_lb = QLabel("未选择媒体库"); self.root_lb.setWordWrap(True); self.root_lb.setObjectName("small")
        lv.addWidget(self.root_lb)
        hb = FlowLayout(spacing=6)
        b1 = QPushButton(icon("folder", theme.ACCENT_D, 16), "选择"); b1.clicked.connect(self.choose_root)
        b1.setToolTip("选择媒体库文件夹")
        b2 = QPushButton(icon("refresh", theme.ACCENT_D, 16), "扫描"); b2.clicked.connect(self.rescan)
        b2.setToolTip("重新扫描媒体库")
        hb.addWidget(b1); hb.addWidget(b2); lv.addLayout(hb)
        self.tree = QTreeWidget(); self.tree.setHeaderLabels(["作品", "集", "对齐"])
        self.tree.setColumnWidth(0, 120); self.tree.setColumnWidth(1, 34)
        self.tree.setRootIsDecorated(False)
        from PySide6.QtWidgets import QHeaderView
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.itemDoubleClicked.connect(self._tree_pick)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.setToolTip("右键作品：对齐这部作品")
        lv.addWidget(self.tree, 1)
        self.d_lib = self._dock("媒体库", lw, Qt.LeftDockWidgetArea, "dock_lib")
        # 对齐（左下，紧凑）
        aw = QWidget(); av = QVBoxLayout(aw); av.setContentsMargins(0, 0, 0, 0); av.setSpacing(6)
        self.al_lb = QLabel("右键作品或到对齐中心选集")
        self.al_lb.setWordWrap(True)
        self.al_lb.setObjectName("muted")
        self.al_bar = QProgressBar(); self.al_bar.setRange(0, 1000); self.al_bar.setTextVisible(False)
        self.al_bar.setFixedHeight(6)
        self.al_btn = QPushButton("暂停"); self.al_btn.clicked.connect(self._toggle_align)
        self.al_center_btn = QPushButton(icon("wave", "#FFFFFF", 16), "对齐中心"); self.al_center_btn.setObjectName("primary")
        self.al_center_btn.clicked.connect(self.open_align_center)
        self.al_log = QListWidget(); self.al_log.setMaximumHeight(110)
        self.al_log.setStyleSheet(f"font-size:11px;color:{theme.TEXT2};")
        av.addWidget(self.al_lb); av.addWidget(self.al_bar)
        ah = FlowLayout(spacing=6); ah.addWidget(self.al_center_btn); ah.addWidget(self.al_btn); av.addLayout(ah)
        av.addWidget(self.al_log)
        self.d_align = self._dock("对齐", aw, Qt.LeftDockWidgetArea, "dock_align")
        # 预览（右上）
        pw = QWidget(); pv = QVBoxLayout(pw); pv.setContentsMargins(0, 0, 0, 0); pv.setSpacing(6)
        vf = QFrame(); vf.setObjectName("videoFrame")
        vfl = QVBoxLayout(vf); vfl.setContentsMargins(6, 6, 6, 6)
        self.video = QVideoWidget(); self.video.setMinimumSize(160, 90)
        self.video.setStyleSheet("background:#0F172A;")
        vfl.addWidget(self.video)
        self.player = QMediaPlayer(self); self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio); self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self._on_pos)
        self.player.mediaStatusChanged.connect(self._on_status)
        pv.addWidget(vf, 1)
        self.sub_en = QLabel(""); self.sub_en.setAlignment(Qt.AlignCenter); self.sub_en.setWordWrap(True)
        self.sub_en.setStyleSheet(f"font-size:16px;font-weight:600;color:{theme.TEXT};")
        self.sub_zh = QLabel(""); self.sub_zh.setAlignment(Qt.AlignCenter); self.sub_zh.setWordWrap(True)
        self.sub_zh.setStyleSheet(f"font-size:13px;color:{theme.TEXT2};")
        pv.addWidget(self.sub_en); pv.addWidget(self.sub_zh)
        ctl = FlowLayout(spacing=6)
        b_prev = QPushButton(icon("prev", theme.ACCENT_D, 16), ""); b_prev.clicked.connect(lambda: self.step(-1))
        b_prev.setToolTip("上一条（Ctrl+↑）")
        self.b_play = QPushButton(icon("play", "#FFFFFF", 16), "播放本句"); self.b_play.setObjectName("primary")
        self.b_play.clicked.connect(self.play_clip)
        b_next = QPushButton(icon("next", theme.ACCENT_D, 16), ""); b_next.clicked.connect(lambda: self.step(1))
        b_next.setToolTip("下一条（Ctrl+↓）")
        self.speed = QComboBox()
        for s in ("0.5", "0.75", "1.0", "1.25"):
            self.speed.addItem(s + "×", float(s))
        self.speed.setCurrentIndex(2)
        self.speed.currentIndexChanged.connect(lambda _: self.player.setPlaybackRate(self.speed.currentData()))
        self.speed.setToolTip("倍速")
        self.c_loop = QPushButton(icon("loop", theme.ACCENT_D, 16), "连播"); self.c_loop.setObjectName("chip")
        self.c_loop.setCheckable(True)
        for x in (b_prev, self.b_play, b_next, self.speed, self.c_loop):
            ctl.addWidget(x)
        pv.addLayout(ctl)
        self.d_prev = self._dock("预览", pw, Qt.RightDockWidgetArea, "dock_preview")
        # 切点
        cw = QWidget(); cv = QVBoxLayout(cw); cv.setContentsMargins(0, 0, 0, 0); cv.setSpacing(6)
        self.wave = WaveformBar()
        self.wave.changed.connect(self._cut_changed)
        self.wave.seek.connect(lambda t: self.player.setPosition(int(t * 1000)))
        cv.addWidget(self.wave, 1)
        self.cut_lb = QLabel(""); self.cut_lb.setObjectName("small")
        self.cut_lb.setWordWrap(True)
        cv.addWidget(self.cut_lb)
        ch = FlowLayout(spacing=6)
        bt = QPushButton(icon("play", theme.ACCENT_D, 14), "试听"); bt.clicked.connect(self.play_clip)
        bs = QPushButton(icon("bookmark", theme.ACCENT_D, 16), "保存微调"); bs.clicked.connect(self.save_cut)
        br = QPushButton("恢复自动"); br.setObjectName("ghost"); br.clicked.connect(self.reset_cut)
        ch.addWidget(bt); ch.addWidget(bs); ch.addWidget(br)
        self.wave.setToolTip("拖两端调整，吸附词边界；按住 Shift 自由拖")
        cv.addLayout(ch)
        self.d_cut = self._dock("切点", cw, Qt.RightDockWidgetArea, "dock_cut")
        # 上下文
        self.ctx = QListWidget(); self.ctx.setWordWrap(True)
        self.ctx.itemDoubleClicked.connect(self._ctx_pick)
        self.d_ctx = self._dock("上下文", self.ctx, Qt.RightDockWidgetArea, "dock_ctx")
        # 采集包
        kw = QWidget(); kv = QVBoxLayout(kw); kv.setContentsMargins(0, 0, 0, 0); kv.setSpacing(6)
        kh = QHBoxLayout(); kh.setSpacing(6)
        self.pack_cb = QComboBox(); self.pack_cb.currentIndexChanged.connect(lambda _: self.load_pack())
        bn = QPushButton(icon("newpack", theme.ACCENT_D, 16), "新建"); bn.clicked.connect(self.new_pack)
        kh.addWidget(self.pack_cb, 1); kh.addWidget(bn)
        kv.addLayout(kh)
        self.pack_list = QListWidget(); self.pack_list.setWordWrap(True)
        self.pack_list.itemDoubleClicked.connect(self._pack_pick)
        kv.addWidget(self.pack_list, 1)
        kb = FlowLayout(spacing=6)
        for t, fn in (("上移", lambda: self._pack_move(-1)), ("下移", lambda: self._pack_move(1)),
                      ("移除", self._pack_remove)):
            b = QPushButton(t); b.setObjectName("ghost"); b.clicked.connect(fn); kb.addWidget(b)
        be = QPushButton(icon("package", "#FFFFFF", 16), "导出此包"); be.setObjectName("primary")
        be.clicked.connect(self.export_pack)
        kb.addWidget(be)
        kv.addLayout(kb)
        self.d_pack = self._dock("采集包", kw, Qt.RightDockWidgetArea, "dock_pack")
        # 导出任务（和上下文、采集包叠成标签，不再占底部一整条）
        self.tasks = QListWidget(); self.tasks.setWordWrap(True)
        self.tasks.itemDoubleClicked.connect(self._open_task)
        self.d_tasks = self._dock("导出任务", self.tasks, Qt.RightDockWidgetArea, "dock_tasks")
        self._default_layout()

    def _default_layout(self):
        """左：媒体库 + 对齐；中：搜索与结果；右：预览 → 切点 → 上下文 / 采集包 / 导出任务（标签）。"""
        for d in (self.d_lib, self.d_align):
            self.addDockWidget(Qt.LeftDockWidgetArea, d); d.show(); d.setFloating(False)
        self.splitDockWidget(self.d_lib, self.d_align, Qt.Vertical)
        for d in (self.d_prev, self.d_cut, self.d_ctx, self.d_pack, self.d_tasks):
            self.addDockWidget(Qt.RightDockWidgetArea, d); d.show(); d.setFloating(False)
        self.splitDockWidget(self.d_prev, self.d_cut, Qt.Vertical)
        self.splitDockWidget(self.d_cut, self.d_ctx, Qt.Vertical)
        self.tabifyDockWidget(self.d_ctx, self.d_pack)
        self.tabifyDockWidget(self.d_pack, self.d_tasks)
        self.d_ctx.raise_()
        self.resizeDocks([self.d_lib, self.d_prev], [250, 500], Qt.Horizontal)
        self.resizeDocks([self.d_lib, self.d_align], [560, 200], Qt.Vertical)
        self.resizeDocks([self.d_prev, self.d_cut, self.d_ctx], [400, 190, 240], Qt.Vertical)

    def _act(self, text, fn, key=None, tip=None, icon=None, checkable=False):  # noqa: icon 是主题图标名
        a = QAction(text, self)
        if key:
            a.setShortcut(QKeySequence(key))
            a.setShortcutContext(Qt.ApplicationShortcut)
        if tip:
            a.setToolTip(tip); a.setStatusTip(tip)
        if icon is not None:
            a.setIcon(theme.icon(icon))
        a.setCheckable(checkable)
        a.setMenuRole(QAction.NoRole)   # Mac 上不要被挪进「应用程序」菜单，留在看得见的地方
        a.triggered.connect(fn)
        return a

    def _cur_do(self, fn):
        if self.cur:
            fn(self.cur)
        else:
            self.statusBar().showMessage("先在结果里选一条（预览一下）", 2500)

    def _build_menu(self):
        """所有功能都列在顶部菜单 + 工具栏（参照 PR）。"""
        m = self.menuBar()
        A = self.acts = {}
        # 文件
        fm = m.addMenu("文件")
        A["root"] = self._act("选择媒体库文件夹…", self.choose_root, "Ctrl+O", icon="folder")
        A["rescan"] = self._act("重新扫描媒体库", self.rescan, "F5", icon="refresh")
        A["open_root"] = self._act("在文件夹中打开媒体库", lambda: self.lib and QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.lib.root))))
        A["open_exp"] = self._act("打开导出文件夹", self._open_export_dir)
        A["settings"] = self._act("设置…", self.open_settings, "Ctrl+,", "常规 / 对齐 / 字幕样式",
                                  icon="settings")
        A["quit"] = self._act("退出", self.close, "Ctrl+Q")
        for k in ("root", "rescan", "open_root", "open_exp", None, "settings", None, "quit"):
            fm.addSeparator() if k is None else fm.addAction(A[k])
        # 搜索
        sm = m.addMenu("搜索")
        A["find"] = self._act("搜索框", lambda: (self.q.setFocus(), self.q.selectAll()), "Ctrl+F")
        A["search"] = self._act("执行搜索", self.do_search, icon="search")
        sm.addAction(A["find"]); sm.addAction(A["search"]); sm.addSeparator()
        for c in (self.c_exact, self.c_order, self.c_single, self.c_video):
            a = QAction(c.text(), self, checkable=True); a.setChecked(c.isChecked())
            a.toggled.connect(c.setChecked); c.toggled.connect(a.setChecked)
            sm.addAction(a)
        sm.addSeparator()
        for i, t in enumerate(("全部", TIER_ORIG, TIER_NEAR, TIER_FUNC)):
            sm.addAction(self._act(f"只看：{t}", lambda _=False, i=i: self.tabs.setCurrentIndex(i), f"Ctrl+{i + 1}"))
        # 片段
        cm = m.addMenu("片段")
        A["play"] = self._act("播放本句", self.play_clip, "Ctrl+Return", icon="play")
        A["prev"] = self._act("上一条", lambda: self.step(-1), "Ctrl+Up", icon="prev")
        A["next"] = self._act("下一条", lambda: self.step(1), "Ctrl+Down", icon="next")
        A["loop"] = self._act("连播", lambda on: self.c_loop.setChecked(on), checkable=True)
        self.c_loop.toggled.connect(A["loop"].setChecked)
        A["save_cut"] = self._act("保存微调", self.save_cut, "Ctrl+S", icon="bookmark")
        A["reset_cut"] = self._act("恢复自动切点", self.reset_cut)
        A["ctx"] = self._act("看上下文", lambda: self._cur_do(lambda h: (self.show_context(h), self.d_ctx.raise_())))
        A["collect"] = self._act("采集到当前包", lambda: self._cur_do(self.collect), "Ctrl+D", icon="collect")
        A["export_one"] = self._act("导出这一条…", lambda: self._cur_do(self.export_one), "Ctrl+E",
                                    icon="export")
        for k in ("play", "prev", "next", "loop", None, "save_cut", "reset_cut", "ctx", None, "collect", "export_one"):
            cm.addSeparator() if k is None else cm.addAction(A[k])
        spm = cm.addMenu("倍速")
        g = QActionGroup(self)
        for i in range(self.speed.count()):
            a = QAction(self.speed.itemText(i), self, checkable=True); g.addAction(a); spm.addAction(a)
            a.setChecked(i == self.speed.currentIndex())
            a.triggered.connect(lambda _=False, i=i: self.speed.setCurrentIndex(i))
        # 采集包
        pm = m.addMenu("采集包")
        A["new_pack"] = self._act("新建采集包…", self.new_pack, "Ctrl+N", icon="newpack")
        A["pack_up"] = self._act("选中条目上移", lambda: self._pack_move(-1))
        A["pack_down"] = self._act("选中条目下移", lambda: self._pack_move(1))
        A["pack_rm"] = self._act("移除选中条目", self._pack_remove)
        A["export_pack"] = self._act("导出此包…", self.export_pack, "Ctrl+Shift+E", icon="package")
        for k in ("new_pack", None, "pack_up", "pack_down", "pack_rm", None, "export_pack"):
            pm.addSeparator() if k is None else pm.addAction(A[k])
        # 对齐
        am = m.addMenu("对齐")
        A["align_center"] = self._act("对齐中心…", self.open_align_center, "Ctrl+L", "选集对齐、看进度和失败原因",
                                      icon="wave")
        A["align_cur"] = self._act("对齐当前这一集", lambda: self._cur_do(lambda h: self.queue_keys([h.media_key], True)))
        A["align_show"] = self._act("对齐选中的作品", self._align_selected_shows)
        A["align_all"] = self._act("对齐全部未对齐的集…", self._align_all)
        A["align_pause"] = self._act("暂停 / 继续", self._toggle_align)
        A["align_cancel"] = self._act("取消排队", self.cancel_queue)
        A["align_log"] = self._act("打开对齐日志", self._open_align_log)
        A["auto"] = self._act("入库后自动对齐", self._set_auto, checkable=True)
        for k in ("align_center", None, "align_cur", "align_show", "align_all", None, "align_pause", "align_cancel",
                  "align_log", None, "auto"):
            am.addSeparator() if k is None else am.addAction(A[k])
        # 窗口
        vm = m.addMenu("窗口")
        for d in (self.d_lib, self.d_align, self.d_prev, self.d_cut, self.d_ctx, self.d_pack, self.d_tasks):
            vm.addAction(d.toggleViewAction())
        vm.addSeparator()
        A["toolbar"] = self.toolbar_toggle = vm.addAction("工具栏")
        A["layout"] = self._act("恢复默认布局", self._default_layout, icon="layout")
        vm.addAction(A["layout"])
        # 帮助
        hm = m.addMenu("帮助")
        hm.addAction(self._act("使用说明（网页）", lambda: QDesktopServices.openUrl(
            QUrl("https://github.com/wimmekhi/clipper/blob/main/%E4%BD%BF%E7%94%A8%E8%AF%B4%E6%98%8E.md"))))
        A["update"] = self._act("检查更新 / 一键更新…", self._update_clicked, icon="update")
        hm.addAction(A["update"])
        hm.addAction(self._act(f"关于（版本 {VERSION}）", lambda: QMessageBox.about(
            self, APP, f"{APP} {VERSION}\nhttps://github.com/wimmekhi/clipper")))

        # 工具栏：图标 + 文字，按「媒体库 / 播放 / 片段 / 采集包 / 对齐」分组；右侧固定 更新 + 设置
        tb = QToolBar("工具栏", self); tb.setObjectName("toolbar_main")
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); tb.setIconSize(QSize(18, 18))
        tb.setMovable(False)
        A["prev"].setIconText("上一条"); A["next"].setIconText("下一条"); A["play"].setIconText("播放")
        A["collect"].setIconText("采集"); A["export_one"].setIconText("导出这条")
        A["root"].setIconText("媒体库"); A["rescan"].setIconText("扫描"); A["export_pack"].setIconText("导出包")
        A["new_pack"].setIconText("新建包"); A["align_center"].setIconText("对齐中心"); A["settings"].setIconText("设置")
        for k in ("root", "rescan", None, "prev", "play", "next", None, "save_cut", "collect", "export_one", None,
                  "new_pack", "export_pack", None, "align_center"):
            tb.addSeparator() if k is None else tb.addAction(A[k])
        sp = QWidget(); sp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(sp)
        # 一键更新按钮：平时是「检查更新」；查到新版就变成高亮的「更新到 x.y.z」，点一下直接下载替换
        self.upd_btn = QToolButton(); self.upd_btn.setObjectName("update")
        self.upd_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.upd_btn.setIcon(theme.icon("update")); self.upd_btn.setIconSize(QSize(16, 16))
        self.upd_btn.setText("检查更新"); self.upd_btn.setToolTip(f"当前版本 {VERSION}，点一下检查新版")
        self.upd_btn.setCursor(Qt.PointingHandCursor)
        self.upd_btn.clicked.connect(self._update_clicked)
        self._upd_result = None
        tb.addWidget(self.upd_btn)
        tb.addAction(A["settings"])
        self.addToolBar(Qt.TopToolBarArea, tb)
        self.toolbar = tb
        self.toolbar_toggle.setCheckable(True); self.toolbar_toggle.setChecked(True)
        self.toolbar_toggle.toggled.connect(tb.setVisible)
        self.ver_lb = QLabel(f"版本 {VERSION}"); self.ver_lb.setObjectName("small")
        self.statusBar().addPermanentWidget(self.ver_lb)
        self.statusBar().setSizeGripEnabled(False)
        if self._autostart:
            QTimer.singleShot(5000, lambda: self.check_update(manual=False))

    def _open_export_dir(self):
        if not self.lib:
            return
        d = Path(self._export_dir()); d.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _open_align_log(self):
        if not self.lib:
            return
        f = self.lib.data / "align.log"
        if not f.exists():
            f.write_text("", "utf-8")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(f)))

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
        if self.align_center:
            self.align_center.close(); self.align_center.deleteLater(); self.align_center = None
        self.lib = Library(root)
        self.searcher = Searcher(self.lib)
        self.qs.setValue("root", root)
        self.root_lb.setText(root)
        self.setWindowTitle(f"{APP} {VERSION} — {Path(root).name}")
        if self.qs.value("manual_align_105", "") != root:
            # 1.0.5 起默认手动选集对齐：老库第一次打开时关掉自动对齐
            self.lib.set_setting("auto_align", False)
            self.qs.setValue("manual_align_105", root)
        self.acts["auto"].blockSignals(True)
        self.acts["auto"].setChecked(self.lib_settings()["auto_align"])
        self.acts["auto"].blockSignals(False)
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
                    hf_mirror=g("hf_mirror", True), auto_align=bool(g("auto_align", False)))

    def start_align(self, force=False):
        if not self.lib:
            return
        if not force and not self.lib_settings()["auto_align"]:
            self.al_lb.setText("右键作品或到对齐中心选集")
            return
        if self.align_worker and self.align_worker.isRunning():
            self.align_worker.settings = self.lib_settings()
            self.align_worker.kick()
            return
        self.align_worker = AlignWorker(str(self.lib.root), self.lib_settings())
        self.align_worker.status.connect(self._align_status)
        self.align_worker.episode_done.connect(self._align_done)
        self.align_worker.idle.connect(self._align_idle)
        self.align_worker.crashed.connect(lambda m: self.al_lb.setText("对齐线程出错，打开「对齐中心」看详情"))
        self.align_worker.start()
        if self.align_center:
            self.align_center.attach(self.align_worker)

    def queue_keys(self, keys, redo=False):
        """手动对齐：把这些集排进队列并开跑。redo=True 连已完成的也重跑。"""
        if not self.lib or not keys:
            return 0
        db = self.lib.db
        cond = "align_status!='running'" if redo else "align_status IN ('none','error')"
        n = 0
        for k in keys:
            n += db.execute(f"UPDATE media SET align_status='queued', align_msg=NULL WHERE key=? AND {cond} "
                            "AND rel_video IS NOT NULL AND rel_sub IS NOT NULL", (k,)).rowcount
        db.commit()
        self.statusBar().showMessage(f"已排队 {n} 集" if n else "选中的都已对齐过（要重跑用对齐中心的「重新对齐」）", 4000)
        if n:
            w = self.align_worker
            if w and w.isRunning() and w.paused:
                w.pause(False); self.al_btn.setText("暂停")
            self.start_align(force=True)
            if self.align_center:
                self.align_center.reload()
        return n

    def show_keys(self, shows):
        q = ",".join("?" * len(shows))
        return [r[0] for r in self.lib.db.execute(f"SELECT key FROM media WHERE show IN ({q})", list(shows))]

    def _align_selected_shows(self):
        shows = [it.data(0, Qt.UserRole) for it in self.tree.selectedItems()]
        if not shows:
            QMessageBox.information(self, APP, "先在左边媒体库里选中作品（可多选），或打开对齐中心按集选。")
            return
        self.queue_keys(self.show_keys(shows))

    def _align_all(self):
        if not self.lib:
            return
        n = self.lib.db.execute("SELECT COUNT(*) FROM media WHERE align_status IN ('none','error') "
                                "AND rel_video IS NOT NULL AND rel_sub IS NOT NULL").fetchone()[0]
        if QMessageBox.question(self, APP, f"把 {n} 集未对齐的全部排队？整库会跑很久。") == QMessageBox.Yes:
            keys = [r[0] for r in self.lib.db.execute("SELECT key FROM media")]
            self.queue_keys(keys)

    def cancel_queue(self):
        if not self.lib:
            return
        n = self.lib.db.execute("UPDATE media SET align_status='none' WHERE align_status='queued'").rowcount
        self.lib.db.commit()
        self.statusBar().showMessage(f"已取消 {n} 集排队（正在跑的这集会跑完）", 4000)
        if self.align_center:
            self.align_center.reload()

    def _set_auto(self, on):
        if not self.lib:
            return
        self.lib.set_setting("auto_align", bool(on))
        if self.align_worker:
            self.align_worker.settings = self.lib_settings(); self.align_worker.kick()
        if on:
            self.start_align()

    def _tree_menu(self, pos):
        from PySide6.QtWidgets import QMenu
        it = self.tree.itemAt(pos)
        if it and not it.isSelected():
            self.tree.clearSelection(); it.setSelected(True)
        shows = [i.data(0, Qt.UserRole) for i in self.tree.selectedItems()]
        mnu = QMenu(self)
        if shows:
            label = shows[0] if len(shows) == 1 else f"{len(shows)} 部作品"
            mnu.addAction(f"对齐「{label}」未对齐的集", self._align_selected_shows)
            mnu.addAction(f"重新对齐「{label}」全部集", lambda: self.queue_keys(self.show_keys(shows), redo=True))
            mnu.addAction("只搜这部作品", lambda: self._tree_pick(it or self.tree.selectedItems()[0], 0))
            mnu.addSeparator()
        mnu.addAction("对齐中心…", self.open_align_center)
        mnu.exec(self.tree.viewport().mapToGlobal(pos))

    def open_align_center(self):
        if not self.lib:
            return
        from .aligncenter import AlignCenter
        if self.align_center is None:
            self.align_center = AlignCenter(self)
        else:
            self.align_center.fill_shows(); self.align_center.reload()
            self.align_center.attach(self.align_worker)
        self.align_center.show(); self.align_center.raise_(); self.align_center.activateWindow()

    def _align_status(self, key, f, text, done, todo):
        self.al_lb.setText(f"正在对齐：{key}\n{text}\n已完成 {done} 集，还剩 {todo} 集")
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
            vw = self._card_width()
            it.setSizeHint(QSize(vw, card.heightForWidth(vw) + 4))
            self.results.addItem(it)
            self.results.setItemWidget(it, card)
        self.shown = min(len(lst), self.shown + PAGE)

    def _card_width(self):
        return max(200, self.results.viewport().width() - 4)

    def _relayout_cards(self):
        vw = self._card_width()
        for i in range(self.results.count()):
            it = self.results.item(i)
            card = self.results.itemWidget(it)
            if card is not None:
                it.setSizeHint(QSize(vw, card.heightForWidth(vw) + 4))

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if obj is self.results.viewport() and ev.type() == QEvent.Resize:
            self._relayout_timer.start()
        return super().eventFilter(obj, ev)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        tb = getattr(self, "toolbar", None)
        if tb is not None:   # 窗口窄时工具栏只留图标，保证右上角的「更新」「设置」一直看得见
            style = Qt.ToolButtonTextBesideIcon if self.width() >= 1280 else Qt.ToolButtonIconOnly
            if tb.toolButtonStyle() != style:
                tb.setToolButtonStyle(style)

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
            self.acts["auto"].blockSignals(True)
            self.acts["auto"].setChecked(self.lib_settings()["auto_align"])
            self.acts["auto"].blockSignals(False)
            if self.align_worker:
                self.align_worker.settings = self.lib_settings()
            self.start_align()
            if self.cur:
                self.refresh_current()

    # ------------------------------------------------------------ 更新
    def check_update(self, manual: bool):
        if getattr(self, "_upd_check", None) and self._upd_check.isRunning():
            return
        self._upd_check = UpdateCheck()
        self._upd_check.done.connect(lambda r: self._update_checked(r, manual))
        self._upd_check.start()

    def _update_clicked(self):
        """工具栏的更新按钮：已经查到新版就直接下载替换（一键）；还没查过就先查。"""
        from .. import updater
        r = self._upd_result
        if r and r.get("url") and updater.is_newer(r.get("version", "")):
            if updater.current_target() is None:     # 源码运行：没有可替换的程序，去下载页
                QDesktopServices.openUrl(QUrl(r["page"]))
                return
            ok = QMessageBox.question(
                self, "一键更新", f"更新到 {r['version']}（约 {r.get('size', 0)/1e6:.0f} MB）？\n"
                "下载完软件会自动关闭、替换并重新打开；对齐结果和采集包都在媒体库里，不受影响。")
            if ok == QMessageBox.Yes:
                self._download_update(r)
            return
        self.check_update(manual=True)

    def _mark_update(self, r: dict | None):
        from .. import updater
        self._upd_result = r
        hot = bool(r and r.get("url") and updater.is_newer(r.get("version", "")))
        self.upd_btn.setProperty("hot", "true" if hot else "false")
        self.upd_btn.setIcon(theme.icon("update", "#FFFFFF" if hot else theme.ACCENT_D))
        if hot:
            self.upd_btn.setText(f"更新到 {r['version']}")
            self.upd_btn.setToolTip(f"有新版 {r['version']}（当前 {VERSION}），点一下下载并自动替换")
            self.statusBar().showMessage(f"有新版 {r['version']}，点右上角「更新到 {r['version']}」一键更新", 15000)
        else:
            self.upd_btn.setText("检查更新")
        self.upd_btn.style().unpolish(self.upd_btn); self.upd_btn.style().polish(self.upd_btn)

    def _update_checked(self, r: dict, manual: bool):
        from .. import updater
        if not r.get("error"):
            self._mark_update(r)
        if not manual:
            return      # 启动时自动查：只把更新按钮点亮，不弹窗打扰
        if r.get("error"):
            if manual:
                QMessageBox.warning(self, "检查更新", "连不上 GitHub：" + r["error"][:200])
            return
        ver = r.get("version", "")
        if not updater.is_newer(ver):
            if manual:
                QMessageBox.information(self, "检查更新", f"已经是最新版（{VERSION}）。")
            return
        if not r.get("url"):
            if manual:
                QMessageBox.information(self, "检查更新", f"有新版 {ver}，但还没有本平台的安装包。")
            return
        notes = (r.get("notes") or "").strip()
        box = QMessageBox(self)
        box.setWindowTitle("有新版本")
        box.setText(f"新版 {ver}（当前 {VERSION}），下载约 {r.get('size', 0)/1e6:.0f} MB。\n"
                    "更新时会先关闭软件，替换后自动重新打开。对齐结果、采集包都在媒体库里，不受影响。")
        if notes:
            box.setDetailedText(notes)
        b_now = box.addButton("一键更新", QMessageBox.AcceptRole)
        box.addButton("以后再说", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is b_now:
            if updater.current_target() is None:
                QDesktopServices.openUrl(QUrl(r["page"]))
                return
            self._download_update(r)

    def _download_update(self, r):
        from PySide6.QtWidgets import QProgressDialog
        dlg = QProgressDialog(f"正在下载 {r['version']}…", "取消", 0, 1000, self)
        dlg.setWindowTitle("一键更新"); dlg.setWindowModality(Qt.WindowModal); dlg.setMinimumDuration(0)
        dlg.setAutoClose(False); dlg.setAutoReset(False)
        w = self._upd_dl = UpdateDownload(r["url"], r.get("size", 0))

        def prog(g, t):
            dlg.setValue(int(g / t * 1000) if t else 0)
            dlg.setLabelText(f"正在下载 {r['version']}… {g/1e6:.0f} / {t/1e6:.0f} MB")

        def done(path, err):
            dlg.close()
            if err:
                if err != "已取消":
                    QMessageBox.warning(self, "一键更新", "下载失败：" + err[:300] + "\n\n可以到 Releases 页面手动下载。")
                return
            from .. import updater
            try:
                updater.prepare_and_launch(Path(path))
            except Exception as ex:  # noqa
                QMessageBox.warning(self, "一键更新", f"替换失败：{ex}\n\n新版下载在：{path}")
                return
            QMessageBox.information(self, "一键更新", "下载好了，软件会关闭并替换，几秒后自动打开新版。")
            self._updating = True
            self.close()
            QApplication.quit()
        w.progress.connect(prog)
        w.done.connect(done)
        dlg.canceled.connect(lambda: setattr(w, "cancel", True))
        w.start()

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


def light_palette(app):
    """固定浅色小清新主题（见 theme.py）。"""
    theme.apply_theme(app)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setOrganizationName("local33")
    light_palette(app)
    # 只允许开一个：防止任何意外重启把 Dock 撑满
    from PySide6.QtCore import QLockFile, QDir
    lock = QLockFile(QDir.tempPath() + "/clipper-local33.lock")
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, APP, "台词切片编辑器已经开着了。")
        sys.exit(0)
    app._single_lock = lock
    from PySide6.QtGui import QIcon
    if icon_path().exists():
        app.setWindowIcon(QIcon(str(icon_path())))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
