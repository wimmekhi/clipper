"""对话框：设置（含字幕样式）、导出。"""
from __future__ import annotations

import tempfile
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap, QFontDatabase
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QWidget, QLineEdit,
                               QPushButton, QComboBox, QCheckBox, QSpinBox, QDoubleSpinBox, QLabel, QFileDialog,
                               QColorDialog, QDialogButtonBox, QFontComboBox, QGroupBox)

from ..export import Style, PRESETS


class ColorButton(QPushButton):
    def __init__(self, color: str):
        super().__init__()
        self.setFixedWidth(70)
        self.set(color)
        self.clicked.connect(self.pick)

    def set(self, c):
        self.color = c
        self.setText(c)
        fg = "#000" if QColor(c).lightness() > 128 else "#fff"
        self.setStyleSheet(f"background:{c};color:{fg};border:1px solid #999;border-radius:3px;")

    def pick(self):
        c = QColorDialog.getColor(QColor(self.color), self)
        if c.isValid():
            self.set(c.name().upper())


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, style: Style, preview_ctx=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(820, 640)
        self.preview_ctx = preview_ctx       # 供样式预览：dict(video,t,en,zh,matched,title,source)
        tabs = QTabWidget()
        # ---- 常规 ----
        g = QWidget(); f = QFormLayout(g)
        self.ffmpeg = QLineEdit(settings.get("ffmpeg", ""))
        self.ffmpeg.setPlaceholderText("留空 = 自动找系统里的 ffmpeg")
        b = QPushButton("选择…"); b.clicked.connect(self._pick_ffmpeg)
        row = QHBoxLayout(); row.addWidget(self.ffmpeg); row.addWidget(b)
        f.addRow("ffmpeg 路径", row)
        self.export_dir = QLineEdit(settings.get("export_dir", ""))
        self.export_dir.setPlaceholderText("留空 = 媒体库根目录/导出")
        b2 = QPushButton("选择…"); b2.clicked.connect(self._pick_export)
        row2 = QHBoxLayout(); row2.addWidget(self.export_dir); row2.addWidget(b2)
        f.addRow("导出文件夹", row2)
        self.pre = QDoubleSpinBox(); self.pre.setRange(0, 2); self.pre.setSingleStep(0.05)
        self.pre.setValue(settings.get("pad_pre", 0.30)); self.pre.setSuffix(" 秒")
        self.post = QDoubleSpinBox(); self.post.setRange(0, 2); self.post.setSingleStep(0.05)
        self.post.setValue(settings.get("pad_post", 0.35)); self.post.setSuffix(" 秒")
        f.addRow("句前余量", self.pre)
        f.addRow("句后余量", self.post)
        tabs.addTab(g, "常规")
        # ---- 对齐 ----
        a = QWidget(); fa = QFormLayout(a)
        self.auto_align = QCheckBox("视频入库后自动在后台对齐"); self.auto_align.setChecked(settings.get("auto_align", True))
        fa.addRow(self.auto_align)
        self.engine = QComboBox()
        self.engine.addItem("自动（Mac 装了 mlx-whisper 用 GPU，否则 CPU）", "")
        self.engine.addItem("faster-whisper（CPU）", "faster")
        self.engine.addItem("mlx-whisper（Mac Apple 芯片 GPU）", "mlx")
        i = self.engine.findData(settings.get("align_engine", ""))
        self.engine.setCurrentIndex(max(0, i))
        fa.addRow("识别引擎", self.engine)
        self.model = QComboBox()
        for m in ("tiny.en", "base.en", "small.en", "small", "medium.en", "large-v3-turbo"):
            self.model.addItem(m)
        self.model.setCurrentText(settings.get("align_model", "small"))
        fa.addRow("模型", self.model)
        fa.addRow(QLabel("模型越大越准越慢。先用 small，实测后再调。"))
        self.mirror = QCheckBox("模型下载走国内镜像 hf-mirror.com"); self.mirror.setChecked(settings.get("hf_mirror", True))
        fa.addRow(self.mirror)
        tabs.addTab(a, "对齐")
        # ---- 字幕样式 ----
        s = QWidget(); hs = QHBoxLayout(s)
        left = QWidget(); fs = QFormLayout(left)
        self.font = QFontComboBox(); self.font.setCurrentText(style.font)
        fs.addRow("字体", self.font)
        self.en_size = self._spin(style.en_size, 16, 120); fs.addRow("英文字号", self.en_size)
        self.zh_size = self._spin(style.zh_size, 16, 120); fs.addRow("中文字号", self.zh_size)
        self.en_color = ColorButton(style.en_color); fs.addRow("英文颜色", self.en_color)
        self.zh_color = ColorButton(style.zh_color); fs.addRow("中文颜色", self.zh_color)
        self.bold_en = QCheckBox("英文加粗"); self.bold_en.setChecked(style.bold_en); fs.addRow(self.bold_en)
        self.hl_on = QCheckBox("句型词高亮"); self.hl_on.setChecked(style.highlight_on); fs.addRow(self.hl_on)
        self.hl = ColorButton(style.highlight); fs.addRow("高亮颜色", self.hl)
        self.outline = self._spin(style.outline, 0, 8); fs.addRow("描边粗细", self.outline)
        self.outline_color = ColorButton(style.outline_color); fs.addRow("描边颜色", self.outline_color)
        self.bg = ColorButton(style.bg_color); fs.addRow("上下底色", self.bg)
        self.title_on = QCheckBox("顶部显示句型标题（两个竖屏版都有）"); self.title_on.setChecked(style.title_on)
        fs.addRow(self.title_on)
        self.title_size = self._spin(style.title_size, 16, 120); fs.addRow("标题字号", self.title_size)
        self.title_color = ColorButton(style.title_color); fs.addRow("标题颜色", self.title_color)
        self.source_on = QCheckBox("标题下显示剧名季集"); self.source_on.setChecked(style.source_on)
        fs.addRow(self.source_on)
        self.gap = self._spin(style.gap, 0, 200); fs.addRow("字幕离画面距离", self.gap)
        hs.addWidget(left, 1)
        right = QVBoxLayout()
        self.prev = QLabel("选中一条结果后可预览")
        self.prev.setAlignment(Qt.AlignCenter)
        self.prev.setFixedSize(270, 480)
        self.prev.setStyleSheet("background:#111;color:#999;border-radius:6px;")
        right.addWidget(self.prev)
        pb = QPushButton("预览样式"); pb.clicked.connect(self._preview)
        pb.setEnabled(bool(preview_ctx))
        right.addWidget(pb)
        right.addStretch(1)
        hs.addLayout(right)
        tabs.addTab(s, "字幕样式")
        v = QVBoxLayout(self)
        v.addWidget(tabs)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._still = None

    def _spin(self, v, lo, hi):
        sp = QSpinBox(); sp.setRange(lo, hi); sp.setValue(int(v)); return sp

    def _pick_ffmpeg(self):
        p, _ = QFileDialog.getOpenFileName(self, "选择 ffmpeg")
        if p:
            self.ffmpeg.setText(p)

    def _pick_export(self):
        p = QFileDialog.getExistingDirectory(self, "选择导出文件夹")
        if p:
            self.export_dir.setText(p)

    def settings(self) -> dict:
        return dict(ffmpeg=self.ffmpeg.text().strip(), export_dir=self.export_dir.text().strip(),
                    pad_pre=self.pre.value(), pad_post=self.post.value(), auto_align=self.auto_align.isChecked(),
                    align_engine=self.engine.currentData(), align_model=self.model.currentText(),
                    hf_mirror=self.mirror.isChecked())

    def style(self) -> Style:
        return Style(font=self.font.currentText(), en_size=self.en_size.value(), zh_size=self.zh_size.value(),
                     en_color=self.en_color.color, zh_color=self.zh_color.color, highlight=self.hl.color,
                     highlight_on=self.hl_on.isChecked(), outline=self.outline.value(),
                     outline_color=self.outline_color.color, bold_en=self.bold_en.isChecked(),
                     bg_color=self.bg.color, title_on=self.title_on.isChecked(), title_size=self.title_size.value(),
                     title_color=self.title_color.color, source_on=self.source_on.isChecked(), gap=self.gap.value())

    def _preview(self):
        from .workers import StillWorker
        c = self.preview_ctx
        if not c:
            return
        self.prev.setText("生成中…")
        out = Path(tempfile.gettempdir()) / "clipper_style_preview.png"
        self._still = StillWorker(c["video"], c["t"], out, self.style(), c["en"], c["zh"], c["matched"],
                                  c["title"], c["source"])
        self._still.done.connect(self._show_still)
        self._still.start()

    def _show_still(self, path):
        if not path:
            self.prev.setText("预览失败（检查 ffmpeg）")
            return
        pm = QPixmap(path).scaled(self.prev.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.prev.setPixmap(pm)


class ExportDialog(QDialog):
    def __init__(self, title: str, count: int, default_dir: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出")
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"<b>{title}</b>　共 {count} 条"))
        box = QGroupBox("版本（可多选）")
        bl = QVBoxLayout(box)
        self.checks = {}
        for p in PRESETS:
            c = QCheckBox(p); c.setChecked(True); bl.addWidget(c); self.checks[p] = c
        v.addWidget(box)
        self.comp = QCheckBox("同时拼成合集长视频（每个版本一条）"); self.comp.setChecked(count >= 2)
        self.comp.setEnabled(count >= 2)
        v.addWidget(self.comp)
        row = QHBoxLayout()
        self.dir = QLineEdit(default_dir)
        b = QPushButton("选择…"); b.clicked.connect(self._pick)
        row.addWidget(QLabel("导出到")); row.addWidget(self.dir, 1); row.addWidget(b)
        v.addLayout(row)
        v.addWidget(QLabel("规格：H.264 + AAC 48kHz，23.976 帧，横屏 1280×720 / 竖屏 720×1280，faststart"))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("开始导出")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.resize(560, 300)

    def _pick(self):
        p = QFileDialog.getExistingDirectory(self, "选择导出文件夹", self.dir.text())
        if p:
            self.dir.setText(p)

    def presets(self):
        return [p for p, c in self.checks.items() if c.isChecked()]
