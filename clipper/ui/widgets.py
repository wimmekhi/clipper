"""自绘控件：结果卡片、切点波形条。"""
from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt, Signal, QRectF, QPointF, QRect, QPoint, QSize
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QPainterPath, QBrush
from PySide6.QtWidgets import (QLayout, QWidget, QLabel, QHBoxLayout, QVBoxLayout, QPushButton, QFrame,
                               QSizePolicy)

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
