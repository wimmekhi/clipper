"""资源路径：源码运行与打包（PyInstaller 单文件 / .app）两种情况。"""
from __future__ import annotations

import sys
from pathlib import Path

VERSION = "1.3.4"
# 1.2.0：所有集已对齐完，界面里去掉对齐功能（要对齐请用 1.1.0 及以前的版本）。对齐数据照常使用。
ALIGN_UI = False
# 1.3.2：视频都是压好的 480p，只用「横屏原画无损」导出；重新编码的三种版本、字幕样式、合集从界面拿掉（代码保留）
REENCODE_UI = False


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    if frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def fonts_dir() -> Path:
    return resource_dir() / "assets" / "fonts"


def bin_dir() -> Path:
    return resource_dir() / "bin"


def icon_path() -> Path:
    return resource_dir() / "assets" / "icon.png"
