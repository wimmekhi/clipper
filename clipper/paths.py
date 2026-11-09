"""资源路径：源码运行与打包（PyInstaller 单文件 / .app）两种情况。"""
from __future__ import annotations

import sys
from pathlib import Path

VERSION = "1.0.4"


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
