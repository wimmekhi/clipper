# PyInstaller 打包配置：Windows 出单个 .exe，macOS 出 .app
# 用法：pyinstaller clipper.spec --noconfirm   （bin/ 里放 ffmpeg，会一起打进去）
import os, sys, glob
from PyInstaller.utils.hooks import collect_all

sys.path.insert(0, os.path.abspath("."))
from clipper.paths import VERSION

NAME = "台词切片编辑器"
datas = [("assets", "assets")]
binaries = [(f, "bin") for f in glob.glob("bin/*")]
hidden = ["clipper.ui.main"]
for pkg in ("faster_whisper", "ctranslate2", "onnxruntime"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h

excludes = ["tkinter", "matplotlib", "IPython", "pytest", "mlx", "mlx_whisper", "torch", "scipy",
            "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick",
            "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
            "PySide6.QtDesigner", "PySide6.QtBluetooth", "PySide6.QtLocation", "PySide6.QtPositioning"]

a = Analysis(["run.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False, upx=False,
              icon="assets/icon.png")
    coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
    app = BUNDLE(coll, name=NAME + ".app", icon="assets/icon.png", bundle_identifier="com.wimmekhi.clipper",
                 info_plist={"CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
                             "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0"})
else:
    # 单文件：双击即用；每次启动先解压到临时目录，所以打开要等几秒
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=NAME, console=False, upx=False,
              icon="assets/icon.png", runtime_tmpdir=None)
