"""显卡加速（Windows N 卡）的状态 + 下载 CUDA 运行库。设置页和对齐中心共用；下载在后台，关掉窗口也继续。"""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .. import gpu

_DL: list = []          # 全局唯一的下载线程


def current_download():
    return _DL[0] if _DL and not _DL[0].finished else None


class CudaPanel(QWidget):
    changed = Signal()      # 运行库就绪/删除后通知外面刷新引擎显示

    def __init__(self, parent=None, compact=False):
        super().__init__(parent)
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(4)
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0)
        self.lb = QLabel(""); self.lb.setWordWrap(True)
        h.addWidget(self.lb, 1)
        self.btn = QPushButton("下载 CUDA 运行库")
        self.btn.clicked.connect(self._click)
        h.addWidget(self.btn)
        self.rm = QPushButton("删除运行库")
        self.rm.setToolTip("删掉已下载的 CUDA 运行库，腾出约 1.3 GB；之后对齐用 CPU")
        self.rm.clicked.connect(self._remove)
        h.addWidget(self.rm)
        v.addLayout(h)
        self.bar = QProgressBar(); self.bar.setRange(0, 1000); self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.step = QLabel(""); self.step.setObjectName("muted"); self.step.setWordWrap(True)
        self.step.setVisible(False)
        v.addWidget(self.step)
        if not compact:
            tip = QLabel(f"只在这台电脑上用：放在 {gpu.cuda_dir()}，不占移动硬盘。下载走清华/阿里等国内镜像，可断点续传。")
            tip.setObjectName("muted"); tip.setWordWrap(True)
            v.addWidget(tip)
            self.tip = tip
        self.timer = QTimer(self, interval=500, timeout=self._poll)
        self.refresh()
        if current_download():
            self.timer.start()

    def refresh(self):
        st = gpu.status()
        dl = current_download()
        self.lb.setText("显卡：" + st["text"])
        self.btn.setVisible(st["state"] in ("need_runtime",) or bool(dl))
        self.btn.setText("暂停下载" if dl else ("继续下载 CUDA 运行库" if self._has_partial() else
                                              f"下载 CUDA 运行库（约 {gpu.APPROX_MB} MB）"))
        self.rm.setVisible(st["state"] in ("ready", "broken") and not dl)
        self.bar.setVisible(bool(dl)); self.step.setVisible(bool(dl) or bool(self.step.text()))
        if st["state"] == "na":
            self.setVisible(False)

    def _has_partial(self) -> bool:
        d = gpu.cuda_dir()
        try:
            return d.is_dir() and any(d.iterdir())
        except OSError:
            return False

    def _click(self):
        dl = current_download()
        if dl:
            dl.cancel()
            self.btn.setEnabled(False)
            return
        t = gpu.DownloadThread()
        _DL.clear(); _DL.append(t)
        t.start()
        self.step.setText("连接镜像…")
        self.timer.start()
        self.refresh()

    def _poll(self):
        t = _DL[0] if _DL else None
        if not t:
            self.timer.stop(); return
        if t.total:
            self.bar.setValue(int(1000 * t.got / t.total))
        self.step.setText(t.text)
        if t.finished:
            self.timer.stop()
            self.btn.setEnabled(True)
            if t.error:
                self.step.setText("下载失败：" + t.error + "（再点一次会接着下）")
            elif not t.cancelled:
                self.step.setText("CUDA 运行库已就绪，下一集对齐起用显卡")
                self.changed.emit()
            self.refresh()

    def _remove(self):
        if QMessageBox.question(self, "删除运行库", "删掉 CUDA 运行库？之后对齐改用 CPU，要用显卡得重新下载。") \
                != QMessageBox.Yes:
            return
        gpu.remove_runtime()
        self.step.setText("")
        self.refresh()
        self.changed.emit()
