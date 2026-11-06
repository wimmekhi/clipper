"""启动：python run.py　　自检：run.py --selftest 结果文件"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def selftest(out):
    lines, ok = [], True
    def chk(name, fn):
        nonlocal ok
        try:
            lines.append(f"OK   {name}: {fn()}")
        except Exception as ex:  # noqa
            ok = False
            lines.append(f"FAIL {name}: {ex!r}")
    from clipper import tools, paths
    chk("version", lambda: paths.VERSION)
    chk("ffmpeg", lambda: tools.ffmpeg())
    chk("ffmpeg+libass", lambda: tools.check() or "libass ok")
    chk("fonts", lambda: sorted(os.listdir(paths.fonts_dir())))
    chk("faster_whisper", lambda: __import__("faster_whisper").__version__)
    chk("ctranslate2", lambda: __import__("ctranslate2").__version__)
    chk("onnxruntime", lambda: __import__("onnxruntime").__version__)
    chk("sqlite fts5", lambda: __import__("sqlite3").connect(":memory:").execute(
        "create virtual table t using fts5(a, tokenize='porter unicode61')") and "ok")
    def qt():
        from PySide6.QtWidgets import QApplication
        from PySide6.QtMultimedia import QMediaPlayer
        app = QApplication.instance() or QApplication([])
        QMediaPlayer()
        import clipper.ui.main  # noqa
        return "ok"
    chk("qt+multimedia+ui", qt)
    if tools.check() is not None:
        ok = False
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + ("\nALL OK\n" if ok else "\nFAILED\n"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2])
    from clipper.ui.main import main
    main()
