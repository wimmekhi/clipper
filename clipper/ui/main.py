"""主窗口（1.0.7）：结构参照 quipclipper —— 顶栏（品牌 + 带范围的搜索框 + 页面导航 + 更新 / 设置），
下面分页：媒体库 / 搜索 / 工作台 / 采集包 / 导出任务。页面里的各块用分隔条，拖动可调大小。

工作台 = 播放器 + 整集说话地图 + 可缩放波形（选区两端吸附词边界 / 字幕边 / 静音 / 过零点）+ 台词本（跟随播放）。
"""
from __future__ import annotations

import bisect
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote

from PySide6.QtCore import Qt, QSettings, QTimer, QUrl, QSize
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut, QDesktopServices, QColor, QFont
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaDevices
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
                               QPushButton, QComboBox, QLabel, QListWidget, QListWidgetItem, QTabBar,
                               QFileDialog, QMessageBox, QProgressBar, QSpinBox, QInputDialog, QTreeWidget,
                               QTreeWidgetItem, QSizePolicy, QFrame, QDoubleSpinBox, QAbstractItemView, QToolBar,
                               QToolButton, QStackedWidget, QSplitter, QCheckBox, QMenu, QHeaderView, QButtonGroup)

from .. import tools
from ..paths import VERSION, icon_path, ALIGN_UI
from ..library import Library, JOIN_BASE, AUTO_BASE
from ..search import Searcher, Hit, TIER_ORIG, TIER_NEAR, TIER_FUNC, _tier_rank
from ..export import Style, clip_name
from .widgets import (WaveformView, DialogueMap, GroupHeader, ResultRow, fmt_ms, fmt_short, highlight_html,
                      TIMING_TEXT, FlowLayout, tag, MONO, TagDelegate)
from .workers import ScanWorker, AlignWorker, ExportWorker, WaveWorker, UpdateCheck, UpdateDownload
from .dialogs import SettingsDialog, ExportDialog
from . import theme
from .theme import icon

APP = "台词切片编辑器"
PAGE = 80
P_LIB, P_SEARCH, P_WORK, P_PACK, P_TASKS = range(5)
PAGES = (("媒体库", "library"), ("搜索", "search"), ("工作台", "scissors"), ("采集包", "package"), ("导出任务", "export"))
LINK_RX = re.compile(r"^\s*clip://([^#\s]+)#(\d+)")


def panel(margins=(14, 12, 14, 12), spacing=8, obj="panel"):
    f = QFrame(); f.setObjectName(obj)
    v = QVBoxLayout(f); v.setContentsMargins(*margins); v.setSpacing(spacing)
    return f, v


def label(text, obj=None, wrap=False):
    lb = QLabel(text)
    if obj:
        lb.setObjectName(obj)
    lb.setWordWrap(wrap)
    return lb


def chip(text, on=False, tip=None):
    c = QPushButton(text); c.setObjectName("chip"); c.setCheckable(True); c.setChecked(on)
    c.setCursor(Qt.PointingHandCursor)
    if tip:
        c.setToolTip(tip)
    return c


def btn(text, ic=None, fn=None, obj=None, tip=None, white=False):
    b = QPushButton(text)
    if ic:
        b.setIcon(icon(ic, "#FFFFFF" if (obj == "primary" or white) else theme.ACCENT_D, 16))
    if obj:
        b.setObjectName(obj)
    if fn:
        b.clicked.connect(fn)
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    return b


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
        self.items = []            # 搜索结果展开后的行：("g", key, label, n) / ("r", hit)
        self.shown = 0
        self.rows = {}             # cue_id -> (QListWidgetItem, ResultRow)
        self.group_items = {}      # media_key -> [QListWidgetItem]
        self.collapsed = set()
        self.checked = {}          # cue_id -> hit
        self.cur = None            # 当前片段 hit
        self.cur_span = None
        self.ws_media = None       # 工作台当前这一集（media 行）
        self.script_rows = []
        self._script_starts = []
        self.align_worker = None
        self.align_center = None
        self.export_workers = []
        self._wave_token = 0
        self._wave_workers = []
        self._stop_at = None
        self._prev_page = P_SEARCH
        self.lib_show = ""
        self._build_topbar()
        self._build_pages()
        self._build_menu()
        self._build_shortcuts()
        geo = self.qs.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        for sp in self.findChildren(QSplitter):
            st = self.qs.value("split107/" + sp.objectName())
            if st is not None:
                sp.restoreState(st)
        self.goto(P_LIB)
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

    # ================================================================== 顶栏
    def _build_topbar(self):
        tb = QToolBar("顶栏", self); tb.setObjectName("toolbar_main")
        tb.setMovable(False); tb.setFloatable(False)
        tb.setStyleSheet(f"QToolBar#toolbar_main{{background:rgba(255,255,255,0.86);border:none;"
                         f"border-bottom:1px solid {theme.LINE};padding:8px 16px;spacing:6px;}}")
        tb.setIconSize(QSize(18, 18))
        self.brand_icon = QLabel()
        if icon_path().exists():
            from PySide6.QtGui import QPixmap
            self.brand_icon.setPixmap(QPixmap(str(icon_path())).scaled(28, 28, Qt.KeepAspectRatio,
                                                                       Qt.SmoothTransformation))
        tb.addWidget(self.brand_icon)
        self.brand = label(" 台词切片", "brand")
        tb.addWidget(self.brand)
        sp0 = QWidget(); sp0.setFixedWidth(10); tb.addWidget(sp0)
        # 搜索框：左边是范围（全部作品 / 某部作品），右边是 / 快捷键提示
        box = self.searchbox = QFrame(); box.setObjectName("searchbox")
        bh = QHBoxLayout(box); bh.setContentsMargins(5, 3, 8, 3); bh.setSpacing(6)
        self.f_show = QComboBox(); self.f_show.setObjectName("scope")
        self.f_show.setToolTip("搜索范围：全部作品，或只搜一部")
        self.f_show.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.f_show.addItem("全部作品", "")
        self.f_show.currentIndexChanged.connect(lambda _: self.do_search() if self.q.text().strip() else None)
        bh.addWidget(self.f_show)
        self.q = QLineEdit(); self.q.setObjectName("topsearch")
        self.q.setPlaceholderText("搜索台词或句型，例如 whose * is this")
        self.q.returnPressed.connect(self.do_search)
        self.q.setMinimumWidth(160)
        bh.addWidget(self.q, 1)
        kb = label("/", "kbd"); kb.setToolTip("按 / 跳到搜索框"); bh.addWidget(kb)
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        box.setMaximumWidth(680)
        tb.addWidget(box)
        sp1 = QWidget(); sp1.setFixedWidth(10); tb.addWidget(sp1)
        self.nav = []
        grp = QButtonGroup(self); grp.setExclusive(True)
        for i, (t, ic) in enumerate(PAGES):
            b = QToolButton(); b.setObjectName("nav"); b.setText(t); b.setCheckable(True)
            b.setIcon(icon(ic, theme.ACCENT_D, 16)); b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            b.setToolTip(f"{t}（Ctrl+{i + 1}）"); b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, i=i: self.goto(i))
            grp.addButton(b)
            tb.addWidget(b)
            self.nav.append(b)
        sp = QWidget(); sp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred); tb.addWidget(sp)
        self.top_align = QToolButton(); self.top_align.setObjectName("topbtn")
        self.top_align.setIcon(icon("wave", theme.ACCENT_D, 18)); self.top_align.setText("对齐中心")
        self.top_align.setToolButtonStyle(Qt.ToolButtonTextBesideIcon); self.top_align.setToolTip("对齐中心（Ctrl+L）")
        self.top_align.clicked.connect(self.open_align_center)
        if ALIGN_UI:
            tb.addWidget(self.top_align)
        # 一键更新：平时「检查更新」；查到新版变成高亮「更新到 x.y.z」，点一下直接下载替换
        self.upd_btn = QToolButton(); self.upd_btn.setObjectName("update")
        self.upd_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.upd_btn.setIcon(icon("update")); self.upd_btn.setIconSize(QSize(16, 16))
        self.upd_btn.setText("检查更新"); self.upd_btn.setToolTip(f"当前版本 {VERSION}，点一下检查新版")
        self.upd_btn.setCursor(Qt.PointingHandCursor)
        self.upd_btn.clicked.connect(self._update_clicked)
        self._upd_result = None
        tb.addWidget(self.upd_btn)
        self.top_set = QToolButton(); self.top_set.setObjectName("topbtn")
        self.top_set.setIcon(icon("settings", theme.ACCENT_D, 18)); self.top_set.setToolTip("设置（Ctrl+,）")
        self.top_set.clicked.connect(self.open_settings)
        tb.addWidget(self.top_set)
        self.addToolBar(Qt.TopToolBarArea, tb)
        self.toolbar = tb

    def goto(self, i):
        if self.stack.currentIndex() != i and self.stack.currentIndex() in (P_LIB, P_SEARCH, P_PACK):
            self._prev_page = self.stack.currentIndex()
        self.stack.setCurrentIndex(i)
        self.nav[i].setChecked(True)
        # 同一个播放器：在哪页就把画面接到哪页
        self.player.setVideoOutput(self.video if i == P_WORK else self.video_search)
        if i == P_LIB:
            self.refresh_lib_page()

    # ================================================================== 页面
    def _build_pages(self):
        self.stack = QStackedWidget()
        self.player = QMediaPlayer(self); self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        # 1.2.0：中途连上 / 断开 AirPods 等，跟着系统当前的默认输出走（以前只认启动时那个设备）
        self.media_devices = QMediaDevices(self)
        self.media_devices.audioOutputsChanged.connect(lambda: QTimer.singleShot(300, self._sync_audio_device))
        self.player.positionChanged.connect(self._on_pos)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.stack.addWidget(self._page_library())
        self.stack.addWidget(self._page_search())
        self.stack.addWidget(self._page_work())
        self.stack.addWidget(self._page_packs())
        self.stack.addWidget(self._page_tasks())
        wrap = QWidget(); v = QVBoxLayout(wrap); v.setContentsMargins(16, 14, 16, 8); v.addWidget(self.stack)
        self.setCentralWidget(wrap)

    # ------------------------------------------------------------ 媒体库页
    def _page_library(self):
        sp = QSplitter(Qt.Horizontal); sp.setObjectName("sp_lib"); sp.setChildrenCollapsible(False)
        side = QFrame(); side.setObjectName("sidebar")
        sv = QVBoxLayout(side); sv.setContentsMargins(4, 4, 10, 4); sv.setSpacing(8)
        sv.addWidget(label("媒体库", "eyebrow"))
        self.tree = QTreeWidget(); self.tree.setObjectName("shows")
        self.tree.setHeaderHidden(True); self.tree.setColumnCount(2); self.tree.setRootIsDecorated(False)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.tree.itemSelectionChanged.connect(self._show_picked)
        self.tree.itemDoubleClicked.connect(self._tree_pick)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.tree.setToolTip("单击看这部的集；双击只搜这部" + ("；右键对齐" if ALIGN_UI else ""))
        sv.addWidget(self.tree, 1)
        self.root_lb = label("未选择媒体库", "small", True)
        sv.addWidget(self.root_lb)
        hb = QHBoxLayout(); hb.setSpacing(6)
        hb.addWidget(btn("选择", "folder", self.choose_root, tip="选择媒体库文件夹（Ctrl+O）"))
        hb.addWidget(btn("扫描", "refresh", self.rescan, tip="重新扫描（F5）"))
        hb.addStretch(1)
        sv.addLayout(hb)
        side.setMinimumWidth(170)
        sp.addWidget(side)

        main = QWidget(); mv = QVBoxLayout(main); mv.setContentsMargins(10, 0, 0, 0); mv.setSpacing(10)
        self.crumb = label("媒体库", "crumb"); self.crumb.setTextFormat(Qt.RichText)
        self.crumb.linkActivated.connect(lambda _: self.tree.setCurrentItem(self.tree.topLevelItem(0)))
        mv.addWidget(self.crumb)
        self.ep_filter = QLineEdit(); self.ep_filter.setPlaceholderText("筛选集…（集号、文件名）")
        self.ep_filter.textChanged.connect(self._filter_eps)
        mv.addWidget(self.ep_filter)
        # 字幕换过的提示条
        self.banner = QFrame(); self.banner.setObjectName("banner")
        bl = QHBoxLayout(self.banner); bl.setContentsMargins(16, 10, 12, 10)
        self.banner_lb = label("", wrap=True); bl.addWidget(self.banner_lb, 1)
        self.banner_btn = btn("重新对齐它们", "refresh", self._realign_changed)
        bl.addWidget(self.banner_btn)
        self.banner.hide()
        mv.addWidget(self.banner)
        # 对齐卡片
        card, cv = panel()
        top = QHBoxLayout(); top.setSpacing(8)
        tv = QVBoxLayout(); tv.setSpacing(3)
        tv.addWidget(label("整集对齐", "h2"))
        self.al_sum = label("", "muted", True); tv.addWidget(self.al_sum)
        top.addLayout(tv, 1)
        self.al_center_btn = btn("对齐中心", "wave", self.open_align_center, "primary")
        self.al_show_btn = btn("对齐这部作品", None, self._align_selected_shows,
                               tip="把选中作品里还没对齐的集排队")
        self.al_btn = btn("暂停", None, self._toggle_align)
        for b in (self.al_show_btn, self.al_btn, self.al_center_btn):
            top.addWidget(b, 0, Qt.AlignTop)
        cv.addLayout(top)
        self.al_bar = QProgressBar(); self.al_bar.setRange(0, 1000); self.al_bar.setTextVisible(False)
        self.al_bar.setFixedHeight(6)
        cv.addWidget(self.al_bar)
        self.al_lb = label("右键作品或到对齐中心选集", "small", True)
        cv.addWidget(self.al_lb)
        self.al_log = QListWidget(); self.al_log.setMaximumHeight(54)
        self.al_log.setStyleSheet(f"font-size:11px;color:{theme.TEXT2};")
        self.al_log.hide()
        cv.addWidget(self.al_log)
        mv.addWidget(card)
        if not ALIGN_UI:
            card.hide()
            mv.insertWidget(mv.indexOf(card), self.al_lb)
            self.al_lb.setText("")
        # 集列表
        self.eps = QTreeWidget(); self.eps.setObjectName("episodes")
        self.eps.setHeaderLabels(["集", "文件", "台词", "状态"])
        self.eps.setRootIsDecorated(False); self.eps.setAlternatingRowColors(False)
        h = self.eps.header(); h.setStretchLastSection(False)
        # 1.0.8：列宽固定 / 拉伸，不再 ResizeToContents（上千行时每加一行都要重算）
        h.setSectionResizeMode(0, QHeaderView.Interactive)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.Interactive)
        h.setSectionResizeMode(3, QHeaderView.Fixed)
        self.eps.setColumnWidth(0, 200); self.eps.setColumnWidth(2, 110); self.eps.setColumnWidth(3, 170)
        self.eps.setUniformRowHeights(True)
        self.eps.setItemDelegateForColumn(3, TagDelegate(self.eps))
        self._ep_items = {}
        self.eps.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.eps.itemDoubleClicked.connect(lambda it, _c: self.open_episode(it.data(0, Qt.UserRole)))
        self.eps.setContextMenuPolicy(Qt.CustomContextMenu)
        self.eps.customContextMenuRequested.connect(self._eps_menu)
        self.eps.setToolTip("双击在工作台打开" + ("；右键对齐" if ALIGN_UI else ""))
        epw, epv = panel((6, 6, 6, 6))
        epv.addWidget(self.eps)
        mv.addWidget(epw, 1)
        sp.addWidget(main)
        sp.setStretchFactor(1, 1)
        sp.setSizes([240, 1100])
        return sp

    # ------------------------------------------------------------ 搜索页
    def _page_search(self):
        page = QWidget(); v = QVBoxLayout(page); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)
        opts = FlowLayout(spacing=6)
        self.c_exact = chip("精确", tip="不做词形变化（loved 不算 love）")
        self.c_order = chip("严格顺序", tip="词必须按顺序连在一起；* 或 ... 表示中间可以有几个词")
        self.c_single = chip("只看单人", True)
        self.c_video = chip("只看有视频")
        self.c_machine = chip("含识别台词", tip="把对齐时语音识别出来、字幕里没有的话也搜进来（紫色）")
        for c in (self.c_exact, self.c_order, self.c_single, self.c_video, self.c_machine):
            c.toggled.connect(lambda _=False: self.do_search() if self.q.text().strip() else None)
            opts.addWidget(c)
        # 1.2.0：排序选项（只影响排列，不用重新搜）；记在本机
        self.c_ted = chip("TED优先", str(self.qs.value("sort_ted", "false")).lower() == "true",
                          "TED 演讲的结果排在最前面")
        self.c_rank = chip("只按匹配度", str(self.qs.value("sort_rank", "true")).lower() != "false",
                           "所有结果按匹配度从高到低排一列；关掉则按集分组、原句型在前")
        for c, k in ((self.c_ted, "sort_ted"), (self.c_rank, "sort_rank")):
            c.toggled.connect(lambda on, k=k: (self.qs.setValue(k, "true" if on else "false"), self.render_results()))
            opts.addWidget(c)

        def pair(text, w):
            box = QWidget(); hl = QHBoxLayout(box); hl.setContentsMargins(6, 0, 0, 0); hl.setSpacing(4)
            hl.addWidget(label(text, "muted")); hl.addWidget(w)
            opts.addWidget(box)
        self.f_season = QSpinBox(); self.f_season.setRange(0, 99); self.f_season.setSpecialValueText("全部")
        self.f_season.valueChanged.connect(lambda _: self.do_search() if self.q.text().strip() else None)
        pair("季", self.f_season)
        self.f_maxdur = QDoubleSpinBox(); self.f_maxdur.setRange(0, 60); self.f_maxdur.setValue(8)
        self.f_maxdur.setSuffix(" 秒"); self.f_maxdur.setSpecialValueText("不限")
        pair("最长", self.f_maxdur)
        v.addLayout(opts)
        th = QHBoxLayout(); th.setSpacing(8)
        self.tabs = QTabBar()
        for t in ("全部", TIER_ORIG, TIER_NEAR, TIER_FUNC):
            self.tabs.addTab(t)
        self.tabs.setDrawBase(False); self.tabs.setExpanding(False)
        self.tabs.currentChanged.connect(lambda _: self.render_results())
        th.addWidget(self.tabs)
        self.count_lb = label("输入台词或句型后回车", "muted")
        th.addWidget(self.count_lb, 1)
        v.addLayout(th)

        sp = QSplitter(Qt.Horizontal); sp.setObjectName("sp_search"); sp.setChildrenCollapsible(False)
        left = QWidget(); lv = QVBoxLayout(left); lv.setContentsMargins(0, 0, 0, 0); lv.setSpacing(8)
        self.results = QListWidget(); self.results.setObjectName("results")
        self.results.setSpacing(3)
        self.results.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.results.verticalScrollBar().valueChanged.connect(self._maybe_more)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.results.setSelectionMode(QAbstractItemView.NoSelection)
        self.results.setMinimumWidth(380)
        self._relayout_timer = QTimer(self, singleShot=True, interval=60, timeout=self._relayout_rows)
        self.results.viewport().installEventFilter(self)
        lv.addWidget(self.results, 1)
        # 底部批量操作条
        bar = QFrame(); bar.setObjectName("batchbar")
        bh = QHBoxLayout(bar); bh.setContentsMargins(14, 8, 10, 8); bh.setSpacing(8)
        self.c_all = QCheckBox("全选"); self.c_all.toggled.connect(self._check_all)
        bh.addWidget(self.c_all)
        self.sel_lb = label("勾选几条可以一起采集 / 导出", "muted"); bh.addWidget(self.sel_lb, 1)
        self.pack_btn_s = self._pack_button()
        bh.addWidget(self.pack_btn_s)
        self.b_bcollect = btn("采集选中", "star", self._batch_collect)
        self.b_bexport = btn("导出选中", "scissors", self._batch_export, "primary")
        bh.addWidget(self.b_bcollect); bh.addWidget(self.b_bexport)
        lv.addWidget(bar)
        sp.addWidget(left)
        # 右侧预览
        pv_w, pv = panel((14, 12, 14, 12), 6)
        pv.addWidget(label("预览", "eyebrow"))
        self.pv_title = label("", "h2", True); pv.addWidget(self.pv_title)
        self.pv_time = label("", "mono"); pv.addWidget(self.pv_time)
        vf = QFrame(); vf.setObjectName("videoFrame")
        vfl = QVBoxLayout(vf); vfl.setContentsMargins(4, 4, 4, 4)
        self.video_search = QVideoWidget(); self.video_search.setMinimumSize(160, 96)
        self.video_search.setStyleSheet("background:#0F172A;")
        vfl.addWidget(self.video_search)
        pv.addWidget(vf, 3)
        self.pv_en = label("", wrap=True); self.pv_en.setStyleSheet("font-size:15px;font-weight:600;")
        self.pv_zh = label("", "muted", True)
        pv.addWidget(self.pv_en); pv.addWidget(self.pv_zh)
        prow = QHBoxLayout(); prow.setSpacing(6)
        self.b_play = btn("播放本句", "play", self.play_clip, "primary", "Ctrl+Enter")
        prow.addWidget(self.b_play)
        self.speed = QComboBox()
        for s in ("0.5", "0.75", "1.0", "1.25"):
            self.speed.addItem(s + "×", float(s))
        self.speed.setCurrentIndex(2)
        self.speed.currentIndexChanged.connect(lambda _: self.player.setPlaybackRate(self.speed.currentData()))
        self.speed.setToolTip("倍速")
        prow.addWidget(self.speed)
        self.c_loop = QPushButton(icon("loop", theme.ACCENT_D, 16), "连播"); self.c_loop.setObjectName("chip")
        self.c_loop.setCheckable(True); self.c_loop.setToolTip("播完自动下一条")
        prow.addWidget(self.c_loop)
        prow.addStretch(1)
        pv.addLayout(prow)
        pv.addWidget(label("上下文", "eyebrow"))
        self.ctx = QListWidget(); self.ctx.setObjectName("script"); self.ctx.setWordWrap(True)
        self.ctx.itemDoubleClicked.connect(self._ctx_pick)
        self.ctx.setToolTip("双击播放这一句")
        pv.addWidget(self.ctx, 2)
        pv.addWidget(btn("在工作台打开", "scissors", lambda: self.cur and self.open_workspace(self.cur),
                         tip="精修切点、看整集台词本"))
        pv_w.setMinimumWidth(280)
        sp.addWidget(pv_w)
        sp.setStretchFactor(0, 3); sp.setStretchFactor(1, 2)
        sp.setSizes([820, 480])
        v.addWidget(sp, 1)
        return page

    # ------------------------------------------------------------ 工作台
    def _page_work(self):
        page = QWidget(); v = QVBoxLayout(page); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)
        hd = QHBoxLayout(); hd.setSpacing(12)
        hd.addWidget(btn("返回", "back", lambda: self.goto(self._prev_page), tip="回到上一页"), 0, Qt.AlignTop)
        tv = QVBoxLayout(); tv.setSpacing(2)
        self.ws_title = label("还没打开片段", "h1"); tv.addWidget(self.ws_title)
        self.ws_sub = label("在搜索结果里双击一条，或在媒体库里双击一集", "muted")
        self.ws_sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tv.addWidget(self.ws_sub)
        hd.addLayout(tv, 1)
        self.pack_btn_w = self._pack_button()
        hd.addWidget(self.pack_btn_w, 0, Qt.AlignVCenter)
        hd.addWidget(btn("采集", "star", lambda: self._cur_do(self.collect), tip="采集到当前包（Ctrl+D）"), 0,
                     Qt.AlignVCenter)
        hd.addWidget(btn("复制链接", "link", lambda: self._cur_do(self.copy_link)), 0, Qt.AlignVCenter)
        hd.addWidget(btn("导出这条", "scissors", lambda: self._cur_do(self.export_one), "primary",
                         "Ctrl+E"), 0, Qt.AlignVCenter)
        v.addLayout(hd)

        sp = QSplitter(Qt.Horizontal); sp.setObjectName("sp_work"); sp.setChildrenCollapsible(False)
        left = QSplitter(Qt.Vertical); left.setObjectName("sp_work_left"); left.setChildrenCollapsible(False)
        # 播放器
        pw, pv = panel((10, 10, 10, 10), 6)
        vf = QFrame(); vf.setObjectName("videoFrame")
        vfl = QVBoxLayout(vf); vfl.setContentsMargins(4, 4, 4, 4)
        self.video = QVideoWidget(); self.video.setMinimumSize(200, 112)
        self.video.setStyleSheet("background:#0F172A;")
        vfl.addWidget(self.video)
        pv.addWidget(vf, 1)
        self.sub_en = label("", wrap=True); self.sub_en.setAlignment(Qt.AlignCenter)
        self.sub_en.setStyleSheet(f"font-size:16px;font-weight:600;color:{theme.TEXT};")
        self.sub_zh = label("", wrap=True); self.sub_zh.setAlignment(Qt.AlignCenter)
        self.sub_zh.setStyleSheet(f"font-size:13px;color:{theme.TEXT2};")
        pv.addWidget(self.sub_en); pv.addWidget(self.sub_zh)
        ctl = QHBoxLayout(); ctl.setSpacing(6)
        ctl.addStretch(1)
        ctl.addWidget(btn("", "prev", lambda: self.step(-1), tip="上一条结果（Ctrl+↑）"))
        self.b_play_w = btn("播放 / 暂停", "play", self.toggle_play, "primary", "空格")
        ctl.addWidget(self.b_play_w)
        ctl.addWidget(btn("", "next", lambda: self.step(1), tip="下一条结果（Ctrl+↓）"))
        ctl.addStretch(1)
        pv.addLayout(ctl)
        left.addWidget(pw)
        # 切点编辑
        ew, ev = panel((12, 10, 12, 10), 7)
        self.dmap = DialogueMap(); self.dmap.seek.connect(self._map_seek)
        self.dmap.setToolTip("整集的说话分布（紫色是识别补出的）；点哪儿跳到哪儿")
        ev.addWidget(self.dmap)
        r1 = FlowLayout(spacing=6)
        for ic, tip, fn in (("minus", "缩小（Ctrl+滚轮）", lambda: self.wave.zoom(1.5)),
                            ("plus", "放大（Ctrl+滚轮）", lambda: self.wave.zoom(1 / 1.5))):
            b = btn("", ic, fn, tip=tip); b.setFixedWidth(36); r1.addWidget(b)
        r1.addWidget(btn("适应选区", "fit", lambda: self.wave.fit_selection()))
        r1.addWidget(btn("整段", None, lambda: self.wave.fit_whole(), tip="显示加载的整段（前后各 4 秒）"))
        self.zoom_lb = label("", "mono"); r1.addWidget(self.zoom_lb)
        r1.addWidget(label("　吸附", "eyebrow"))
        snaps = (self.qs.value("snap107", "word,silence,zero") or "").split(",")
        self.snap_chips = {}
        for k, t, tip in (("word", "词边界", "识别出来的每个词的开头 / 结尾"),
                          ("cue", "字幕边", "台词条（上面一行）的两端"),
                          ("silence", "静音", "附近 ±0.12 秒里最安静的位置：不切在说话中间、不切爆音"),
                          ("zero", "过零点", "最后对齐到波形穿过 0 的那一点，切口不会「咔」一声")):
            c = chip(t, k in snaps, tip + "；按住 Alt / Option 拖动时临时不吸附")
            c.toggled.connect(self._snap_changed)
            self.snap_chips[k] = c
            r1.addWidget(c)
        ev.addLayout(r1)
        r2 = FlowLayout(spacing=6)
        r2.addWidget(btn("选区", "play", self.play_clip, tip="播放整个选区（Enter）"))
        r2.addWidget(btn("入点后 1 秒", "play", self.audition_in, tip="只听开头 1 秒（[）"))
        r2.addWidget(btn("出点前 1 秒", "play", self.audition_out, tip="只听结尾 1 秒（]）"))
        r2.addWidget(btn("选到虚线", "fit", self.select_sub,
                         tip="选区一键定到两条虚线之间（原字幕时间）（U）"))
        r2.addWidget(btn("保存微调", "bookmark", self.save_cut, tip="Ctrl+S；重新对齐也不会覆盖"))
        r2.addWidget(btn("恢复自动", None, self.reset_cut, "ghost"))
        ev.addLayout(r2)
        self.wave = WaveformView()
        self.wave.changed.connect(self._cut_changed)
        self.wave.seek.connect(lambda t: (self._play_from(t)))
        self.wave.pick_line.connect(lambda idx, ext: self.pick_cue(idx, ext))
        self.wave.view_changed.connect(self._view_changed)
        self.wave.snap = set(k for k in snaps if k)
        ev.addWidget(self.wave, 1)
        self.cut_lb = label("", "small", True); ev.addWidget(self.cut_lb)
        hint = label("拖两端改切点，空白处拖出新选区；单击从那里播放；点上面的台词条选中那句，Shift 点加选；"
                     "滚轮平移，Ctrl/⌘+滚轮缩放。按 I / O 把入点 / 出点设到播放头。", "small", True)
        ev.addWidget(hint)
        left.addWidget(ew)
        left.setSizes([420, 380])
        sp.addWidget(left)
        # 台词本
        sw, sv = panel((10, 10, 10, 10), 6)
        sh = QHBoxLayout()
        sh.addWidget(label("台词本", "h2"), 1)
        self.c_follow = chip("跟随播放", True, "播放时自动滚到正在说的那句")
        sh.addWidget(self.c_follow)
        sv.addLayout(sh)
        self.script = QListWidget(); self.script.setObjectName("script"); self.script.setWordWrap(True)
        self.script.setUniformItemSizes(False)
        self.script.itemClicked.connect(lambda it: self.pick_cue(it.data(Qt.UserRole),
                                                                 bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)))
        self.script.setToolTip("点一句选中并播放；Shift 点把选区扩到这句；紫色斜体是识别补出的")
        sv.addWidget(self.script, 1)
        sw.setMinimumWidth(260)
        sp.addWidget(sw)
        sp.setStretchFactor(0, 3); sp.setStretchFactor(1, 2)
        sp.setSizes([860, 460])
        v.addWidget(sp, 1)
        return page

    # ------------------------------------------------------------ 采集包页
    def _page_packs(self):
        page = QWidget(); v = QVBoxLayout(page); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)
        hd = QHBoxLayout(); hd.setSpacing(8)
        hd.addWidget(label("采集包", "h1"))
        self.pack_cb = QComboBox(); self.pack_cb.setMinimumWidth(220)
        self.pack_cb.currentIndexChanged.connect(lambda _: (self.load_pack(), self._sync_pack_buttons()))
        hd.addWidget(self.pack_cb)
        hd.addWidget(btn("新建", "newpack", self.new_pack, tip="Ctrl+N"))
        hd.addStretch(1)
        for t, fn in (("上移", lambda: self._pack_move(-1)), ("下移", lambda: self._pack_move(1)),
                      ("移除", self._pack_remove)):
            hd.addWidget(btn(t, None, fn, "ghost"))
        hd.addWidget(btn("导出此包", "package", self.export_pack, "primary", "Ctrl+Shift+E"))
        v.addLayout(hd)
        v.addWidget(label("包里的片段按顺序导出，可拼成合集长视频。双击一条在工作台打开。", "muted"))
        w, pv = panel((6, 6, 6, 6))
        self.pack_list = QListWidget(); self.pack_list.setObjectName("script"); self.pack_list.setWordWrap(True)
        self.pack_list.itemDoubleClicked.connect(self._pack_pick)
        pv.addWidget(self.pack_list)
        v.addWidget(w, 1)
        return page

    def _page_tasks(self):
        page = QWidget(); v = QVBoxLayout(page); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(10)
        hd = QHBoxLayout()
        hd.addWidget(label("导出任务", "h1"), 1)
        hd.addWidget(btn("打开导出文件夹", "folder", self._open_export_dir))
        v.addLayout(hd)
        w, pv = panel((6, 6, 6, 6))
        self.tasks = QListWidget(); self.tasks.setObjectName("script"); self.tasks.setWordWrap(True)
        self.tasks.itemDoubleClicked.connect(self._open_task)
        pv.addWidget(self.tasks)
        v.addWidget(w, 1)
        v.addWidget(label("双击一条打开它的文件夹。", "muted"))
        return page

    def _pack_button(self):
        b = QToolButton(); b.setObjectName("topbtn"); b.setPopupMode(QToolButton.InstantPopup)
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        b.setIcon(icon("package", theme.ACCENT_D, 16)); b.setText("采集到：—")
        b.setStyleSheet("QToolButton::menu-indicator{image:none;width:0;}")
        b.setToolTip("采集到哪个包")
        m = QMenu(b)
        m.aboutToShow.connect(lambda m=m: self._fill_pack_menu(m))
        b.setMenu(m)
        return b

    def _fill_pack_menu(self, m):
        m.clear()
        for i in range(self.pack_cb.count()):
            a = m.addAction(self.pack_cb.itemText(i))
            a.setCheckable(True); a.setChecked(i == self.pack_cb.currentIndex())
            a.triggered.connect(lambda _=False, i=i: self.pack_cb.setCurrentIndex(i))
        m.addSeparator()
        m.addAction("新建采集包…", self.new_pack)

    def _sync_pack_buttons(self):
        name = self.pack_cb.currentText() or "—"
        short = name if len(name) <= 16 else name[:15] + "…"
        for b in (getattr(self, "pack_btn_s", None), getattr(self, "pack_btn_w", None)):
            if b is not None:
                b.setText(f"采集到：{short}"); b.setToolTip(f"当前采集包：{name}（点一下换）")

    # ================================================================== 菜单 / 快捷键
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
        a.setMenuRole(QAction.NoRole)   # Mac 上不要被挪进「应用程序」菜单
        a.triggered.connect(fn)
        return a

    def _cur_do(self, fn):
        if self.cur:
            fn(self.cur)
        else:
            self.statusBar().showMessage("先选一条片段（搜索结果里点一下）", 2500)

    def _build_menu(self):
        m = self.menuBar()
        A = self.acts = {}
        fm = m.addMenu("文件")
        A["root"] = self._act("选择媒体库文件夹…", self.choose_root, "Ctrl+O", icon="folder")
        A["rescan"] = self._act("重新扫描媒体库", self.rescan, "F5", icon="refresh")
        A["open_root"] = self._act("在文件夹中打开媒体库", lambda: self.lib and QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.lib.root))))
        A["open_exp"] = self._act("打开导出文件夹", self._open_export_dir)
        A["settings"] = self._act("设置…", self.open_settings, "Ctrl+,", "常规 / 字幕样式" if not ALIGN_UI else "常规 / 对齐 / 字幕样式", icon="settings")
        A["quit"] = self._act("退出", self.close, "Ctrl+Q")
        for k in ("root", "rescan", "open_root", "open_exp", None, "settings", None, "quit"):
            fm.addSeparator() if k is None else fm.addAction(A[k])
        sm = m.addMenu("搜索")
        A["find"] = self._act("搜索框", self.focus_search, "Ctrl+F")
        A["search"] = self._act("执行搜索", self.do_search, icon="search")
        sm.addAction(A["find"]); sm.addAction(A["search"]); sm.addSeparator()
        for c in (self.c_exact, self.c_order, self.c_single, self.c_video, self.c_machine):
            a = QAction(c.text(), self, checkable=True); a.setChecked(c.isChecked())
            a.toggled.connect(c.setChecked); c.toggled.connect(a.setChecked)
            sm.addAction(a)
        sm.addSeparator()
        for i, t in enumerate(("全部", TIER_ORIG, TIER_NEAR, TIER_FUNC)):
            sm.addAction(self._act(f"只看：{t}", lambda _=False, i=i: self.tabs.setCurrentIndex(i)))
        cm = m.addMenu("片段")
        A["play"] = self._act("播放本句", self.play_clip, "Ctrl+Return", icon="play")
        A["prev"] = self._act("上一条", lambda: self.step(-1), "Ctrl+Up", icon="prev")
        A["next"] = self._act("下一条", lambda: self.step(1), "Ctrl+Down", icon="next")
        A["loop"] = self._act("连播", lambda on: self.c_loop.setChecked(on), checkable=True)
        self.c_loop.toggled.connect(A["loop"].setChecked)
        A["work"] = self._act("在工作台打开", lambda: self._cur_do(self.open_workspace))
        A["save_cut"] = self._act("保存微调", self.save_cut, "Ctrl+S", icon="bookmark")
        A["reset_cut"] = self._act("恢复自动切点", self.reset_cut)
        A["aud_in"] = self._act("试听开头 1 秒", self.audition_in)
        A["aud_out"] = self._act("试听结尾 1 秒", self.audition_out)
        A["collect"] = self._act("采集到当前包", lambda: self._cur_do(self.collect), "Ctrl+D", icon="collect")
        A["export_one"] = self._act("导出这一条…", lambda: self._cur_do(self.export_one), "Ctrl+E", icon="export")
        A["link"] = self._act("复制片段链接", lambda: self._cur_do(self.copy_link), "Ctrl+Shift+C", icon="link")
        for k in ("play", "prev", "next", "loop", None, "work", "save_cut", "reset_cut", "aud_in", "aud_out", None,
                  "collect", "export_one", "link"):
            cm.addSeparator() if k is None else cm.addAction(A[k])
        spm = cm.addMenu("倍速")
        g = QActionGroup(self)
        for i in range(self.speed.count()):
            a = QAction(self.speed.itemText(i), self, checkable=True); g.addAction(a); spm.addAction(a)
            a.setChecked(i == self.speed.currentIndex())
            a.triggered.connect(lambda _=False, i=i: self.speed.setCurrentIndex(i))
        pm = m.addMenu("采集包")
        A["new_pack"] = self._act("新建采集包…", self.new_pack, "Ctrl+N", icon="newpack")
        A["pack_up"] = self._act("选中条目上移", lambda: self._pack_move(-1))
        A["pack_down"] = self._act("选中条目下移", lambda: self._pack_move(1))
        A["pack_rm"] = self._act("移除选中条目", self._pack_remove)
        A["export_pack"] = self._act("导出此包…", self.export_pack, "Ctrl+Shift+E", icon="package")
        for k in ("new_pack", None, "pack_up", "pack_down", "pack_rm", None, "export_pack"):
            pm.addSeparator() if k is None else pm.addAction(A[k])
        am = m.addMenu("对齐")
        A["align_center"] = self._act("对齐中心…", self.open_align_center, "Ctrl+L", "选集对齐、看进度和失败原因",
                                      icon="wave")
        A["align_cur"] = self._act("对齐当前这一集", lambda: self._cur_do(lambda h: self.queue_keys([h.media_key], True)))
        A["align_show"] = self._act("对齐选中的作品", self._align_selected_shows)
        A["align_all"] = self._act("对齐全部未对齐的集…", self._align_all)
        A["align_changed"] = self._act("重新对齐换过字幕的集", self._realign_changed)
        A["align_pause"] = self._act("暂停 / 继续", self._toggle_align)
        A["align_cancel"] = self._act("取消排队", self.cancel_queue)
        A["align_log"] = self._act("打开对齐日志", self._open_align_log)
        A["auto"] = self._act("入库后自动对齐", self._set_auto, checkable=True)
        for k in ("align_center", None, "align_cur", "align_show", "align_all", "align_changed", None, "align_pause",
                  "align_cancel", "align_log", None, "auto"):
            am.addSeparator() if k is None else am.addAction(A[k])
        if not ALIGN_UI:
            am.menuAction().setVisible(False)
            for k in ("align_center", "align_cur", "align_show", "align_all", "align_changed", "align_pause",
                      "align_cancel", "align_log", "auto"):
                A[k].setEnabled(False)
        vm = m.addMenu("窗口")
        for i, (t, ic) in enumerate(PAGES):
            vm.addAction(self._act(t, lambda _=False, i=i: self.goto(i), f"Ctrl+{i + 1}", icon=ic))
        vm.addSeparator()
        A["toolbar"] = self.toolbar_toggle = vm.addAction("顶栏")
        self.toolbar_toggle.setCheckable(True); self.toolbar_toggle.setChecked(True)
        self.toolbar_toggle.toggled.connect(self.toolbar.setVisible)
        A["layout"] = self._act("恢复默认布局", self._default_layout, icon="layout")
        vm.addAction(A["layout"])
        hm = m.addMenu("帮助")
        hm.addAction(self._act("使用说明（网页）", lambda: QDesktopServices.openUrl(
            QUrl("https://github.com/wimmekhi/clipper/blob/main/%E4%BD%BF%E7%94%A8%E8%AF%B4%E6%98%8E.md"))))
        hm.addAction(self._act("快捷键", self._show_keys))
        A["update"] = self._act("检查更新 / 一键更新…", self._update_clicked, icon="update")
        hm.addAction(A["update"])
        hm.addAction(self._act(f"关于（版本 {VERSION}）", lambda: QMessageBox.about(
            self, APP, f"{APP} {VERSION}\nhttps://github.com/wimmekhi/clipper")))
        self.ver_lb = label(f"版本 {VERSION}", "small")
        self.statusBar().addPermanentWidget(self.ver_lb)
        self.statusBar().setSizeGripEnabled(False)
        if self._autostart:
            QTimer.singleShot(5000, lambda: self.check_update(manual=False))

    def _build_shortcuts(self):
        def sc(key, fn, work_only=False):
            s = QShortcut(QKeySequence(key), self)
            s.setContext(Qt.WindowShortcut)
            s.activated.connect(lambda: (fn() if (not work_only or self.stack.currentIndex() == P_WORK) else None))
            if work_only:
                self._work_shortcuts.append(s)
            return s
        # 1.2.0：工作台快捷键（Enter / I / O …）只在焦点不在输入框时生效——
        # 以前全局的 Enter 会抢走搜索框的回车，导致已有结果时再搜没反应
        self._work_shortcuts = []
        QApplication.instance().focusChanged.connect(self._focus_changed)
        sc("/", self.focus_search)
        sc("Space", self.toggle_play)
        sc("K", self.toggle_play)
        sc("Return", self.play_clip, True)
        sc("I", self.set_in, True)
        sc("O", self.set_out, True)
        sc("[", self.audition_in, True)
        sc("]", self.audition_out, True)
        sc("U", self.select_sub, True)
        sc("Alt+Up", lambda: self._script_step(-1), True)
        sc("Alt+Down", lambda: self._script_step(1), True)
        sc("Escape", lambda: self.goto(self._prev_page) if self.stack.currentIndex() == P_WORK else None)

    def _focus_changed(self, _old, new):
        typing = isinstance(new, (QLineEdit, QSpinBox, QDoubleSpinBox)) or (
            isinstance(new, QComboBox) and new.isEditable())
        for s in getattr(self, "_work_shortcuts", []):
            s.setEnabled(not typing)

    def _show_keys(self):
        QMessageBox.information(self, "快捷键", "\n".join((
            "/　　　　跳到搜索框", "空格 / K　播放 / 暂停", "Ctrl+↑ / ↓　上一条 / 下一条结果",
            "Ctrl+Enter　播放本句", "Ctrl+1…5　切换页面（媒体库 / 搜索 / 工作台 / 采集包 / 导出任务）",
            "", "工作台：", "Enter　　播放选区", "I / O　　入点 / 出点设到播放头", "U　　　选区定到虚线（原字幕时间）", "[ / ]　　试听开头 / 结尾 1 秒",
            "Alt+↑ / ↓　台词本上一句 / 下一句", "Ctrl+S　保存微调", "Esc　　返回",
            "波形：拖两端改切点，Alt 拖不吸附；滚轮平移，Ctrl/⌘+滚轮缩放；中键拖动平移",
            "", "Ctrl+D 采集　Ctrl+E 导出这条　Ctrl+Shift+C 复制片段链接")))

    def focus_search(self):
        self.q.setFocus(); self.q.selectAll()

    def _default_layout(self):
        for sp in self.findChildren(QSplitter):
            self.qs.remove("split107/" + sp.objectName())
        self.findChild(QSplitter, "sp_lib").setSizes([240, 1100])
        self.findChild(QSplitter, "sp_search").setSizes([820, 480])
        self.findChild(QSplitter, "sp_work").setSizes([860, 460])
        self.findChild(QSplitter, "sp_work_left").setSizes([420, 380])
        self.toolbar.show()

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

    # ================================================================== 媒体库
    def _first_run(self):
        QMessageBox.information(self, APP, "先选择媒体库文件夹（移动硬盘上那个）。\n\n"
                                "整理方式：剧名/S01/S01E01.mp4，字幕同名放旁边（.srt 或 .ass，单独中文字幕加 .zh）。\n"
                                "字幕组发布包结构也能认（顶层文件夹 = 剧名，按 S01E02 配对）。")
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
        if not ALIGN_UI and self.lib_settings()["auto_align"]:
            self.lib.set_setting("auto_align", False)
        if self.qs.value("manual_align_105", "") != root:
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
        # 1.0.9：启动/重扫不再自动开跑。只有开了「入库后自动对齐」且这次扫到新视频，才把这些新集排队。
        if not ALIGN_UI:
            return
        new_keys = st.get("new_keys") or []
        if new_keys and self.lib_settings()["auto_align"]:
            self.queue_keys(new_keys)
            return
        n_q = self.lib.db.execute("SELECT COUNT(*) FROM media WHERE align_status IN ('queued','running')").fetchone()[0]
        if n_q:
            self.al_lb.setText(self.al_lb.text() + f"　上次还有 {n_q} 集在排队，到对齐中心点「开始」继续")

    def refresh_tree(self):
        if not self.lib:
            return
        sel = {it.data(0, Qt.UserRole) for it in self.tree.selectedItems()}
        self.tree.blockSignals(True)
        self.tree.clear()
        rows = self.lib.shows()
        tot = sum(r["n"] for r in rows)
        it = QTreeWidgetItem(["全部作品", str(tot)]); it.setData(0, Qt.UserRole, "")
        it.setIcon(0, icon("library", theme.ACCENT_D, 16))
        self.tree.addTopLevelItem(it)
        cur = self.f_show.currentData()
        self.f_show.blockSignals(True)
        self.f_show.clear(); self.f_show.addItem("全部作品", "")
        for r in rows:
            t = QTreeWidgetItem([r["show"], f"{r['aligned'] or 0}/{r['has_video'] or 0}"])
            t.setData(0, Qt.UserRole, r["show"])
            t.setIcon(0, icon("folder" if r["kind"] == "tv" else "film", theme.ACCENT_D, 16))
            t.setToolTip(1, "已对齐 / 有视频的集数")
            if not r["has_video"]:
                t.setToolTip(0, "只有字幕，没有视频：能搜索，暂时不能播放")
            self.tree.addTopLevelItem(t)
            self.f_show.addItem(r["show"], r["show"])
        i = self.f_show.findData(cur)
        self.f_show.setCurrentIndex(max(0, i))
        self.f_show.blockSignals(False)
        for i in range(self.tree.topLevelItemCount()):
            t = self.tree.topLevelItem(i)
            if t.data(0, Qt.UserRole) in sel:
                t.setSelected(True)
        if not self.tree.selectedItems():
            self.tree.topLevelItem(0).setSelected(True)
        self.tree.blockSignals(False)
        self.refresh_lib_page()

    def _show_picked(self):
        sel = [it.data(0, Qt.UserRole) for it in self.tree.selectedItems()]
        self.lib_show = sel[0] if len(sel) == 1 else ""
        self.refresh_lib_page()

    def refresh_lib_page(self):
        if not self.lib:
            return
        show = self.lib_show
        self.crumb.setText("<a href='#' style='color:#0F766E;text-decoration:none'>媒体库</a>"
                           + (f" / <b style='color:#1E293B'>{show}</b>" if show else " / 全部作品"))
        self.al_show_btn.setText("对齐这部作品" if show else "对齐全部未对齐")
        db = self.lib.db
        where, args = ("WHERE show=?", (show,)) if show else ("", ())
        n, nv, na, nq = db.execute(f"SELECT COUNT(*), SUM(rel_video IS NOT NULL), SUM(align_status='done'), "
                                   f"SUM(align_status IN ('queued','running')) FROM media {where}", args).fetchone()
        self.al_sum.setText(f"{na or 0} / {nv or 0} 集已对齐（有视频的）" + (f"，{nq} 集排队中" if nq else "")
                            + "。对齐后切点精确到词，并能补出字幕里漏掉的台词。")
        if not (self.align_worker and self.align_worker.isRunning()):
            self.al_bar.setValue(int((na or 0) / max(1, nv or 0) * 1000))
        ch = self.lib.changed_keys()
        if ch and ALIGN_UI:
            self.banner_lb.setText(f"<b>{len(ch)} 集换过字幕或视频</b>，之前的对齐对不上了（搜索已按新字幕重建）。")
            self.banner.show()
        else:
            self.banner.hide()
        self.refresh_episodes()

    @staticmethod
    def _ep_tags(r):
        tags = [("字幕", "#64748B", False) if r["rel_sub"] else ("无字幕", "#E11D48", False)]
        if not r["rel_video"]:
            tags.append(("无视频", "#E11D48", False))
        st = r["align_status"]
        if r["sub_changed"]:
            tags.append(("已变更", "#F59E0B", True))
        elif st == "done":
            tags.append(("已对齐", "#10B981", False))
        elif st in ("queued", "running"):
            tags.append(("对齐中" if st == "running" else "排队", "#7C3AED", False))
        elif st == "error":
            tags.append(("对齐失败", "#E11D48", False))
        return tags

    def refresh_episodes(self):
        self.eps.setUpdatesEnabled(False)
        self.eps.clear()
        self._ep_items = {}
        if not self.lib:
            self.eps.setUpdatesEnabled(True)
            return
        from .widgets import TAGS_ROLE
        rows = self.lib.episodes(self.lib_show or None)
        items = []
        for r in rows[:5000]:
            ep = f"S{r['season']:02d}E{r['episode']:02d}" if r["kind"] == "tv" else "电影"
            name = Path(r["rel_video"] or r["rel_sub"] or r["key"]).name
            lines = f"{r['n_cues']} 条" + (f" +{r['n_auto']} 识别" if r["n_auto"] else "")
            it = QTreeWidgetItem([ep if self.lib_show else f"{r['show']}  {ep}", name, lines, ""])
            it.setData(0, Qt.UserRole, r["key"])
            it.setToolTip(1, r["rel_video"] or "（没有视频）")
            it.setData(3, TAGS_ROLE, self._ep_tags(r))
            items.append(it)
            self._ep_items[r["key"]] = it
        self.eps.addTopLevelItems(items)
        self._filter_eps(self.ep_filter.text())
        self.eps.setUpdatesEnabled(True)

    def update_episode_row(self, key):
        """只改一行（对齐状态变了），不重建整张表。"""
        it = self._ep_items.get(key)
        if it is None or not self.lib:
            return
        from .widgets import TAGS_ROLE
        r = self.lib.db.execute(
            "SELECT m.*, (SELECT COUNT(*) FROM cues c WHERE c.media_id=m.id AND c.src='sub') n_cues, "
            "(SELECT COUNT(*) FROM cues c WHERE c.media_id=m.id AND c.src='auto') n_auto FROM media m WHERE key=?",
            (key,)).fetchone()
        if not r:
            return
        it.setText(2, f"{r['n_cues']} 条" + (f" +{r['n_auto']} 识别" if r["n_auto"] else ""))
        it.setData(3, TAGS_ROLE, self._ep_tags(r))

    def refresh_counts(self):
        """对齐一集后：只更新左栏作品计数、对齐卡数字和黄条，不动集列表。"""
        if not self.lib:
            return
        cnt = {r["show"]: r for r in self.lib.shows()}
        for i in range(1, self.tree.topLevelItemCount()):
            t = self.tree.topLevelItem(i)
            r = cnt.get(t.data(0, Qt.UserRole))
            if r:
                t.setText(1, f"{r['aligned'] or 0}/{r['has_video'] or 0}")
        show = self.lib_show
        where, args = ("WHERE show=?", (show,)) if show else ("", ())
        n, nv, na, nq = self.lib.db.execute(
            f"SELECT COUNT(*), SUM(rel_video IS NOT NULL), SUM(align_status='done'), "
            f"SUM(align_status IN ('queued','running')) FROM media {where}", args).fetchone()
        self.al_sum.setText(f"{na or 0} / {nv or 0} 集已对齐（有视频的）" + (f"，{nq} 集排队中" if nq else "")
                            + "。对齐后切点精确到词，并能补出字幕里漏掉的台词。")
        ch = self.lib.changed_keys()
        if ch:
            self.banner_lb.setText(f"<b>{len(ch)} 集换过字幕或视频</b>，之前的对齐对不上了（搜索已按新字幕重建）。")
        self.banner.setVisible(bool(ch) and ALIGN_UI)

    def _filter_eps(self, text):
        t = text.strip().lower()
        for i in range(self.eps.topLevelItemCount()):
            it = self.eps.topLevelItem(i)
            it.setHidden(bool(t) and t not in (it.text(0) + " " + it.text(1)).lower())

    def _eps_menu(self, pos):
        keys = [i.data(0, Qt.UserRole) for i in self.eps.selectedItems()]
        it = self.eps.itemAt(pos)
        if it and it.data(0, Qt.UserRole) not in keys:
            self.eps.clearSelection(); it.setSelected(True); keys = [it.data(0, Qt.UserRole)]
        if not keys:
            return
        mnu = QMenu(self)
        mnu.addAction("在工作台打开", lambda: self.open_episode(keys[0]))
        if ALIGN_UI:
            mnu.addSeparator()
            mnu.addAction(f"对齐这 {len(keys)} 集", lambda: self.queue_keys(keys))
            mnu.addAction(f"重新对齐这 {len(keys)} 集", lambda: self.queue_keys(keys, redo=True))
        mnu.exec(self.eps.viewport().mapToGlobal(pos))

    def _tree_pick(self, it, _col):
        i = self.f_show.findData(it.data(0, Qt.UserRole))
        if i >= 0:
            self.f_show.setCurrentIndex(i)
            self.statusBar().showMessage(f"搜索范围：{self.f_show.currentText()}", 2500)
            self.focus_search()

    def open_episode(self, key):
        if not self.lib or not key:
            return
        m = self.lib.media_by_key(key)
        r = self.lib.db.execute("SELECT * FROM cues WHERE media_id=? AND COALESCE(src,'sub')!='join' "
                                "ORDER BY COALESCE(a_start,start) LIMIT 1", (m["id"],)).fetchone()
        if not r:
            QMessageBox.information(self, APP, "这一集没有台词。")
            return
        self.open_workspace(self.hit_from_row(r, m), autoplay=False)

    def _realign_changed(self):
        if not self.lib:
            return
        keys = self.lib.changed_keys()
        if keys:
            self.queue_keys(keys, redo=True)
            self.refresh_lib_page()

    # ================================================================== 对齐
    def lib_settings(self):
        g = self.lib.get_setting
        return dict(align_model=g("align_model", "small"), align_engine=g("align_engine", ""),
                    hf_mirror=g("hf_mirror", True), auto_align=bool(g("auto_align", False)),
                    # 性能上限只跟这台电脑有关（Win / Mac 共用一个媒体库），存在本机设置里
                    align_limit=int(self.qs.value("align_limit", 80) or 80),
                    align_isolate=str(self.qs.value("align_isolate", "true")).lower() != "false")

    def set_align_limit(self, pct: int):
        self.qs.setValue("align_limit", int(pct))
        if self.align_worker:
            self.align_worker.settings = self.lib_settings()

    def start_align(self, force=False):
        if not self.lib:
            return
        if not force and not self.lib_settings()["auto_align"]:
            self.al_lb.setText("右键作品 / 集，或到对齐中心选集")
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
        self.statusBar().showMessage(f"已排队 {n} 集" if n else "选中的都已对齐过（要重跑用「重新对齐」）", 4000)
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
        if not self.lib:
            return
        shows = [it.data(0, Qt.UserRole) for it in self.tree.selectedItems()]
        shows = [s for s in shows if s]
        if not shows:
            self._align_all()
            return
        self.queue_keys(self.show_keys(shows))
        self.refresh_lib_page()

    def _align_all(self):
        if not self.lib:
            return
        n = self.lib.db.execute("SELECT COUNT(*) FROM media WHERE align_status IN ('none','error') "
                                "AND rel_video IS NOT NULL AND rel_sub IS NOT NULL").fetchone()[0]
        if QMessageBox.question(self, APP, f"把 {n} 集未对齐的全部排队？整库会跑很久。") == QMessageBox.Yes:
            self.queue_keys([r[0] for r in self.lib.db.execute("SELECT key FROM media")])
            self.refresh_lib_page()

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

    def _tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        if it and not it.isSelected():
            self.tree.clearSelection(); it.setSelected(True)
        shows = [i.data(0, Qt.UserRole) for i in self.tree.selectedItems() if i.data(0, Qt.UserRole)]
        mnu = QMenu(self)
        if shows:
            lb = shows[0] if len(shows) == 1 else f"{len(shows)} 部作品"
            if ALIGN_UI:
                mnu.addAction(f"对齐「{lb}」未对齐的集", self._align_selected_shows)
                mnu.addAction(f"重新对齐「{lb}」全部集", lambda: self.queue_keys(self.show_keys(shows), redo=True))
            mnu.addAction("只搜这部作品", lambda: self._tree_pick(it or self.tree.selectedItems()[0], 0))
        if ALIGN_UI:
            mnu.addSeparator()
            mnu.addAction("对齐中心…", self.open_align_center)
        if mnu.isEmpty():
            return
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
        if key != getattr(self, "_al_key", None):
            self._al_key = key
            self.update_episode_row(key)
        self.al_lb.setText(f"正在对齐：{key}　{text}　（已完成 {done} 集，还剩 {todo} 集）")
        self.al_bar.setValue(int(f * 1000))

    def _align_done(self, key, msg):
        self.al_log.show()
        self.al_log.insertItem(0, f"{time.strftime('%H:%M')}  {key}  {msg}")
        self.update_episode_row(key)
        self.refresh_counts()
        if self.cur and self.cur.media_key == key:
            self.ws_media = None
            self.refresh_current()

    def _align_idle(self):
        n = self.lib.db.execute("SELECT COUNT(*) FROM media WHERE align_status='done'").fetchone()[0] if self.lib else 0
        self.al_lb.setText(f"对齐：队列里的都跑完了（共 {n} 集已对齐）")
        self.refresh_counts()

    def _toggle_align(self):
        if not self.align_worker:
            return
        on = not self.align_worker.paused
        self.align_worker.pause(on)
        self.al_btn.setText("继续" if on else "暂停")
        if on:
            self.al_lb.setText("对齐已暂停（当前这集跑完后停）")

    # ================================================================== 搜索
    def do_search(self):
        if not self.searcher:
            return
        q = self.q.text().strip()
        if not q:
            return
        m = LINK_RX.match(q)
        if m:
            self.open_link(unquote(m.group(1)), int(m.group(2)))
            return
        filters = {}
        if self.f_show.currentData():
            filters["shows"] = [self.f_show.currentData()]
        if self.f_season.value():
            filters["season"] = self.f_season.value()
        if self.c_video.isChecked():
            filters["video_only"] = True
        if self.c_machine.isChecked():
            filters["machine"] = True
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
        self.checked.clear()
        self.goto(P_SEARCH)
        self.render_results()

    def filtered(self):
        i = self.tabs.currentIndex()
        if i <= 0:
            return self.hits
        tier = (TIER_ORIG, TIER_NEAR, TIER_FUNC)[i - 1]
        return [h for h in self.hits if h.tier == tier]

    @staticmethod
    def is_ted(h):
        return "ted" in re.split(r"[^a-z0-9]+", (h.show or "").lower())

    def sorted_hits(self):
        """当前标签页里的结果，按排序选项排好。"""
        lst = self.filtered()
        if self.c_rank.isChecked():
            lst = sorted(lst, key=lambda h: (-h.score, _tier_rank(h.tier)))
        if self.c_ted.isChecked():
            lst = sorted(lst, key=lambda h: not self.is_ted(h))        # 稳定排序：TED 整体提前，内部顺序不变
        return lst

    def render_results(self):
        """默认（只按匹配度）：一列按分数排，每条带出处。关掉则按集分组（组的顺序 = 组里最好那条的名次）。"""
        lst = self.sorted_hits()
        groups, order = {}, []
        for h in lst:
            if h.media_key not in groups:
                groups[h.media_key] = []; order.append(h.media_key)
            groups[h.media_key].append(h)
        self.items = []
        if self.c_rank.isChecked():
            self.items = [("r", h) for h in lst]
        else:
            for k in order:
                hs = groups[k]
                self.items.append(("g", k, hs[0].label, len(hs)))
                self.items += [("r", h) for h in hs]
        self.results.clear(); self.rows.clear(); self.group_items.clear()
        self.shown = 0
        self._more()
        n = len(lst)
        files = len(order)
        self.count_lb.setText(f"{n} 条，分布在 {files} 集 · 同集同句已合并 · {getattr(self, 'search_ms', 0):.0f} ms"
                              if n else "没有找到：试试关掉「精确 / 严格顺序」，或换个说法")
        self._update_sel()

    def _more(self):
        vw = self._row_width()
        cur_group = None
        for item in self.items[self.shown:self.shown + PAGE]:
            it = QListWidgetItem()
            if item[0] == "g":
                w = GroupHeader(item[1], item[2], item[3])
                w.toggled.connect(self._group_toggled)
                cur_group = item[1]
                self.group_items.setdefault(cur_group, [])
            else:
                h = item[1]
                row = self._cue_row(h)
                s, e, src = self.lib.final_span(row, h.media_key)
                w = ResultRow(h, src, (s, e), show_source=self.c_rank.isChecked())
                w.clicked.connect(lambda h: self.preview(h))
                w.double.connect(self.open_workspace)
                w.collect.connect(self.collect)
                w.export_one.connect(self.export_one)
                w.link.connect(self.copy_link)
                w.checked.connect(self._row_checked)
                if h.cue_id in self.checked:
                    w.cb.setChecked(True)
                if self.cur and h.cue_id == self.cur.cue_id:
                    w.set_selected(True)
                self.rows[h.cue_id] = (it, w)
                if not self.c_rank.isChecked():
                    self.group_items.setdefault(h.media_key, []).append(it)
                    it.setHidden(h.media_key in self.collapsed)
            self.results.addItem(it)
            self.results.setItemWidget(it, w)
            it.setSizeHint(QSize(vw, self._row_h(w, vw)))
        self.shown = min(len(self.items), self.shown + PAGE)

    def _row_width(self):
        return max(200, self.results.viewport().width() - 4)

    def _row_h(self, w, vw):
        # 1.2.0：先让样式表（字号、内边距）生效再量高度，否则量出来偏矮，英文台词的 y / g 下半截被切掉
        w.ensurePolished()
        for c in w.findChildren(QWidget):
            c.ensurePolished()
        lay = w.layout()
        if lay is not None:
            lay.invalidate()
        h = w.heightForWidth(vw) if w.hasHeightForWidth() else w.sizeHint().height()
        # 列表的 spacing 会从格子里上下各扣掉一截，再加回来（以前就是这里把文字压扁、切掉下伸部）
        return h + 2 * self.results.spacing()

    def _relayout_rows(self):
        vw = self._row_width()
        for i in range(self.results.count()):
            it = self.results.item(i)
            w = self.results.itemWidget(it)
            if w is not None:
                it.setSizeHint(QSize(vw, self._row_h(w, vw)))

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if obj is self.results.viewport() and ev.type() == QEvent.Resize:
            self._relayout_timer.start()
        return super().eventFilter(obj, ev)

    def _group_toggled(self, key, open_):
        (self.collapsed.discard if open_ else self.collapsed.add)(key)
        for it in self.group_items.get(key, []):
            it.setHidden(not open_)

    def _maybe_more(self, v):
        sb = self.results.verticalScrollBar()
        if v >= sb.maximum() - 60 and self.shown < len(self.items):
            self._more()

    def _row_checked(self, h, on):
        if on:
            self.checked[h.cue_id] = h
        else:
            self.checked.pop(h.cue_id, None)
        self._update_sel()

    def _check_all(self, on):
        lst = self.filtered()
        if on:
            self.checked = {h.cue_id: h for h in lst}
        else:
            self.checked.clear()
        for cid, (_it, w) in self.rows.items():
            w.cb.blockSignals(True); w.cb.setChecked(cid in self.checked); w.cb.blockSignals(False)
        self._update_sel()

    def _update_sel(self):
        n = len(self.checked)
        self.sel_lb.setText(f"已选 {n} 条" if n else "勾选几条可以一起采集 / 导出")
        self.b_bcollect.setText(f"采集选中（{n}）" if n else "采集选中")
        self.b_bexport.setText(f"导出选中（{n}）" if n else "导出选中")
        self.b_bcollect.setEnabled(bool(n)); self.b_bexport.setEnabled(bool(n))

    def _batch_collect(self):
        if not self.checked:
            return
        if self.pack_cb.count() == 0:
            self.lib.pack_id(self.q.text().strip() or "未命名"); self.refresh_packs()
        name = self.pack_cb.currentText()
        n = sum(self.lib.add_to_pack(name, h.media_key, h.idx, self.q.text().strip(), h.tier)
                for h in self.checked.values())
        self.statusBar().showMessage(f"采集了 {n} 条到「{name}」（{len(self.checked) - n} 条本来就在）", 4000)
        self.load_pack()

    def _batch_export(self):
        hs = [h for h in self.hits if h.cue_id in self.checked]
        if not hs:
            return
        dlg = ExportDialog(f"选中的 {len(hs)} 条", len(hs), str(Path(self._export_dir()) / "选中"), self)
        if dlg.exec():
            spec = [dict(media_key=h.media_key, idx=h.idx, pattern=self.q.text().strip(), tier=h.tier,
                         matched=h.matched) for h in hs]
            self._run_export(f"选中的 {len(hs)} 条 · {self.q.text().strip()[:20]}", dlg.dir.text(), dlg.presets(),
                             dlg.comp.isChecked(), items=spec)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if not hasattr(self, "nav"):
            return
        wide = self.width() >= 1260
        style = Qt.ToolButtonTextBesideIcon if wide else Qt.ToolButtonIconOnly
        for b in self.nav + [self.top_align]:
            if b.toolButtonStyle() != style:
                b.setToolButtonStyle(style)
        self.brand.setVisible(self.width() >= 1100)
        self.searchbox.setMinimumWidth(460 if self.width() >= 1360 else 360 if wide else 240)

    def _cue_row(self, h):
        return self.lib.db.execute("SELECT * FROM cues WHERE id=?", (h.cue_id,)).fetchone()

    def hit_from_row(self, r, m=None):
        m = m or self.lib.db.execute("SELECT * FROM media WHERE id=?", (r["media_id"],)).fetchone()
        return Hit(r["id"], r["media_id"], m["key"], m["show"], m["season"], m["episode"], m["kind"], r["idx"],
                   r["start"], r["end"], r["en"], r["zh"] or "", r["speakers"], r["a_start"], r["a_end"],
                   r["a_score"], m["rel_video"] is not None, "", 0, "", r["src"] or "sub")

    # ================================================================== 片段链接
    def link_of(self, h):
        return f"clip://{quote(h.media_key)}#{h.idx}"

    def copy_link(self, h):
        txt = f"{self.link_of(h)}  {h.label} {fmt_ms(h.start)}  {h.en}"
        QApplication.clipboard().setText(txt)
        self.statusBar().showMessage("已复制片段链接：粘贴到搜索框回车就能直接打开这一条", 4000)

    def open_link(self, key, idx):
        if not self.lib:
            return
        m = self.lib.media_by_key(key)
        r = self.lib.db.execute("SELECT * FROM cues WHERE media_id=? AND idx=?", (m["id"], idx)).fetchone() if m else None
        if not r:
            QMessageBox.information(self, APP, "这个片段链接在当前媒体库里找不到（可能换过库或删了这一集）。")
            return
        self.open_workspace(self.hit_from_row(r, m))

    # ================================================================== 预览 / 工作台
    def open_workspace(self, h, autoplay=True):
        self.goto(P_WORK)
        self.preview(h, autoplay)

    def preview(self, h, autoplay=True):
        old = self.cur
        self.cur = h
        if old and old.cue_id in self.rows:
            self.rows[old.cue_id][1].set_selected(False)
        if h.cue_id in self.rows:
            self.rows[h.cue_id][1].set_selected(True)
        self.refresh_current()
        self.show_context(h)
        if not h.has_video:
            return
        m = self.lib.media_by_key(h.media_key)
        url = QUrl.fromLocalFile(str(self.lib.abs(m["rel_video"])))
        self._pending_play = autoplay
        if self.player.source() != url:
            self.player.setSource(url)
        elif autoplay:
            self.play_clip()

    def _load_script(self, m):
        """换集时：台词本、说话地图、波形上的台词条。"""
        self.ws_media = m
        rows = self.lib.script(m["id"])
        self.script_rows = rows
        t_of = lambda r: r["a_start"] if r["a_start"] is not None else r["start"]
        self._script_starts = [t_of(r) for r in rows]
        self.script.clear()
        mono = QFont(MONO.split(",")[0]); mono.setPointSize(11)
        for r in rows:
            auto = r["src"] == "auto"
            txt = f"{fmt_short(t_of(r)):>8}    " + ("识别 · " if auto else "") + r["en"]
            if r["zh"]:
                txt += f"\n{'':>12}{r['zh']}"
            it = QListWidgetItem(txt)
            it.setData(Qt.UserRole, r["idx"])
            if auto:
                f = QFont(); f.setItalic(True); it.setFont(f); it.setForeground(QColor("#7C3AED"))
            self.script.addItem(it)
        dur = m["duration"] or (max((r["end"] for r in rows), default=60) + 5)
        self.dmap.set_data(dur, [(t_of(r), r["a_end"] or r["end"], r["src"]) for r in rows])
        self._wave_lines = [(t_of(r), r["a_end"] if r["a_start"] is not None else r["end"], r["en"], r["idx"],
                             r["src"]) for r in rows]
        name = Path(m["rel_video"] or m["rel_sub"] or m["key"]).name
        self.ws_sub.setText(f"{name}　·　{len(rows)} 句台词" + ("" if m["align_status"] == "done" else "　·　未对齐（切点用字幕时间）"))

    def refresh_current(self):
        h = self.cur
        if not h or not self.lib:
            return
        row = self._cue_row(h)
        if row is None:
            return
        m = self.lib.media_by_key(h.media_key)
        if self.ws_media is None or self.ws_media["id"] != m["id"] or self.ws_media["align_status"] != m["align_status"]:
            self._load_script(m)
        s, e, src = self.lib.final_span(row, h.media_key)
        self.cur_span = (s, e)
        # 文字
        html_en = highlight_html(h.en, h.matched)
        self.sub_en.setText(html_en); self.sub_zh.setText(h.zh)
        self.pv_en.setText(html_en); self.pv_zh.setText(h.zh)
        self.pv_title.setText(h.label)
        self.pv_time.setText(f"{fmt_ms(s)} – {fmt_ms(e)}")
        self.ws_title.setText(f"{h.label}")
        # 波形
        words = self.lib.words(h.media_key)
        t0 = max(0.0, min(s, row["start"]) - 4)
        t1 = max(e, row["end"]) + 4
        self.wave.set_data(t0, t1, words, self._wave_lines, (row["start"], row["end"]), (s, e))
        self._update_cut_label(row, s, e, src)
        self._highlight_script(h.idx)
        if h.has_video:
            self._wave_token += 1
            w = WaveWorker(self._wave_token, self.lib.abs(m["rel_video"]), t0, t1)
            w.done.connect(lambda tok, arr, t0=t0: self._wave_done(tok, arr, t0))
            self._wave_workers.append(w)
            w.finished.connect(lambda w=w: self._wave_workers.remove(w) if w in self._wave_workers else None)
            w.start()

    def _wave_done(self, token, arr, t0):
        if token == self._wave_token and arr is not None:
            self.wave.set_samples(arr, t0)

    def _highlight_script(self, idx):
        if idx >= JOIN_BASE and idx < AUTO_BASE:
            idx -= JOIN_BASE
        for i in range(self.script.count()):
            if self.script.item(i).data(Qt.UserRole) == idx:
                self.script.blockSignals(True)
                self.script.setCurrentRow(i)
                self.script.blockSignals(False)
                self.script.scrollToItem(self.script.item(i), QAbstractItemView.PositionAtCenter)
                break

    def _update_cut_label(self, row, s, e, src):
        name, color = TIMING_TEXT.get(src, ("", "#000"))
        self.cut_lb.setText(
            f"<span style='color:{color}'>● {name}</span>　选区 <span style='font-family:{MONO}'>"
            f"{fmt_ms(s)} → {fmt_ms(e)}</span>（{e - s:.2f} 秒）　原字幕 {fmt_ms(row['start'])} → "
            f"{fmt_ms(row['end'])}　修正 {s - row['start']:+.2f} / {e - row['end']:+.2f} 秒")

    def _view_changed(self, a, b):
        self.zoom_lb.setText(f"{b - a:.2f} 秒宽")
        self.dmap.set_view(a, b)

    def _snap_changed(self):
        self.wave.snap = {k for k, c in self.snap_chips.items() if c.isChecked()}
        self.qs.setValue("snap107", ",".join(sorted(self.wave.snap)))

    def _map_seek(self, t):
        """点说话地图：选中离这个时间最近的那句。"""
        if not self.script_rows:
            return
        k = max(0, bisect.bisect_right(self._script_starts, t) - 1)
        self.pick_cue(self.script_rows[k]["idx"])

    def pick_cue(self, idx, extend=False):
        if not self.ws_media or idx is None:
            return
        r = self.lib.db.execute("SELECT * FROM cues WHERE media_id=? AND idx=?", (self.ws_media["id"], idx)).fetchone()
        if not r:
            return
        if extend and self.cur and self.cur_span and self.cur.media_id == r["media_id"]:
            s, e, _ = self.lib.final_span(r, self.cur.media_key)
            self.cur_span = (min(self.cur_span[0], s), max(self.cur_span[1], e))
            self.wave.set_cut(self.cur_span)
            self._cut_changed(*self.cur_span)
            return
        self.preview(self.hit_from_row(r, self.ws_media))

    def _script_step(self, d):
        i = self.script.currentRow() + d
        if 0 <= i < self.script.count():
            self.pick_cue(self.script.item(i).data(Qt.UserRole))

    def _on_status(self, st):
        if st in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and getattr(self, "_pending_play", False):
            self._pending_play = False
            self.play_clip()

    def _sync_audio_device(self):
        """系统的音频输出变了（连上 / 断开 AirPods 等）：换一个跟着新默认设备的 QAudioOutput。
        只在设备列表变化时做；不在每次播放时去动设备（Mac 上播放中 setDevice 可能卡死）。"""
        try:
            dev = QMediaDevices.defaultAudioOutput()
            if dev.isNull() or dev.id() == self.audio.device().id():
                return
            vol, muted = self.audio.volume(), self.audio.isMuted()
            old = self.audio
            self.audio = QAudioOutput(dev, self)
            self.audio.setVolume(vol); self.audio.setMuted(muted)
            self.player.setAudioOutput(self.audio)
            old.deleteLater()
        except Exception:   # noqa
            pass

    def play_range(self, a, b):
        if not self.cur or not self.cur.has_video:
            return
        self._stop_at = b
        self.player.setPosition(int(max(0.0, a) * 1000))
        self.player.setPlaybackRate(self.speed.currentData())
        self.player.play()

    def play_clip(self):
        if self.cur_span:
            self.play_range(*self.cur_span)

    def audition_in(self):
        if self.cur_span:
            s, e = self.cur_span
            self.play_range(s, min(e, s + 1.0))

    def audition_out(self):
        if self.cur_span:
            s, e = self.cur_span
            self.play_range(max(s, e - 1.0), e)

    def _play_from(self, t):
        self._stop_at = None
        self.player.setPosition(int(t * 1000))
        self.player.play()

    def toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        elif self.cur_span:
            pos = self.player.position() / 1000
            if self.cur_span[0] <= pos < self.cur_span[1] - 0.05:
                self._stop_at = self.cur_span[1]
                self.player.play()
            else:
                self.play_clip()

    def set_in(self):
        if self.cur_span:
            t = self.player.position() / 1000
            self._cut_changed(t, max(t + 0.05, self.cur_span[1]))
            self.wave.set_cut(self.cur_span)

    def set_out(self):
        if self.cur_span:
            t = self.player.position() / 1000
            self._cut_changed(min(self.cur_span[0], t - 0.05), t)
            self.wave.set_cut(self.cur_span)

    def _on_pos(self, ms):
        t = ms / 1000
        self.wave.set_playhead(t)
        self.dmap.set_playhead(t)
        if self.c_follow.isChecked() and self._script_starts and self.player.playbackState() == QMediaPlayer.PlayingState:
            k = bisect.bisect_right(self._script_starts, t + 0.05) - 1
            if 0 <= k < self.script.count() and self.script.currentRow() != k:
                self.script.blockSignals(True); self.script.setCurrentRow(k); self.script.blockSignals(False)
                self.script.scrollToItem(self.script.item(k), QAbstractItemView.PositionAtCenter)
        stop = self._stop_at
        if stop is not None and t >= stop and self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self._stop_at = None
            if self.c_loop.isChecked() and self.stack.currentIndex() == P_SEARCH:
                QTimer.singleShot(400, lambda: self.step(1))

    def step(self, d):
        lst = [h for h in self.sorted_hits() if h.has_video]
        if not lst:
            return
        try:
            i = next(k for k, h in enumerate(lst) if self.cur and h.cue_id == self.cur.cue_id)
        except StopIteration:
            i = -1 if d > 0 else len(lst)
        j = i + d
        if 0 <= j < len(lst):
            h = lst[j]
            while h.cue_id not in self.rows and self.shown < len(self.items):
                self._more()
            self.preview(h)
            if h.cue_id in self.rows:
                self.results.scrollToItem(self.rows[h.cue_id][0])

    def _cut_changed(self, s, e):
        self.cur_span = (s, e)
        row = self._cue_row(self.cur)
        self._update_cut_label(row, s, e, "manual")
        self.pv_time.setText(f"{fmt_ms(s)} – {fmt_ms(e)}（未保存）")

    def select_sub(self):
        """选区 = 波形上两条虚线之间（这句原字幕的时间）。"""
        sub = self.wave.sub
        if not self.cur or not sub:
            return
        self._cut_changed(float(sub[0]), float(sub[1]))
        self.wave.set_cut(self.cur_span)
        self.statusBar().showMessage("选区已定到虚线（原字幕时间）；满意就 Ctrl+S 保存微调", 3000)

    def save_cut(self):
        if self.cur and self.cur_span:
            self.lib.save_adjust(self.cur.media_key, self.cur.idx, *self.cur_span)
            self.statusBar().showMessage("已保存微调（重新对齐也不会覆盖）", 3000)
            self.refresh_current()

    def reset_cut(self):
        if self.cur:
            self.lib.clear_adjust(self.cur.media_key, self.cur.idx)
            self.refresh_current()

    # ------------------------------------------------------------ 上下文（搜索页预览里）
    def show_context(self, h):
        self.ctx.clear()
        rows = self.lib.context(h.media_id, h.idx, 2)
        base = h.idx - JOIN_BASE if JOIN_BASE <= h.idx < AUTO_BASE else h.idx
        for r in rows:
            t = r["a_start"] if r["a_start"] is not None else r["start"]
            auto = r["src"] == "auto"
            it = QListWidgetItem(f"{fmt_short(t):>8}   " + ("识别 · " if auto else "") + r["en"]
                                 + (f"\n{'':>11}{r['zh']}" if r["zh"] else ""))
            it.setData(Qt.UserRole, r["idx"])
            if auto:
                it.setForeground(QColor("#7C3AED"))
            self.ctx.addItem(it)
            if r["idx"] == base:
                f = it.font(); f.setBold(True); it.setFont(f)
                it.setBackground(QColor(204, 251, 241, 160))
                row = self.ctx.count() - 1
                QTimer.singleShot(0, lambda row=row: self.ctx.item(row) and self.ctx.scrollToItem(
                    self.ctx.item(row), QAbstractItemView.PositionAtCenter))

    def _ctx_pick(self, it):
        if not self.cur:
            return
        r = self.lib.db.execute("SELECT * FROM cues WHERE media_id=? AND idx=?",
                                (self.cur.media_id, it.data(Qt.UserRole))).fetchone()
        if r:
            s, e, _ = self.lib.final_span(r, self.cur.media_key)
            self.play_range(s, e)

    # ================================================================== 采集包
    def refresh_packs(self, select=None):
        self.pack_cb.blockSignals(True)
        cur = select or self.pack_cb.currentText()
        self.pack_cb.clear()
        for p in self.lib.packs():
            self.pack_cb.addItem(p["name"], p["id"])
        i = self.pack_cb.findText(cur) if cur else -1
        self.pack_cb.setCurrentIndex(max(0, i))
        self.pack_cb.blockSignals(False)
        self.load_pack()
        self._sync_pack_buttons()

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

    def load_pack(self):
        self.pack_list.clear()
        pid = self.pack_cb.currentData()
        if pid is None or not self.lib:
            return
        for r in self.lib.pack_items(pid):
            lb = f"{r['show']} S{r['season']:02d}E{r['episode']:02d}" if r["kind"] == "tv" else r["show"]
            it = QListWidgetItem(f"{self.pack_list.count() + 1:>3}.  {lb}　{r['en']}" + (f"\n{'':>6}{r['zh']}" if r["zh"] else ""))
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
        self.open_link(key, idx)

    # ================================================================== 导出
    def style(self) -> Style:
        return Style.load(self.lib.get_setting("style"))

    def _export_dir(self):
        return self.lib.get_setting("export_dir", "") or str(self.lib.root / "导出")

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
        if self.cur and h.cue_id == self.cur.cue_id and self.cur_span:
            row = self._cue_row(h)
            s, e, _ = self.lib.final_span(row, h.media_key)
            if (abs(s - self.cur_span[0]) > 0.005 or abs(e - self.cur_span[1]) > 0.005) and \
                    QMessageBox.question(self, "导出", "选区改了还没保存，先保存微调再导出？") == QMessageBox.Yes:
                self.save_cut()
        dlg = ExportDialog(h.label, 1, str(Path(self._export_dir()) / "单条"), self, name=clip_name(h.en))
        if dlg.exec():
            spec = [dict(media_key=h.media_key, idx=h.idx, pattern=self.q.text().strip(), tier=h.tier,
                         matched=h.matched, name=dlg.name())]
            self._run_export(f"{h.label} · {h.en[:30]}", dlg.dir.text(), dlg.presets(), False, items=spec)

    def _run_export(self, title, out, presets, comp, pack_id=None, items=None):
        if not presets:
            return
        it = QListWidgetItem(f"⏳ {title}　准备中…")
        it.setData(Qt.UserRole, out)
        self.tasks.insertItem(0, it)
        self.nav[P_TASKS].setText("导出任务 ·")
        w = ExportWorker(str(self.lib.root), items or [], out, presets, self.style(), comp, pack_id)
        w.progress.connect(lambda n, t, s, it=it: it.setText(f"⏳ {title}　{n}/{t}　{s}"))
        w.finished_ok.connect(lambda r, it=it: self._export_done(it, title, r))
        self.export_workers.append(w)
        w.start()
        self.statusBar().showMessage(f"开始导出：{title}（进度在「导出任务」页）", 4000)

    def _export_done(self, it, title, r):
        if r.get("ok"):
            comp = "，合集 " + "、".join(r.get("compilations", {}).keys()) if r.get("compilations") else ""
            it.setText(f"✅ {title}　完成 {r.get('count', 0)} 条{comp}　（双击打开文件夹）")
        else:
            it.setText(f"⚠️ {title}　有错误：" + "；".join(r.get("errors", [])[:3]))
        it.setToolTip("\n".join(r.get("errors", [])))
        if not any(w.isRunning() for w in self.export_workers):
            self.nav[P_TASKS].setText("导出任务")
        self.statusBar().showMessage(f"导出完成：{title}", 5000)

    def _open_task(self, it):
        p = it.data(Qt.UserRole)
        if p and Path(p).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    # ================================================================== 设置
    def open_settings(self):
        if not self.lib:
            QMessageBox.information(self, APP, "先选择媒体库文件夹。")
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
            self.qs.setValue("align_limit", int(s.pop("align_limit", 80)))
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
            if self.cur:
                self.refresh_current()

    # ================================================================== 更新
    def check_update(self, manual: bool):
        if getattr(self, "_upd_check", None) and self._upd_check.isRunning():
            return
        self._upd_check = UpdateCheck()
        self._upd_check.done.connect(lambda r: self._update_checked(r, manual))
        self._upd_check.start()

    def _update_clicked(self):
        """更新按钮：已经查到新版就直接下载替换（一键）；还没查过就先查。"""
        from .. import updater
        r = self._upd_result
        if r and r.get("url") and updater.is_newer(r.get("version", "")):
            if updater.current_target() is None:
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
        self.upd_btn.setIcon(icon("update", "#FFFFFF" if hot else theme.ACCENT_D))
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
            return
        if r.get("error"):
            QMessageBox.warning(self, "检查更新", "连不上 GitHub：" + r["error"][:200])
            return
        ver = r.get("version", "")
        if not updater.is_newer(ver):
            QMessageBox.information(self, "检查更新", f"已经是最新版（{VERSION}）。")
            return
        if not r.get("url"):
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
        self.qs.setValue("geometry", self.saveGeometry())
        for sp in self.findChildren(QSplitter):
            self.qs.setValue("split107/" + sp.objectName(), sp.saveState())
        if self.align_worker:
            self.align_worker.stop()
            if not self.align_worker.wait(1500):
                self.align_worker.terminate()
                self.align_worker.wait(1000)
        super().closeEvent(e)


def light_palette(app):
    theme.apply_theme(app)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setOrganizationName("local33")
    light_palette(app)
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
