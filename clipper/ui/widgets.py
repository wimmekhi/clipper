"""自绘控件：结果卡片、切点波形条。"""
from __future__ import annotations

import html
import re

import numpy as np

from PySide6.QtCore import Qt, Signal, QRectF, QPointF, QRect, QPoint, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QPainterPath, QBrush
from PySide6.QtWidgets import (QLayout, QWidget, QLabel, QHBoxLayout, QVBoxLayout, QPushButton, QFrame,
                               QSizePolicy, QStyledItemDelegate)

from .theme import ACCENT, TIER_COLOR, TIMING_TEXT, CORAL, TEXT, TEXT2, ACCENT_D


def fmt_ms(t: float) -> str:
    ms = int(round(max(0, t) * 1000))
    return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}"


def highlight_html(en: str, matched: str) -> str:
    text = html.escape(en)
    if not matched:
        return text
    words = matched.split()
    pat = r"\b" + r"[\W_]+".join(re.escape(html.escape(w)) for w in words) + r"\b"
    return re.sub(pat, lambda m: f'<span style="color:#F43F5E;font-weight:700">{m.group(0)}</span>', text,
                  count=1, flags=re.I)


def badge(text, color):
    lb = QLabel(text)
    c = QColor(color)
    lb.setStyleSheet(f"QLabel{{color:{color};background:rgba({c.red()},{c.green()},{c.blue()},0.13);border:none;"
                     f"border-radius:8px;padding:1px 7px;font-size:11px;font-weight:600;}}")
    lb.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return lb


class FitLabel(QLabel):
    """放不下就整段不画（不显示被截半截的时间码）。"""

    def paintEvent(self, e):
        if self.fontMetrics().horizontalAdvance(self.text()) <= self.width():
            super().paintEvent(e)


class ResultCard(QFrame):
    preview = Signal(object)
    collect = Signal(object)
    context = Signal(object)
    export_one = Signal(object)

    def __init__(self, hit, timing: str, span: tuple[float, float], parent=None):
        super().__init__(parent)
        self.hit = hit
        self.setObjectName("card")
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(badge("剧集" if hit.kind == "tv" else "电影", "#64748B"))
        top.addWidget(badge(hit.tier, TIER_COLOR.get(hit.tier, "#64748B")))
        tt, tc = TIMING_TEXT.get(timing, ("", "#999"))
        top.addWidget(badge(tt, tc))
        if not hit.has_video:
            top.addWidget(badge("无视频", "#E11D48"))
        if hit.speakers > 1:
            top.addWidget(badge("多人", "#94A3B8"))
        name = QLabel(f"<b>{html.escape(hit.label)}</b>")
        name.setToolTip(hit.label)
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)   # 窄的时候剧名被截，不撑宽卡片
        top.addWidget(name, 1)
        v.addLayout(top)
        en = QLabel(highlight_html(hit.en, hit.matched))
        en.setWordWrap(True)
        en.setStyleSheet("font-size:15px;color:#1E293B;")
        v.addWidget(en)
        if hit.zh:
            zh = QLabel(html.escape(hit.zh))
            zh.setWordWrap(True)
            zh.setStyleSheet("font-size:12px;color:#64748B;")
            v.addWidget(zh)
        btns = QHBoxLayout()
        btns.setSpacing(4)
        for text, sig in (("预览", self.preview), ("采集", self.collect), ("上下文", self.context),
                          ("导出片段", self.export_one)):
            b = QPushButton(text)
            b.setCursor(Qt.PointingHandCursor)
            b.setObjectName("primary" if text == "预览" else "ghost")
            if text == "预览":
                b.setStyleSheet("padding:3px 14px;border-radius:12px;")
            if text == "预览" and not hit.has_video:
                b.setEnabled(False)
            b.clicked.connect(lambda _=False, s=sig: s.emit(self.hit))
            btns.addWidget(b)
        btns.addStretch(1)
        tm = FitLabel(f"{fmt_ms(span[0])} - {fmt_ms(span[1])}")
        tm.setStyleSheet("color:#94A3B8;font-family:Menlo,Consolas,monospace;font-size:11px;")
        tm.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        tm.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        btns.addWidget(tm, 1)
        v.addLayout(btns)

    def mouseDoubleClickEvent(self, e):
        if self.hit.has_video:
            self.preview.emit(self.hit)


class WaveformBar(QWidget):
    """显示片段附近的波形、识别出的词、原字幕时间段和当前切点；两端可拖，默认吸附词边界（按住 Shift 不吸附）。"""
    changed = Signal(float, float)
    seek = Signal(float)

    HANDLE = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setMouseTracking(True)
        self.t0 = 0.0
        self.t1 = 1.0
        self.peaks = None
        self.words = []
        self.sub = None          # (start,end) 原字幕
        self.cut = None          # (start,end) 当前切点
        self.playhead = None
        self._drag = None

    def set_data(self, t0, t1, words, sub, cut):
        self.t0, self.t1 = t0, max(t0 + 0.1, t1)
        self.words = [w for w in words if w["end"] >= t0 and w["start"] <= t1]
        self.sub, self.cut = sub, cut
        self.peaks = None
        self.update()

    def set_peaks(self, p):
        self.peaks = p
        self.update()

    def set_playhead(self, t):
        self.playhead = t
        self.update()

    # 坐标
    def x_of(self, t):
        return (t - self.t0) / (self.t1 - self.t0) * self.width()

    def t_of(self, x):
        return self.t0 + x / max(1, self.width()) * (self.t1 - self.t0)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        bg = QPainterPath(); bg.addRoundedRect(QRectF(self.rect()), 10, 10)
        p.fillPath(bg, QColor(255, 255, 255, 170))
        wave_top, wave_h = 18, H - 50
        mid = wave_top + wave_h / 2
        if self.sub:
            x0, x1 = self.x_of(self.sub[0]), self.x_of(self.sub[1])
            p.fillRect(QRectF(x0, 2, x1 - x0, 12), QColor(148, 163, 184, 60))
            p.setPen(QColor("#6B7280"))
            f = QFont(); f.setPointSize(8); p.setFont(f)
            p.drawText(QRectF(x0 + 3, 1, max(40, x1 - x0), 14), Qt.AlignVCenter | Qt.AlignLeft, "原字幕")
        if self.cut:
            x0, x1 = self.x_of(self.cut[0]), self.x_of(self.cut[1])
            p.fillRect(QRectF(x0, wave_top, x1 - x0, wave_h), QColor(45, 212, 191, 55))
        if self.peaks is not None and len(self.peaks):
            n = len(self.peaks)
            pen = QPen(QColor("#5EB8C9")); pen.setWidthF(1)
            p.setPen(pen)
            for i in range(n):
                x = i / n * W
                a = float(self.peaks[i]) * wave_h / 2
                p.drawLine(QPointF(x, mid - a), QPointF(x, mid + a))
        else:
            p.setPen(QColor("#9CA3AF"))
            p.drawText(QRectF(0, wave_top, W, wave_h), Qt.AlignCenter, "波形加载中…")
        # 词
        f = QFont(); f.setPointSize(8); p.setFont(f)
        fm = p.fontMetrics()
        row_end = [-1e9, -1e9]
        for w in self.words:
            x0, x1 = self.x_of(w["start"]), self.x_of(w["end"])
            p.setPen(QPen(QColor("#CBD5E1"), 1))
            p.drawLine(QPointF(x0, H - 28), QPointF(x0, H - 2))
            p.fillRect(QRectF(x0, H - 28, max(1, x1 - x0), 26), QColor(148, 163, 184, 45))
            tw = fm.horizontalAdvance(w["w"]) + 4
            r = 0 if row_end[0] <= x0 else 1 if row_end[1] <= x0 else None
            if r is None:
                continue
            row_end[r] = x0 + tw
            p.setPen(QColor("#334155"))
            p.drawText(QRectF(x0 + 2, H - 28 + r * 13, tw, 13), Qt.AlignVCenter | Qt.AlignLeft, w["w"])
        # 切点手柄
        if self.cut:
            for t in self.cut:
                x = self.x_of(t)
                p.setPen(QPen(QColor("#0D9488"), 2))
                p.drawLine(QPointF(x, wave_top - 2), QPointF(x, wave_top + wave_h + 2))
                p.setBrush(QBrush(QColor("#0D9488")))
                p.drawRoundedRect(QRectF(x - self.HANDLE / 2, wave_top - 5, self.HANDLE, 12), 3, 3)
        if self.playhead is not None and self.t0 <= self.playhead <= self.t1:
            x = self.x_of(self.playhead)
            p.setPen(QPen(QColor("#F43F5E"), 1.5))
            p.drawLine(QPointF(x, 0), QPointF(x, H))
        p.end()

    def _snap(self, t, shift):
        if shift or not self.words:
            return t
        cands = []
        for w in self.words:
            cands += [w["start"], w["end"]]
        best = min(cands, key=lambda c: abs(c - t))
        if abs(self.x_of(best) - self.x_of(t)) < 14:
            return best
        return t

    def mousePressEvent(self, e):
        if not self.cut:
            return
        x = e.position().x()
        d0, d1 = abs(x - self.x_of(self.cut[0])), abs(x - self.x_of(self.cut[1]))
        if min(d0, d1) <= 10:
            self._drag = 0 if d0 <= d1 else 1
        else:
            self.seek.emit(self.t_of(x))

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self._drag is None:
            if self.cut and min(abs(x - self.x_of(self.cut[0])), abs(x - self.x_of(self.cut[1]))) <= 10:
                self.setCursor(Qt.SizeHorCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
            return
        t = self._snap(self.t_of(x), bool(e.modifiers() & Qt.ShiftModifier))
        a, b = self.cut
        if self._drag == 0:
            a = min(t, b - 0.1)
        else:
            b = max(t, a + 0.1)
        self.cut = (max(self.t0, a), min(self.t1, b))
        self.update()

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            self.changed.emit(*self.cut)


class FlowLayout(QLayout):
    """放不下就自动换行的横排布局：窄窗口时按钮折到下一行，不再把面板撑死、拖不动。"""

    def __init__(self, parent=None, spacing=6, margins=(0, 0, 0, 0)):
        super().__init__(parent)
        self._items = []
        self._sp = spacing
        self.setContentsMargins(*margins)

    def addItem(self, item):
        self._items.append(item)

    def addStretch(self, *_):
        pass

    def addSpacing(self, *_):
        pass

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, r):
        super().setGeometry(r)
        self._do(r, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, rect, test):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            if it.widget() is not None and it.widget().isHidden():
                continue
            hint = it.sizeHint()
            nx = x + hint.width() + self._sp
            if nx - self._sp > r.right() + 1 and line_h > 0:
                x = r.x(); y += line_h + self._sp
                nx = x + hint.width() + self._sp
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x = nx
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


# ====================================================================== 1.0.7
def fmt_short(t: float) -> str:
    """台词本 / 标尺用的短时间：1:23.4 或 1:02:03.4"""
    t = max(0.0, t)
    h, m, s = int(t // 3600), int(t // 60 % 60), t % 60
    return f"{h}:{m:02d}:{s:04.1f}" if h else f"{m}:{s:04.1f}"


MONO = "Menlo,Consolas,'SF Mono',monospace"


class WaveformView(QWidget):
    """工作台的大波形：可缩放 / 平移，上面一条显示台词（点一下选中这句，Shift 点加选），
    中间波形 + 选区（两端拖动），下面识别出的词和时间刻度。

    吸附（按住 Alt / Option 临时不吸附）：词边界、字幕边、静音点、过零点，可分别开关。
    操作：拖两端改切点；在空白处拖出新选区；单击跳到那里播放；滚轮平移，Ctrl/⌘+滚轮缩放。"""
    changed = Signal(float, float)
    seek = Signal(float)
    pick_line = Signal(int, bool)       # idx, extend
    view_changed = Signal(float, float)

    LANE = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(104)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.t0, self.t1 = 0.0, 1.0      # 已加载的音频范围
        self.v0, self.v1 = 0.0, 1.0      # 当前可见范围
        self.samples = None
        self.sr = 8000
        self.s_t0 = 0.0
        self._norm = 1.0
        self.words, self.lines = [], []
        self.sub = None
        self.cut = None
        self.playhead = None
        self.snap = {"word", "cue", "silence"}
        self._drag = None                # 0/1 拖手柄，"new" 拖新选区，"pan"
        self._press = None

    # ---------------- 数据
    def set_data(self, t0, t1, words, lines, sub, cut):
        self.t0, self.t1 = t0, max(t0 + 0.2, t1)
        self.words = [w for w in words if w["end"] >= t0 and w["start"] <= t1]
        self.lines = [l for l in lines if l[1] >= t0 and l[0] <= t1]
        self.sub, self.cut = sub, cut
        self.samples = None
        self.fit_selection(emit=False)
        self.update()

    def set_samples(self, arr, t_start, sr=8000):
        self.samples, self.s_t0, self.sr = arr, t_start, sr
        self._norm = float(np.abs(arr).max()) if arr is not None and len(arr) else 1.0
        self._norm = self._norm or 1.0
        self.update()

    def set_cut(self, cut):
        self.cut = cut
        self.update()

    def set_playhead(self, t):
        self.playhead = t
        if t is not None and self.cut and (t > self.v1 or t < self.v0) and self.t0 <= t <= self.t1:
            w = self.v1 - self.v0
            self._set_view(t - w * 0.1, t + w * 0.9)
        self.update()

    # ---------------- 视图
    def span(self):
        return self.v1 - self.v0

    def _set_view(self, a, b, emit=True):
        w = max(0.02, min(b - a, self.t1 - self.t0))
        a = max(self.t0, min(a, self.t1 - w))
        self.v0, self.v1 = a, a + w
        self.update()
        if emit:
            self.view_changed.emit(self.v0, self.v1)

    def zoom(self, f, center=None):
        c = center if center is not None else (self.v0 + self.v1) / 2
        w = self.span() * f
        r = (c - self.v0) / max(1e-6, self.span())
        self._set_view(c - w * r, c - w * r + w)

    def fit_selection(self, emit=True):
        if self.cut:
            a, b = self.cut
            m = max(0.25, (b - a) * 0.12)
            self._set_view(a - m, b + m, emit)
        else:
            self._set_view(self.t0, self.t1, emit)

    def fit_whole(self):
        self._set_view(self.t0, self.t1)

    # ---------------- 坐标
    def x_of(self, t):
        return (t - self.v0) / max(1e-6, self.span()) * self.width()

    def t_of(self, x):
        return self.v0 + x / max(1, self.width()) * self.span()

    def _geom(self):
        H = self.height()
        top = self.LANE + 2
        bottom = H - 40
        return top, bottom

    # ---------------- 绘制
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        bg = QPainterPath(); bg.addRoundedRect(QRectF(self.rect()), 10, 10)
        p.fillPath(bg, QColor(255, 255, 255, 215))
        top, bottom = self._geom()
        mid = (top + bottom) / 2
        hw = (bottom - top) / 2
        f = QFont(); f.setPointSize(8); p.setFont(f)
        fm = p.fontMetrics()
        # 台词条
        for (s, e, text, idx, src) in self.lines:
            x0, x1 = self.x_of(s), self.x_of(e)
            if x1 < 0 or x0 > W:
                continue
            col = QColor("#8B5CF6") if src == "auto" else QColor("#64748B")
            fill = QColor(col); fill.setAlpha(28)
            p.fillRect(QRectF(x0, 2, max(2, x1 - x0), self.LANE - 4), fill)
            p.setPen(QPen(col, 1)); p.drawLine(QPointF(x0, 2), QPointF(x0, self.LANE - 2))
            t = ("识别 · " if src == "auto" else "") + text
            t = fm.elidedText(t, Qt.ElideRight, int(max(0, x1 - x0 - 6)))
            p.setPen(col)
            p.drawText(QRectF(x0 + 4, 2, max(0, x1 - x0 - 6), self.LANE - 4), Qt.AlignVCenter | Qt.AlignLeft, t)
        # 原字幕时间段（细线）
        if self.sub:
            x0, x1 = self.x_of(self.sub[0]), self.x_of(self.sub[1])
            p.setPen(QPen(QColor(148, 163, 184), 1, Qt.DashLine))
            p.drawLine(QPointF(x0, top), QPointF(x0, bottom)); p.drawLine(QPointF(x1, top), QPointF(x1, bottom))
        # 选区
        if self.cut:
            x0, x1 = self.x_of(self.cut[0]), self.x_of(self.cut[1])
            p.fillRect(QRectF(x0, top, x1 - x0, bottom - top), QColor(45, 212, 191, 50))
        # 波形
        p.setPen(QPen(QColor(15, 23, 42, 22), 1))
        p.drawLine(QPointF(0, mid), QPointF(W, mid))
        if self.samples is not None and len(self.samples):
            a = self.samples
            i0 = (self.v0 - self.s_t0) * self.sr
            per = self.span() * self.sr / max(1, W)
            pen = QPen(QColor("#475569")); pen.setWidthF(1); p.setPen(pen)
            inside = QPen(QColor("#0F766E")); inside.setWidthF(1)
            cx0 = self.x_of(self.cut[0]) if self.cut else -1
            cx1 = self.x_of(self.cut[1]) if self.cut else -1
            if per < 1.5:       # 放得很大：直接连采样点
                path = QPainterPath()
                first = True
                k0, k1 = max(0, int(i0) - 1), min(len(a), int(i0 + W * per) + 2)
                for k in range(k0, k1):
                    x = (k - i0) / per
                    y = mid - float(a[k]) / self._norm * hw
                    if first:
                        path.moveTo(x, y); first = False
                    else:
                        path.lineTo(x, y)
                p.setPen(inside); p.drawPath(path)
            else:
                for x in range(W):
                    k0, k1 = int(i0 + x * per), int(i0 + (x + 1) * per) + 1
                    if k1 <= 0 or k0 >= len(a):
                        continue
                    seg = a[max(0, k0):min(len(a), k1)]
                    if not len(seg):
                        continue
                    amp = float(np.abs(seg).max()) / self._norm * hw
                    p.setPen(inside if cx0 <= x <= cx1 else pen)
                    p.drawLine(QPointF(x + 0.5, mid - amp), QPointF(x + 0.5, mid + amp))
        else:
            p.setPen(QColor("#94A3B8"))
            p.drawText(QRectF(0, top, W, bottom - top), Qt.AlignCenter, "波形加载中…")
        # 识别出的词
        wy = bottom + 2
        row_end = [-1e9, -1e9]
        for w in self.words:
            x0, x1 = self.x_of(w["start"]), self.x_of(w["end"])
            if x1 < 0 or x0 > W:
                continue
            p.fillRect(QRectF(x0, wy, max(1, x1 - x0), 22), QColor(148, 163, 184, 38))
            tw = fm.horizontalAdvance(w["w"]) + 4
            r = 0 if row_end[0] <= x0 else 1 if row_end[1] <= x0 else None
            if r is None:
                continue
            row_end[r] = x0 + tw
            p.setPen(QColor("#334155"))
            p.drawText(QRectF(x0 + 2, wy + r * 11, tw, 11), Qt.AlignVCenter | Qt.AlignLeft, w["w"])
        # 刻度
        ry = H - 14
        p.setPen(QPen(QColor("#CBD5E1"), 1))
        p.drawLine(QPointF(0, ry), QPointF(W, ry))
        step = next((s for s in (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60) if self.x_of(self.v0 + s) - self.x_of(self.v0) >= 70), 60)
        import math
        t = math.ceil(self.v0 / step) * step
        p.setPen(QColor("#64748B"))
        f2 = QFont(MONO.split(",")[0]); f2.setPointSize(8); p.setFont(f2)
        while t <= self.v1:
            x = self.x_of(t)
            p.drawLine(QPointF(x, ry), QPointF(x, ry + 4))
            p.drawText(QRectF(x + 2, ry, 70, 14), Qt.AlignVCenter | Qt.AlignLeft, fmt_short(t))
            t += step
        # 手柄
        if self.cut:
            for t in self.cut:
                x = self.x_of(t)
                p.setPen(QPen(QColor("#0D9488"), 2))
                p.drawLine(QPointF(x, top - 2), QPointF(x, bottom))
                p.setBrush(QBrush(QColor("#0D9488"))); p.setPen(Qt.NoPen)
                p.drawRoundedRect(QRectF(x - 5, bottom - 20, 10, 20), 3, 3)
        if self.playhead is not None and self.v0 <= self.playhead <= self.v1:
            x = self.x_of(self.playhead)
            p.setPen(QPen(QColor("#F43F5E"), 1.5))
            p.drawLine(QPointF(x, 0), QPointF(x, H - 14))
        p.end()

    # ---------------- 吸附
    def _quiet_point(self, t, win=0.12):
        if self.samples is None or not len(self.samples):
            return None
        a = self.samples
        k0 = int((t - win - self.s_t0) * self.sr); k1 = int((t + win - self.s_t0) * self.sr)
        k0, k1 = max(0, k0), min(len(a), k1)
        n = int(0.03 * self.sr)
        if k1 - k0 <= n * 2:
            return None
        seg = np.abs(a[k0:k1])
        c = np.convolve(seg, np.ones(n) / n, mode="valid")
        j = int(np.argmin(c))
        return self.s_t0 + (k0 + j + n / 2) / self.sr

    def _zero_cross(self, t, win=0.004):
        if self.samples is None or not len(self.samples):
            return t
        a = self.samples
        k = int((t - self.s_t0) * self.sr)
        r = int(win * self.sr)
        k0, k1 = max(1, k - r), min(len(a) - 1, k + r)
        if k1 <= k0:
            return t
        seg = a[k0 - 1:k1]
        zc = np.nonzero(np.signbit(seg[:-1]) != np.signbit(seg[1:]))[0]
        if not len(zc):
            return t
        j = zc[np.argmin(np.abs(zc + k0 - 1 - k))]
        return self.s_t0 + (j + k0) / self.sr

    def _snap(self, t, free):
        if free:
            return t
        cands = []
        if "word" in self.snap:
            for w in self.words:
                cands += [w["start"], w["end"]]
        if "cue" in self.snap:
            for l in self.lines:
                cands += [l[0], l[1]]
        best = min(cands, key=lambda c: abs(c - t)) if cands else None
        if best is not None and abs(self.x_of(best) - self.x_of(t)) < 12:
            t = best
        elif "silence" in self.snap:
            q = self._quiet_point(t)
            if q is not None:
                t = q
        if "zero" in self.snap:
            t = self._zero_cross(t)
        return t

    # ---------------- 鼠标
    def _line_at(self, x, y):
        if y > self.LANE:
            return None
        t = self.t_of(x)
        for l in self.lines:
            if l[0] <= t <= l[1]:
                return l
        return None

    def mousePressEvent(self, e):
        x, y = e.position().x(), e.position().y()
        self._press = (x, y)
        if e.button() == Qt.MiddleButton:
            self._drag = ("pan", x, self.v0)
            return
        l = self._line_at(x, y)
        if l is not None:
            self.pick_line.emit(l[3], bool(e.modifiers() & Qt.ShiftModifier))
            self._drag = None
            return
        if self.cut:
            d0, d1 = abs(x - self.x_of(self.cut[0])), abs(x - self.x_of(self.cut[1]))
            if min(d0, d1) <= 9:
                self._drag = 0 if d0 <= d1 else 1
                return
        self._drag = ("new", x)

    def mouseMoveEvent(self, e):
        x = e.position().x()
        free = bool(e.modifiers() & Qt.AltModifier)
        d = self._drag
        if d is None:
            near = self.cut and min(abs(x - self.x_of(self.cut[0])), abs(x - self.x_of(self.cut[1]))) <= 9
            self.setCursor(Qt.SizeHorCursor if near else
                           Qt.PointingHandCursor if self._line_at(x, e.position().y()) else Qt.IBeamCursor)
            return
        if isinstance(d, tuple) and d[0] == "pan":
            dt = (d[1] - x) / max(1, self.width()) * self.span()
            self._set_view(d[2] + dt, d[2] + dt + self.span())
            return
        if isinstance(d, tuple) and d[0] == "new":
            if abs(x - d[1]) < 4:
                return
            a, b = sorted((self._snap(self.t_of(d[1]), free), self._snap(self.t_of(x), free)))
            self.cut = (max(self.t0, a), min(self.t1, max(b, a + 0.05)))
            self.update()
            return
        t = self._snap(self.t_of(x), free)
        a, b = self.cut
        if d == 0:
            a = min(t, b - 0.05)
        else:
            b = max(t, a + 0.05)
        self.cut = (max(self.t0, a), min(self.t1, b))
        self.update()

    def mouseReleaseEvent(self, e):
        d = self._drag
        self._drag = None
        if d is None:
            return
        if isinstance(d, tuple) and d[0] == "pan":
            return
        if isinstance(d, tuple) and d[0] == "new" and abs(e.position().x() - d[1]) < 4:
            self.seek.emit(self.t_of(e.position().x()))
            return
        if self.cut:
            self.changed.emit(*self.cut)

    def wheelEvent(self, e):
        dy = e.angleDelta().y() or e.angleDelta().x()
        if not dy:
            return
        if e.modifiers() & (Qt.ControlModifier | Qt.MetaModifier):
            self.zoom(0.8 if dy > 0 else 1.25, self.t_of(e.position().x()))
        else:
            dt = -dy / 120 * self.span() * 0.15
            self._set_view(self.v0 + dt, self.v1 + dt)
        e.accept()


class DialogueMap(QWidget):
    """整集的「说话地图」：每条台词一根竖线，框出波形当前看的那一段；点哪儿跳哪儿。"""
    seek = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.dur = 1.0
        self.lines = []
        self.view = None
        self.playhead = None
        self.setCursor(Qt.PointingHandCursor)

    def set_data(self, dur, lines):
        self.dur = max(1.0, dur)
        self.lines = lines
        self.update()

    def set_view(self, a, b):
        self.view = (a, b); self.update()

    def set_playhead(self, t):
        self.playhead = t; self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        bg = QPainterPath(); bg.addRoundedRect(QRectF(self.rect()), 9, 9)
        p.fillPath(bg, QColor(255, 255, 255, 200))
        x_of = lambda t: 8 + t / self.dur * (W - 16)
        for (s, e, src) in self.lines:
            x = x_of(s)
            p.setPen(QPen(QColor("#8B5CF6") if src == "auto" else QColor("#94A3B8"), 1.2))
            p.drawLine(QPointF(x, 9), QPointF(x, H - 15))
        if self.view:
            a, b = x_of(self.view[0]), x_of(self.view[1])
            p.setPen(QPen(QColor("#0D9488"), 1.5)); p.setBrush(QColor(45, 212, 191, 50))
            p.drawRoundedRect(QRectF(a - 1, 5, max(4, b - a + 2), H - 16), 3, 3)
        if self.playhead is not None:
            x = x_of(self.playhead)
            p.setPen(QPen(QColor("#F43F5E"), 1.5)); p.drawLine(QPointF(x, 3), QPointF(x, H - 12))
        f = QFont(MONO.split(",")[0]); f.setPointSize(8); p.setFont(f); p.setPen(QColor("#64748B"))
        p.drawText(QRectF(8, H - 13, 80, 12), Qt.AlignLeft | Qt.AlignVCenter, "0:00")
        p.drawText(QRectF(W - 88, H - 13, 80, 12), Qt.AlignRight | Qt.AlignVCenter, fmt_short(self.dur))
        p.end()

    def mousePressEvent(self, e):
        t = (e.position().x() - 8) / max(1, self.width() - 16) * self.dur
        self.seek.emit(max(0.0, min(self.dur, t)))


def tag(text, color="#64748B", solid=False):
    """浅底小胶囊（sub / indexed / changed 那种）。"""
    lb = QLabel(text)
    c = QColor(color)
    bg = f"rgba({c.red()},{c.green()},{c.blue()},{0.95 if solid else 0.12})"
    fg = "#FFFFFF" if solid else color
    lb.setStyleSheet(f"QLabel{{color:{fg};background:{bg};border:none;border-radius:8px;padding:1px 8px;"
                     f"font-size:11px;font-weight:600;}}")
    lb.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return lb


class GroupHeader(QFrame):
    """搜索结果按集分组的组头：▾ 剧名 S01E02 ……  n 条"""
    toggled = Signal(str, bool)

    def __init__(self, key, label, n, parent=None):
        super().__init__(parent)
        self.key, self.open = key, True
        self.setObjectName("grouphdr")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(38)
        h = QHBoxLayout(self); h.setContentsMargins(12, 7, 14, 7); h.setSpacing(8)
        self.arrow = QLabel("▾"); self.arrow.setStyleSheet("color:#64748B;")
        h.addWidget(self.arrow)
        from .theme import icon
        ic = QLabel(); ic.setPixmap(icon("film", "#64748B", 16).pixmap(16, 16)); h.addWidget(ic)
        t = QLabel(f"<b>{html.escape(label)}</b>"); t.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        h.addWidget(t, 1)
        c = QLabel(f"{n} 条"); c.setObjectName("small"); h.addWidget(c)

    def mousePressEvent(self, e):
        self.open = not self.open
        self.arrow.setText("▾" if self.open else "▸")
        self.toggled.emit(self.key, self.open)

    def hasHeightForWidth(self):
        return False

    def sizeHint(self):
        return QSize(300, 38)


class ResultRow(QFrame):
    """一条结果：☐  台词（命中词高亮）/ 译文 / 时间码 ……  匹配度  ★ ✂ 🔗"""
    clicked = Signal(object)
    double = Signal(object)
    collect = Signal(object)
    export_one = Signal(object)
    link = Signal(object)
    checked = Signal(object, bool)

    def __init__(self, hit, timing: str, span, parent=None, show_source=False):
        super().__init__(parent)
        from .theme import icon
        from PySide6.QtWidgets import QCheckBox
        self.hit = hit
        self.setObjectName("row")
        self.setProperty("sel", "false")
        self.setCursor(Qt.PointingHandCursor)
        # 1.3.0：时间 / 出处 / 标签单独一行占满宽度（剪辑页左栏窄，挤在右侧按钮旁会被截断）
        outer = QVBoxLayout(self); outer.setContentsMargins(14, 8, 10, 8); outer.setSpacing(3)
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(10)
        outer.addLayout(h)
        self.cb = QCheckBox(); self.cb.setCursor(Qt.ArrowCursor)
        self.cb.toggled.connect(lambda on: self.checked.emit(self.hit, on))
        h.addWidget(self.cb, 0, Qt.AlignTop)
        v = QVBoxLayout(); v.setSpacing(2); v.setContentsMargins(0, 0, 0, 0)
        self.en = QLabel(highlight_html(hit.en, hit.matched)); self.en.setWordWrap(True)
        # 1.2.0：底下留 3px，免得 y / g / p 的下伸部被切掉（Mac 上粗体高亮时最明显）
        self.en.setStyleSheet("font-size:14px;padding:1px 0 3px 0;" +
                              ("font-style:italic;color:#7C3AED;" if hit.src == "auto" else ""))
        v.addWidget(self.en)
        if hit.zh:
            zh = QLabel(html.escape(hit.zh)); zh.setWordWrap(True)
            zh.setStyleSheet("font-size:12px;color:#64748B;padding-bottom:2px;"); v.addWidget(zh)
        meta = QHBoxLayout(); meta.setSpacing(6); meta.setContentsMargins(0, 0, 0, 0)
        tm = self.tm = QLabel(f"{fmt_short(span[0])} – {fmt_short(span[1])}")
        tm.setToolTip(f"{fmt_ms(span[0])} – {fmt_ms(span[1])}")
        tm.setStyleSheet(f"color:#0F766E;font-family:{MONO};font-size:11px;")
        meta.addWidget(tm)
        if show_source:
            meta.addWidget(tag(hit.label, "#0F766E"))
        tt, tc = TIMING_TEXT.get(timing, ("", "#999"))
        meta.addWidget(tag(hit.tier, TIER_COLOR.get(hit.tier, "#64748B")))
        from ..paths import ALIGN_UI
        if ALIGN_UI:                 # 1.2 起不再显示对齐状态（全部已对齐），省出位置给出处
            meta.addWidget(tag(tt, tc))
        if hit.src == "join":
            meta.addWidget(tag("跨两行", "#0EA5E9"))
        if hit.src == "auto":
            meta.addWidget(tag("识别", "#8B5CF6"))
        if not hit.has_video:
            meta.addWidget(tag("无视频", "#E11D48"))
        if hit.speakers > 1:
            meta.addWidget(tag("多人", "#94A3B8"))
        meta.addStretch(1)
        h.addLayout(v, 1)
        meta.insertSpacing(0, self.cb.sizeHint().width() + 10)
        outer.addLayout(meta)
        pc = QLabel(f"{hit.pct}%"); pc.setObjectName("pct"); pc.setToolTip("匹配度：整句贴合、时长合适、单人、已对齐、有视频越高")
        h.addWidget(pc, 0, Qt.AlignVCenter)
        # 1.3.0：剪辑页左栏变窄，行里只留收藏和链接（导出在右边「导出片段」和底部「导出选中」）
        for nm, tip, sig, prim in (("star", "收藏到当前采集包（Ctrl+D）", self.collect, False),
                                   ("link", "复制片段链接（粘贴到搜索框可直接打开）", self.link, False)):
            b = QPushButton(); b.setObjectName("iconPrimary" if prim else "iconBtn")
            b.setIcon(icon(nm, "#FFFFFF" if prim else "#0F766E", 16)); b.setIconSize(QSize(16, 16))
            b.setToolTip(tip); b.setFixedSize(30, 30); b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, s=sig: s.emit(self.hit))
            h.addWidget(b, 0, Qt.AlignVCenter)
            if nm == "star":
                self.star = b

    def set_span(self, s, e):
        self.tm.setText(f"{fmt_short(s)} – {fmt_short(e)}")
        self.tm.setToolTip(f"{fmt_ms(s)} – {fmt_ms(e)}")

    def mark_collected(self):
        """收藏后：星星变成实心金色，并「弹」一下。"""
        from .theme import icon
        self.star.setIcon(icon("star", "#F59E0B", 16, fill="#FBBF24"))
        self.star.setToolTip("已收藏（再点会提示已在包里）")
        star_pop(self.star)

    def set_selected(self, on):
        self.setProperty("sel", "true" if on else "false")
        self.style().unpolish(self); self.style().polish(self)

    def mousePressEvent(self, e):
        self.clicked.emit(self.hit)

    def mouseDoubleClickEvent(self, e):
        self.double.emit(self.hit)


TAGS_ROLE = Qt.UserRole + 7


class TagDelegate(QStyledItemDelegate):
    """列表里的状态小胶囊直接画出来（1.0.8：以前每行塞 QWidget，上千集时刷新要几十秒）。
    数据放在 TAGS_ROLE：[(文字, 颜色, 实心), …]"""

    def paint(self, p, opt, idx):
        super().paint(p, opt, idx)
        tags = idx.data(TAGS_ROLE) or []
        if not tags:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        f = QFont(opt.font); f.setPixelSize(11); f.setBold(True); p.setFont(f)
        fm = p.fontMetrics()
        x = opt.rect.left() + 2
        h = fm.height() + 2
        y = opt.rect.top() + (opt.rect.height() - h) / 2
        for text, color, solid in tags:
            w = fm.horizontalAdvance(text) + 16
            if x + w > opt.rect.right():
                break
            c = QColor(color)
            bg = QColor(c); bg.setAlphaF(0.95 if solid else 0.12)
            p.setPen(Qt.NoPen); p.setBrush(bg)
            p.drawRoundedRect(QRectF(x, y, w, h), h / 2, h / 2)
            p.setPen(QColor("#FFFFFF") if solid else c)
            p.drawText(QRectF(x, y, w, h), Qt.AlignCenter, text)
            x += w + 4
        p.restore()


# ---------------------------------------------------------------- 收藏动效（1.3.0）
def star_pop(button, big=26, small=16):
    """按钮上的图标放大再弹回（约 0.35 秒）。"""
    from PySide6.QtCore import QVariantAnimation, QEasingCurve
    base = button.iconSize().width() or small
    a = QVariantAnimation(button)
    a.setDuration(360)
    a.setKeyValueAt(0.0, base); a.setKeyValueAt(0.45, big); a.setKeyValueAt(1.0, base)
    a.setEasingCurve(QEasingCurve.OutBack)
    a.valueChanged.connect(lambda v: button.setIconSize(QSize(int(v), int(v))))
    a.start(QVariantAnimation.DeleteWhenStopped)


class Toast(QLabel):
    """窗口中下方浮出的小提示：淡入 + 上移，停 1.4 秒后淡出。"""

    def __init__(self, parent):
        super().__init__(parent)
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        from PySide6.QtCore import QPropertyAnimation, QSequentialAnimationGroup, QParallelAnimationGroup, QEasingCurve, QTimer
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAlignment(Qt.AlignCenter)
        self.eff = QGraphicsOpacityEffect(self); self.eff.setOpacity(0.0); self.setGraphicsEffect(self.eff)
        self.fade = QPropertyAnimation(self.eff, b"opacity", self)
        self.move_a = QPropertyAnimation(self, b"pos", self)
        self.move_a.setEasingCurve(QEasingCurve.OutCubic)
        self.timer = QTimer(self, singleShot=True, interval=1400, timeout=self._out)
        self.hide()

    def pop(self, text, ok=True, mark=None):
        color, bg = ("#FFFFFF", "rgba(15,118,110,0.94)") if ok else ("#0F172A", "rgba(254,243,199,0.97)")
        star = mark if mark is not None else ("★  " if ok else "☆  ")
        self.setText(star + text)
        self.setStyleSheet(f"QLabel{{color:{color};background:{bg};border-radius:18px;padding:9px 22px;"
                           f"font-size:14px;font-weight:600;}}")
        self.adjustSize()
        p = self.parentWidget()
        x = (p.width() - self.width()) // 2
        y = int(p.height() * 0.78)
        self.fade.stop(); self.move_a.stop(); self.timer.stop()
        self.show(); self.raise_()
        self.move_a.setDuration(260); self.move_a.setStartValue(QPoint(x, y + 18)); self.move_a.setEndValue(QPoint(x, y))
        self.fade.setDuration(200); self.fade.setStartValue(self.eff.opacity()); self.fade.setEndValue(1.0)
        self.move_a.start(); self.fade.start()
        self.timer.start()

    def _out(self):
        self.fade.stop()
        self.fade.setDuration(450); self.fade.setStartValue(self.eff.opacity()); self.fade.setEndValue(0.0)
        try:
            self.fade.finished.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.fade.finished.connect(lambda: self.hide() if self.eff.opacity() < 0.05 else None)
        self.fade.start()
