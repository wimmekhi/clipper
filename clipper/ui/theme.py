"""1.0.7：结构参照 quipclipper（顶栏导航 + 媒体库 / 搜索 / 工作台分页），配色沿用 1.0.6 的小清新：
1.0.6 小清新玻璃主题：薄荷 → 天蓝 → 淡紫的柔和底色，面板是半透明磨砂玻璃卡片，线条图标统一青绿色。
所有颜色只在这里定义；Win / Mac 一样的样子（不跟随系统深色模式）。"""
from __future__ import annotations

from PySide6.QtCore import Qt, QByteArray, QRectF, QPointF, QSize
from PySide6.QtGui import (QPalette, QColor, QIcon, QPixmap, QPainter, QLinearGradient, QRadialGradient,
                           QPainterPath)
from PySide6.QtWidgets import QApplication, QWidget, QFrame, QHBoxLayout, QLabel, QToolButton, QDockWidget

# ---------------------------------------------------------------- 色板
TEXT = "#1E293B"
TEXT2 = "#64748B"
TEXT3 = "#94A3B8"
ACCENT = "#14B8A6"        # 青绿
ACCENT_D = "#0F766E"
SKY = "#38BDF8"
CORAL = "#F43F5E"         # 命中高亮 / 播放头
LINE = "rgba(15,118,110,0.14)"
GLASS = "rgba(255,255,255,0.58)"
GLASS_HI = "rgba(255,255,255,0.88)"
GLASS_EDGE = "rgba(255,255,255,0.92)"

TIER_COLOR = {"原句型": "#10B981", "近似变体": "#F59E0B", "功能等价": "#8B5CF6"}
TIMING_TEXT = {"aligned": ("已对齐", "#10B981"), "estimated": ("估算", "#F59E0B"),
               "subtitle": ("字幕时间", "#64748B"), "manual": ("已微调", "#0EA5E9")}
STATUS_COLOR = {"done": "#059669", "error": "#E11D48", "running": "#0284C7", "queued": "#7C3AED"}

QSS = f"""
* {{ font-size: 13px; color: {TEXT}; }}
QToolTip {{ background: #FFFFFF; color: {TEXT}; border: 1px solid {LINE}; border-radius: 6px; padding: 4px 8px; }}
QDialog {{ background: #F1F8F7; }}
QLabel#muted {{ color: {TEXT2}; font-size: 12px; }}
QLabel#small {{ color: {TEXT2}; font-size: 11px; }}

/* 输入 */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: rgba(255,255,255,0.82); border: 1px solid {LINE}; border-radius: 8px; padding: 5px 8px;
    selection-background-color: #99F6E4; selection-color: {TEXT}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit#bigsearch {{ font-size: 15px; padding: 9px 16px; border-radius: 19px; background: rgba(255,255,255,0.9); }}
QComboBox {{ padding-right: 22px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; border: none; width: 20px; }}
QComboBox::down-arrow {{ image: url(@ARROW_DOWN@); width: 12px; height: 12px; margin-right: 6px; }}
QSpinBox, QDoubleSpinBox {{ padding-right: 18px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 18px; border: none; background: transparent; margin-top: 3px; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 18px; border: none; background: transparent; margin-bottom: 3px; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url(@ARROW_UP@); width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url(@ARROW_DOWN@); width: 10px; height: 10px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover, QDoubleSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover {{ background: rgba(20,184,166,0.12); border-radius: 4px; }}
QComboBox QAbstractItemView {{ background: #FFFFFF; border: 1px solid {LINE}; border-radius: 8px;
    selection-background-color: #CCFBF1; selection-color: {TEXT}; outline: 0; }}

/* 按钮 */
QPushButton {{ background: rgba(255,255,255,0.72); border: 1px solid {LINE}; border-radius: 9px; padding: 5px 13px; }}
QPushButton:hover {{ background: #FFFFFF; border-color: rgba(20,184,166,0.55); }}
QPushButton:pressed {{ background: #E6FAF6; }}
QPushButton:disabled {{ color: {TEXT3}; background: rgba(255,255,255,0.4); }}
QPushButton#primary {{ color: #FFFFFF; font-weight: 600; border: none; padding: 6px 18px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2DD4BF, stop:1 #38BDF8); }}
QPushButton#primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #14B8A6, stop:1 #0EA5E9); }}
QPushButton#bigsearch {{ border-radius: 19px; padding: 9px 22px; font-size: 14px; }}
QPushButton#chip {{ border-radius: 13px; padding: 3px 12px; background: rgba(255,255,255,0.55); color: {TEXT2}; }}
QPushButton#chip:checked {{ background: #CCFBF1; color: {ACCENT_D}; border: 1px solid rgba(20,184,166,0.6); font-weight: 600; }}
QPushButton#ghost {{ background: transparent; border: 1px solid transparent; color: {ACCENT_D}; padding: 3px 9px; border-radius: 8px; }}
QPushButton#ghost:hover {{ background: rgba(20,184,166,0.12); }}
QPushButton#ghost:disabled {{ color: {TEXT3}; }}

/* 列表 / 树 / 表 */
QListWidget, QTreeWidget, QTableWidget {{ background: transparent; border: none; outline: 0; }}
QTreeWidget::item, QListWidget::item {{ padding: 3px 2px; border-radius: 6px; }}
QTreeWidget::item:hover, QListWidget::item:hover {{ background: rgba(20,184,166,0.08); }}
QTreeWidget::item:selected, QListWidget::item:selected, QTableWidget::item:selected {{
    background: rgba(20,184,166,0.20); color: {TEXT}; }}
QTableWidget {{ background: rgba(255,255,255,0.7); border: 1px solid {LINE}; border-radius: 10px; gridline-color: rgba(15,118,110,0.08); }}
QHeaderView::section {{ background: transparent; color: {TEXT2}; border: none; border-bottom: 1px solid {LINE}; padding: 4px 6px; font-size: 12px; }}
QListWidget#results::item, QListWidget#results::item:hover, QListWidget#results::item:selected {{ background: transparent; }}

/* 标签 */
QTabBar::tab {{ background: transparent; color: {TEXT2}; padding: 4px 12px; margin: 2px 2px; border-radius: 11px; border: none; }}
QTabBar::tab:hover {{ color: {TEXT}; background: rgba(255,255,255,0.55); }}
QTabBar::tab:selected {{ background: #FFFFFF; color: {ACCENT_D}; font-weight: 600; }}
QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 10px; background: rgba(255,255,255,0.6); top: -1px; }}

/* 进度 */
QProgressBar {{ background: rgba(15,118,110,0.10); border: none; border-radius: 4px; text-align: center; color: {TEXT2}; }}
QProgressBar::chunk {{ border-radius: 4px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2DD4BF, stop:1 #38BDF8); }}

/* 滚动条：细圆 */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: rgba(100,116,139,0.30); border-radius: 3px; min-height: 30px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: rgba(100,116,139,0.5); }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* 菜单 */
QMenuBar {{ background: transparent; }}
QMenuBar::item {{ background: transparent; padding: 4px 10px; border-radius: 6px; }}
QMenuBar::item:selected {{ background: rgba(255,255,255,0.7); }}
QMenu {{ background: #FFFFFF; border: 1px solid {LINE}; border-radius: 10px; padding: 5px; }}
QMenu::item {{ padding: 5px 22px 5px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: #CCFBF1; color: {ACCENT_D}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 8px; }}

/* 主窗口骨架：透明，透出底下的渐变 */
QMainWindow::separator {{ background: transparent; width: 8px; height: 8px; }}
QMainWindow::separator:hover {{ background: rgba(20,184,166,0.25); }}
QToolBar {{ background: transparent; border: none; spacing: 2px; padding: 4px 10px; }}
QToolBar::separator {{ background: {LINE}; width: 1px; margin: 7px 6px; }}
QToolBar QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 9px; padding: 4px 7px; color: {TEXT}; }}
QToolBar QToolButton:hover {{ background: rgba(255,255,255,0.75); border-color: rgba(255,255,255,0.95); }}
QToolBar QToolButton:pressed {{ background: #E6FAF6; }}
QToolButton#update {{ border-radius: 13px; padding: 4px 12px; background: rgba(255,255,255,0.65); border: 1px solid {LINE}; }}
QToolButton#update[hot="true"] {{ color: #FFFFFF; font-weight: 600; border: none;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2DD4BF, stop:1 #38BDF8); }}
QStatusBar {{ background: transparent; color: {TEXT2}; }}
QStatusBar::item {{ border: none; }}

/* 玻璃卡片（停靠面板、搜索区） */
QFrame#glass {{ background: {GLASS}; border: 1px solid {GLASS_EDGE}; border-radius: 16px; }}
QFrame#glassTop {{ background: {GLASS}; border: 1px solid {GLASS_EDGE}; border-bottom: none;
    border-top-left-radius: 16px; border-top-right-radius: 16px; }}
QFrame#glassBody {{ background: {GLASS}; border: 1px solid {GLASS_EDGE}; border-top: none;
    border-bottom-left-radius: 16px; border-bottom-right-radius: 16px; }}
QLabel#dockTitle {{ font-weight: 600; color: {TEXT}; font-size: 13px; background: transparent; }}
QToolButton#dockBtn {{ background: transparent; border: none; border-radius: 6px; padding: 2px; }}
QToolButton#dockBtn:hover {{ background: rgba(15,118,110,0.10); }}
QFrame#card {{ background: rgba(255,255,255,0.80); border: 1px solid rgba(255,255,255,0.95); border-radius: 14px; }}
QFrame#card:hover {{ border: 1px solid rgba(20,184,166,0.55); background: rgba(255,255,255,0.94); }}
QFrame#videoFrame {{ background: #0F172A; border-radius: 12px; }}

/* ---------------- 1.0.7 页面式布局 ---------------- */
QFrame#topbar {{ background: rgba(255,255,255,0.86); border: none; border-bottom: 1px solid {LINE}; }}
QLabel#brand {{ font-size: 15px; font-weight: 700; color: {TEXT}; }}
QFrame#searchbox {{ background: #FFFFFF; border: 1px solid rgba(15,118,110,0.22); border-radius: 10px; }}
QLineEdit#topsearch {{ border: none; background: transparent; font-size: 14px; padding: 6px 4px; }}
QComboBox#scope {{ background: #CCFBF1; color: {ACCENT_D}; border: none; border-radius: 7px; font-weight: 600;
    font-size: 12px; padding: 3px 22px 3px 10px; }}
QLabel#kbd {{ border: 1px solid rgba(15,118,110,0.25); border-radius: 5px; padding: 0 6px; color: {TEXT2};
    font-family: Menlo, Consolas, monospace; font-size: 11px; background: #F8FAFC; }}
QToolButton#nav {{ border-radius: 9px; padding: 5px 12px; color: {TEXT2}; background: transparent; border: 1px solid transparent; }}
QToolButton#nav:hover {{ color: {TEXT}; background: rgba(255,255,255,0.8); }}
QToolButton#nav:checked {{ background: #FFFFFF; color: {TEXT}; font-weight: 600; border: 1px solid {LINE}; }}
QToolButton#topbtn {{ border-radius: 9px; padding: 5px 8px; background: transparent; border: 1px solid transparent; }}
QToolButton#topbtn:hover {{ background: rgba(255,255,255,0.85); border-color: {LINE}; }}
QFrame#panel {{ background: rgba(255,255,255,0.80); border: 1px solid rgba(15,118,110,0.12); border-radius: 12px; }}
QFrame#sidebar {{ background: transparent; }}
QLabel#h1 {{ font-size: 21px; font-weight: 700; color: {TEXT}; }}
QLabel#h2 {{ font-size: 14px; font-weight: 700; color: {TEXT}; }}
QLabel#eyebrow {{ font-size: 11px; color: {TEXT2}; font-weight: 700; letter-spacing: 1px; }}
QLabel#mono {{ font-family: Menlo, Consolas, monospace; color: {ACCENT_D}; font-size: 12px; }}
QLabel#crumb {{ font-size: 14px; color: {TEXT2}; }}
QFrame#banner {{ background: #FFFBEB; border: 1px solid #FDE68A; border-left: 4px solid #F59E0B; border-radius: 10px; }}
QFrame#grouphdr {{ background: rgba(226,240,238,0.85); border-radius: 10px; }}
QFrame#grouphdr:hover {{ background: rgba(204,251,241,0.9); }}
QFrame#row {{ background: rgba(255,255,255,0.88); border-radius: 10px; border: 1px solid transparent; }}
QFrame#row:hover {{ border: 1px solid rgba(20,184,166,0.35); }}
QFrame#row[sel="true"] {{ background: #F0FDFA; border: 1px solid rgba(20,184,166,0.55); border-left: 3px solid {ACCENT}; }}
QLabel#pct {{ background: rgba(15,118,110,0.08); border-radius: 9px; padding: 2px 8px; color: {TEXT2};
    font-size: 11px; font-family: Menlo, Consolas, monospace; }}
QPushButton#iconBtn {{ padding: 0; border-radius: 8px; }}
QPushButton#iconPrimary {{ padding: 0; border-radius: 8px; border: none;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2DD4BF, stop:1 #38BDF8); }}
QPushButton#iconPrimary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #14B8A6, stop:1 #0EA5E9); }}
QFrame#batchbar {{ background: rgba(255,255,255,0.94); border: 1px solid {LINE}; border-radius: 12px; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:hover {{ background: rgba(20,184,166,0.22); border-radius: 3px; }}
QListWidget#script::item {{ padding: 6px 8px; border-radius: 0; border-bottom: 1px solid rgba(15,118,110,0.07); }}
QListWidget#script::item:selected {{ background: #F0FDFA; color: {TEXT}; border-left: 3px solid {ACCENT}; }}
QTreeWidget#episodes::item {{ padding: 7px 4px; }}
QTreeWidget#shows::item {{ padding: 6px 6px; border-radius: 8px; }}
QTreeWidget#shows::item:selected {{ background: rgba(20,184,166,0.16); color: {TEXT}; font-weight: 600; }}
"""


def apply_theme(app: QApplication):
    """固定浅色：否则 Mac 深色模式下字是白的、背景是浅的，看不见。"""
    app.setStyle("Fusion")
    p = QPalette()
    C = QPalette.ColorRole
    for role, c in [(C.Window, "#EEF7F5"), (C.WindowText, TEXT), (C.Base, "#FFFFFF"),
                    (C.AlternateBase, "#F4FBFA"), (C.Text, TEXT), (C.Button, "#FFFFFF"),
                    (C.ButtonText, TEXT), (C.BrightText, CORAL), (C.Highlight, ACCENT),
                    (C.HighlightedText, "#FFFFFF"), (C.ToolTipBase, "#FFFFFF"), (C.ToolTipText, TEXT),
                    (C.PlaceholderText, TEXT3), (C.Link, "#0EA5E9"), (C.Light, "#FFFFFF"),
                    (C.Midlight, "#E2F3F0"), (C.Mid, "#CBE4E0"), (C.Dark, "#94A3B8"), (C.Shadow, "#64748B")]:
        p.setColor(role, QColor(c))
    for role in (C.WindowText, C.Text, C.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(TEXT3))
    app.setPalette(p)
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except Exception:
        pass
    app.setStyleSheet(_qss_with_arrows())


def _qss_with_arrows() -> str:
    """下拉框 / 数字框的小箭头：QSS 只认图片文件，这里现画成 png 放临时目录（不依赖 svg 插件）。"""
    import os
    import tempfile
    d = os.path.join(tempfile.gettempdir(), "clipper-theme-106")
    os.makedirs(d, exist_ok=True)
    paths = {}
    for nm in ("chev-down", "chev-up"):
        f = os.path.join(d, nm + ".png")
        if not os.path.exists(f):
            icon(nm, TEXT2, 24).pixmap(QSize(24, 24), 2.0).save(f)
        paths[nm] = f.replace("\\", "/")
    return QSS.replace("@ARROW_DOWN@", paths["chev-down"]).replace("@ARROW_UP@", paths["chev-up"])


def paint_backdrop(p: QPainter, rect):
    """主窗口底色：薄荷 → 天蓝 → 淡紫的斜向渐变，再叠几团柔光，半透明面板压上去就有磨砂玻璃的感觉。"""
    W, H = rect.width(), rect.height()
    g = QLinearGradient(0, 0, W, H)
    # 1.0.7：底色收淡，让内容卡片更清楚（更「专业」），只留一点薄荷 / 天蓝的气氛
    g.setColorAt(0.0, QColor("#EEF8F5"))
    g.setColorAt(0.5, QColor("#F1F6FB"))
    g.setColorAt(1.0, QColor("#F4F2FA"))
    p.fillRect(rect, g)
    for cx, cy, r, col in ((0.08, 0.05, 0.55, QColor(94, 234, 212, 45)),
                           (0.92, 0.18, 0.50, QColor(125, 211, 252, 40)),
                           (0.55, 1.05, 0.60, QColor(196, 181, 253, 32))):
        rg = QRadialGradient(QPointF(cx * W, cy * H), r * max(W, H) * 0.7)
        rg.setColorAt(0.0, col)
        c2 = QColor(col); c2.setAlpha(0)
        rg.setColorAt(1.0, c2)
        p.fillRect(rect, rg)


# ---------------------------------------------------------------- 线条图标（24×24，2px 圆头描边）
_ICONS = {
    "folder": '<path d="M3 7.5A2 2 0 0 1 5 5.5h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "refresh": '<path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4.5v5h-5"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>',
    "play": '<path d="M8 5.5v13l10.5-6.5z"/>',
    "prev": '<path d="M18 6v12l-8.5-6z"/><path d="M6 6v12"/>',
    "next": '<path d="M6 6v12l8.5-6z"/><path d="M18 6v12"/>',
    "bookmark": '<path d="M7 4h10a1 1 0 0 1 1 1v15l-6-3.8L6 20V5a1 1 0 0 1 1-1z"/>',
    "collect": '<rect x="4" y="4" width="16" height="16" rx="4"/><path d="M12 8.5v7M8.5 12h7"/>',
    "export": '<path d="M12 4v11"/><path d="M7.5 10.5L12 15l4.5-4.5"/><path d="M5 19.5h14"/>',
    "newpack": '<path d="M3 7.5A2 2 0 0 1 5 5.5h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
               '<path d="M12 10.5v6M9 13.5h6"/>',
    "package": '<path d="M12 3l8 4v10l-8 4-8-4V7z"/><path d="M4 7l8 4 8-4"/><path d="M12 11v10"/>',
    "wave": '<path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/>',
    "settings": '<path d="M4 7h9M17 7h3M4 12h3M11 12h9M4 17h11M19 17h1"/>'
                '<circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17" r="2"/>',
    "update": '<circle cx="12" cy="12" r="8.5"/><path d="M12 16.5v-9"/><path d="M8.5 11L12 7.5l3.5 3.5"/>',
    "layout": '<rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="M9 4.5v15M9 11h11.5"/>',
    "float": '<rect x="4" y="8" width="12" height="12" rx="2.5"/><path d="M9 4h9a2 2 0 0 1 2 2v9"/>',
    "chev-down": '<path d="M6.5 9.5l5.5 5.5 5.5-5.5"/>',
    "chev-up": '<path d="M6.5 14.5L12 9l5.5 5.5"/>',
    "close": '<path d="M7 7l10 10M17 7L7 17"/>',
    "loop": '<path d="M17 3.5l3 3-3 3"/><path d="M4 11.5v-1a4 4 0 0 1 4-4h12"/><path d="M7 20.5l-3-3 3-3"/>'
            '<path d="M20 12.5v1a4 4 0 0 1-4 4H4"/>',
    "context": '<path d="M5 6h14M5 12h14M5 18h9"/>',
    "film": '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M7.5 5v14M16.5 5v14M3.5 9.5h4M3.5 14.5h4M16.5 9.5h4M16.5 14.5h4"/>',
    "star": '<path d="M12 4l2.5 5.1 5.6.8-4.05 3.95.95 5.6L12 16.8l-5 2.65.95-5.6L3.9 9.9l5.6-.8z"/>',
    "scissors": '<circle cx="6.5" cy="7" r="2.5"/><circle cx="6.5" cy="17" r="2.5"/><path d="M8.6 8.4L20 17M8.6 15.6L20 7"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "back": '<path d="M19 12H5"/><path d="M11 6l-6 6 6 6"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "fit": '<path d="M8 4H5a1 1 0 0 0-1 1v3M16 4h3a1 1 0 0 1 1 1v3M8 20H5a1 1 0 0 1-1-1v-3M16 20h3a1 1 0 0 0 1-1v-3"/>',
    "library": '<path d="M5 4.5v15M9.5 4.5v15"/><path d="M14 5.5l4.5-1 2 14.5-4.5 1z"/>',
    "script": '<path d="M7 3.5h10a1 1 0 0 1 1 1v15a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1z"/><path d="M9 8h6M9 12h6M9 16h4"/>',
    "trash": '<path d="M4.5 7h15"/><path d="M9.5 7V5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2"/>'
             '<path d="M6.5 7l1 12a1.5 1.5 0 0 0 1.5 1.4h6a1.5 1.5 0 0 0 1.5-1.4l1-12"/><path d="M10 11v5.5M14 11v5.5"/>',
    "edit": '<path d="M15.5 5l3.5 3.5L9 18.5H5.5V15z"/><path d="M13.5 7l3.5 3.5"/>',
    "log": '<path d="M7 3.5h7l4 4v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1z"/><path d="M9 12h6M9 16h6"/>',
}


def icon(name: str, color: str = ACCENT_D, size: int = 20, fill: str | None = None) -> QIcon:
    from PySide6.QtSvg import QSvgRenderer
    body = _ICONS[name]
    fill = fill or (color if name == "play" else "none")
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="{fill}" stroke="{color}" '
           f'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    r = QSvgRenderer(QByteArray(svg.encode()))
    ic = QIcon()
    for scale in (1, 2, 3):
        pm = QPixmap(size * scale, size * scale)
        pm.fill(Qt.transparent)
        pa = QPainter(pm)
        pa.setRenderHint(QPainter.Antialiasing)
        r.render(pa, QRectF(0, 0, size * scale, size * scale))
        pa.end()
        pm.setDevicePixelRatio(scale)
        ic.addPixmap(pm)
    return ic


# ---------------------------------------------------------------- 玻璃停靠面板
class GlassTitle(QFrame):
    """停靠面板的标题栏：玻璃卡片的上半截。拖标题可移动面板（Qt 自己处理），双击浮出/收回。"""

    def __init__(self, dock: QDockWidget):
        super().__init__(dock)
        self.dock = dock
        self.setObjectName("glassTop")
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 7, 8, 3)
        h.setSpacing(2)
        self.lb = QLabel(dock.windowTitle())
        self.lb.setObjectName("dockTitle")
        self.lb.setAttribute(Qt.WA_TransparentForMouseEvents)
        h.addWidget(self.lb, 1)
        for nm, tip, fn in (("float", "浮出 / 收回", lambda: dock.setFloating(not dock.isFloating())),
                            ("close", "关闭（窗口菜单里可再打开）", dock.close)):
            b = QToolButton(self)
            b.setObjectName("dockBtn")
            b.setIcon(icon(nm, TEXT3, 14))
            b.setIconSize(QSize(14, 14))
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            h.addWidget(b)
        dock.windowTitleChanged.connect(self.lb.setText)

    def mouseDoubleClickEvent(self, e):
        self.dock.setFloating(not self.dock.isFloating())

    def mousePressEvent(self, e):
        e.ignore()      # 交给 QDockWidget 处理拖动

    def mouseMoveEvent(self, e):
        e.ignore()

    def mouseReleaseEvent(self, e):
        e.ignore()


def glass_body(widget: QWidget, margins=(10, 4, 10, 10)) -> QFrame:
    f = QFrame()
    f.setObjectName("glassBody")
    from PySide6.QtWidgets import QVBoxLayout
    v = QVBoxLayout(f)
    v.setContentsMargins(*margins)
    v.addWidget(widget)
    return f

