"""启动：python run.py　　自检：run.py --selftest 结果文件"""
import multiprocessing
import os
import sys

# 打包版必须先调：否则下载模型/进度条用到的多进程锁会用本程序路径再启动一个自己（Dock 里一串图标）
# 无控制台的打包版（Win 的 exe、可能还有 Mac .app）里 sys.stdout/stderr 是 None，
# 下模型时 tqdm 往 stderr 写进度就会抛 'NoneType' object has no attribute 'write'，对齐全挂。
for _n in ("stdout", "stderr"):
    if getattr(sys, _n) is None:
        setattr(sys, _n, open(os.devnull, "w", encoding="utf-8"))
multiprocessing.freeze_support()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _mlx_checks(chk, smoke_wav=None):
    """Apple 芯片：mlx 能导入、metallib 和 libmlx 放在一起（mlx 就是按这个位置找）、Metal 能真算。"""
    import platform
    if not (sys.platform == "darwin" and platform.machine() == "arm64"):
        return
    from pathlib import Path
    chk("mlx import", lambda: __import__("mlx.core", fromlist=["x"]).__version__)
    def metallib():
        import mlx
        hits = []
        for r in mlx.__path__:
            for lib in Path(r, "lib").glob("libmlx*.dylib"):
                m = lib.parent / "mlx.metallib"
                if not m.exists():
                    raise RuntimeError(f"libmlx 在 {lib.parent}，旁边没有 mlx.metallib")
                size = m.resolve().stat().st_size
                if size < 30e6:
                    raise RuntimeError(f"mlx.metallib 只有 {size/1e6:.1f} MB，不完整")
                hits.append(f"{m} ({size/1e6:.0f} MB)")
        if not hits:
            raise RuntimeError("没找到 libmlx.dylib")
        return "; ".join(hits)
    chk("mlx.metallib", metallib)
    def metal():
        import mlx.core as mx
        from clipper.align import mlx_device
        dev = mlx_device()
        if not mx.metal.is_available():
            return "DEGRADED 这台机器没有 Metal 设备（CI 虚拟机常见），GPU 计算未验证，只验证了导入与 metallib 文件"
        with mx.stream(mx.gpu):
            a = mx.ones((512, 512))
            v = (a @ a).sum()
            mx.eval(v)
        if float(v) != 512.0 ** 3:
            raise RuntimeError(f"GPU 计算结果不对：{float(v)}")
        return f"{dev} 矩阵乘法 OK（metallib 已加载）"
    chk("metal", metal)
    def whisper_bits():
        import numpy as np
        import mlx_whisper  # noqa
        from mlx_whisper.tokenizer import get_tokenizer
        from mlx_whisper.timing import dtw_cpu
        t = get_tokenizer(multilingual=True, language="en", task="transcribe")
        assert t.decode(t.encode("whose turn is it")) == "whose turn is it"
        assert len(dtw_cpu(np.zeros((2, 4), dtype=np.float32))) == 2
        import scipy  # noqa
        return "tokenizer + dtw(numba) + scipy ok"
    chk("mlx_whisper", whisper_bits)
    if smoke_wav:
        def smoke():
            import numpy as np
            import mlx.core as mx
            from clipper import align
            raw = Path(smoke_wav).read_bytes()   # 16 kHz 单声道 s16le 裸数据
            audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
            steps = []
            path = align.ensure_model("mlx", "tiny", mirror=False, progress=lambda f, t: steps.append(t))
            prog = []
            words = align.transcribe_mlx(audio, "tiny", path, progress=prog.append)
            txt = " ".join(w["w"] for w in words).lower()
            if "turn" not in txt or not all(w["end"] >= w["start"] for w in words):
                raise RuntimeError(f"识别结果不对：{txt!r}")
            if not prog or prog[-1] < 0.99:
                raise RuntimeError(f"识别进度没回报：{prog[-3:]}")
            dev = "GPU" if mx.metal.is_available() else "CPU 设备（无 Metal，降级）"
            return (f"{dev}，{len(words)} 个词带时间：{txt} [{words[0]['start']:.2f}-{words[-1]['end']:.2f}s]，"
                    f"进度回报 {len(prog)} 次")
        chk("mlx 对齐冒烟(tiny)", smoke)


def _faster_smoke(chk, smoke_wav=None):
    """faster-whisper：下载 tiny（不走镜像）→ 加载 → 识别，进度回调要有。"""
    def run():
        import numpy as np
        from pathlib import Path
        from clipper import align
        if smoke_wav:
            audio = np.frombuffer(Path(smoke_wav).read_bytes(), np.int16).astype(np.float32) / 32768.0
        else:
            t = np.arange(16000 * 3) / 16000
            audio = (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        prog = []
        words = align.transcribe(audio, model="tiny", engine="faster", mirror=False,
                                 progress=lambda f, t="": prog.append((f, t)))
        if not prog or prog[-1][0] < 0.99:
            raise RuntimeError(f"进度没走完：{prog[-3:]}")
        txt = " ".join(w["w"] for w in words).lower()
        if smoke_wav and "turn" not in txt:
            raise RuntimeError(f"识别结果不对：{txt!r}")
        return f"{align.LAST_ENGINE}，{len(words)} 个词：{txt[:60]}"
    chk("faster 对齐冒烟(tiny)", run)


def selftest(out, smoke_wav=None):
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

    def upd():
        from clipper import updater
        r = updater.check()
        if not r["version"]:
            raise RuntimeError(f"没读到版本：{r}")
        t = updater.current_target()
        if paths.frozen() and t is None:
            raise RuntimeError("打包版却找不到要替换的 exe/.app")
        return f"线上最新 {r['version']}，本平台包 {'有' if r['url'] else '无'}，替换目标 {t}"
    chk("一键更新：查版本+证书+替换目标", upd)
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
        import clipper.ui.aligncenter  # noqa
        return "ok"
    chk("qt+multimedia+ui", qt)
    def mp_lock():
        lk = multiprocessing.RLock()   # 会拉起 resource_tracker 子进程；没处理好就会重开整个程序
        with lk:
            pass
        import tqdm
        tqdm.tqdm(total=1, disable=False, file=open(os.devnull, "w")).close()
        return "ok"
    chk("multiprocessing lock", mp_lock)
    _mlx_checks(chk, smoke_wav)
    if os.environ.get("CLIPPER_SMOKE") or smoke_wav:
        _faster_smoke(chk, smoke_wav)
    if tools.check() is not None:
        ok = False
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + ("\nALL OK\n" if ok else "\nFAILED\n"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    # 被当成子进程（-c / --multiprocessing-fork 之类）启动时绝不打开界面
    if any(a in ("-c", "-B", "-s", "-E") or a.startswith("--multiprocessing") for a in sys.argv[1:]):
        sys.exit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2], sys.argv[4] if len(sys.argv) >= 5 and sys.argv[3] == "--mlx-smoke" else None)
    from clipper.ui.main import main
    main()
