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


def _ui_test():
    # CI 上万一卡住：4 分钟后把所有线程的调用栈打到 stderr 并退出，方便定位
    import faulthandler
    faulthandler.dump_traceback_later(240, exit=True)
    try:
        return _ui_test_body()
    finally:
        faulthandler.cancel_dump_traceback_later()


def _ui_test_body():
    """真把主窗口开起来：临时媒体库（1 集，字幕里有一句拆成两行）→ 扫描 → 手动排队/取消 → 对齐中心 →
    菜单 / 顶栏 / 五个页面 → 搜索（含跨两行）→ 工作台（切点、I/O、吸附、试听）→ 片段链接 → 无损导出 → 最小宽度。"""
    import tempfile, subprocess, time
    from PySide6.QtWidgets import QApplication, QToolBar
    from clipper import tools, export
    from clipper.ui.main import MainWindow, P_WORK, P_SEARCH
    app = QApplication.instance() or QApplication([])
    root = tempfile.mkdtemp(prefix="clipper-ui-")
    d = os.path.join(root, "Test Show", "S01"); os.makedirs(d)
    vid = os.path.join(d, "S01E01.mp4")
    subprocess.run([tools.ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=6", "-f", "lavfi",
                    "-i", "sine=f=300:d=6", "-shortest", "-c:v", "libx264", "-g", "12", "-c:a", "aac", vid], check=True)
    with open(os.path.join(d, "S01E01.srt"), "w", encoding="utf-8") as fh:
        fh.write("1\n00:00:00,500 --> 00:00:02,000\nWhose turn is it?\n轮到谁了？\n\n"
                 "2\n00:00:02,200 --> 00:00:03,000\nI don't know what\n我不知道\n\n"
                 "3\n00:00:03,100 --> 00:00:04,500\nyou are doing here.\n你在这干嘛\n")
    w = MainWindow(autostart=False)
    w.resize(1000, 650); w.show(); app.processEvents()
    w.open_library(root)
    w._scan.wait(60000); app.processEvents()
    keys = [r[0] for r in w.lib.db.execute("SELECT key FROM media")]
    assert keys, "临时库没扫出集"
    assert not w.lib_settings()["auto_align"], "默认应是手动对齐"
    # 1.3.2：快照——再打开不自动扫描；导出文件夹不进库
    assert w.lib.snapshot() and w.lib.snapshot()["total"] == len(keys), "扫描后没存快照"
    os.makedirs(os.path.join(root, "横屏原画无损")); open(os.path.join(root, "横屏原画无损", "S01E01 x.mp4"), "w").close()
    first_scan = w._scan
    w.open_library(root); app.processEvents()
    assert w._scan is first_scan and "快照" in w.al_lb.text(), "有快照时打开不该自动扫描"
    w.rescan(); w._scan.wait(60000); app.processEvents()
    assert w.lib.db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == len(keys), "导出文件夹被扫进库了"
    w.f_show.setCurrentIndex(0); app.processEvents()
    assert w.f_show.width() < 160, f"全部作品下拉太宽：{w.f_show.width()}"
    started = []
    real_start = w.start_align
    w.start_align = lambda force=False: started.append(force)
    n = w.queue_keys(keys)
    st = w.lib.db.execute("SELECT align_status FROM media").fetchone()[0]
    assert n == 1 and st == "queued" and started == [True], f"手动排队失败 n={n} st={st} started={started}"
    w.cancel_queue()
    assert w.lib.db.execute("SELECT align_status FROM media").fetchone()[0] == "none", "取消排队失败"
    w.start_align = real_start
    w.open_align_center(); app.processEvents()
    # 1.3.1：引擎检测线程会在后台 import mlx_whisper/scipy；等它结束再往下测，
    # 免得和后面的对话框构造、ffmpeg 子进程并发（Mac CI 曾因此卡死 4 分钟）
    _pr = getattr(w.align_center, "_probe", None)
    if _pr is not None:
        _pr.wait(180000); app.processEvents()
    w.align_center.close()
    menus = [a.text() for a in w.menuBar().actions()]
    need = ["文件", "搜索", "片段", "采集包", "对齐", "窗口", "帮助"]
    assert all(m in menus for m in need), f"菜单缺：{menus}"
    tb = w.findChild(QToolBar, "toolbar_main")
    assert tb and len(tb.actions()) >= 9 and len(w.nav) == 4, "顶栏缺"
    assert w.upd_btn.isVisible() and w.upd_btn.text() == "检查更新", "顶栏没有一键更新按钮"
    w._mark_update({"version": "99.0.0", "url": "https://example.invalid/x.zip", "size": 1})
    assert w.upd_btn.text() == "更新到 99.0.0" and w.upd_btn.property("hot") == "true", "更新按钮没点亮"
    w._mark_update(None)
    assert "batchbar" in app.styleSheet(), "主题没生效"
    assert w.eps.topLevelItemCount() == 1, "媒体库页没列出集"
    from clipper.paths import ALIGN_UI
    assert ALIGN_UI or not w.top_align.isVisible(), "1.2 起顶栏不该有对齐中心"
    # 搜索：普通 + 跨两行
    w.q.setText("whose turn"); w.do_search(); app.processEvents()
    assert w.stack.currentIndex() == P_SEARCH and w.hits and w.rows, "搜索没出结果"
    # 1.2.0：已有结果时在搜索框里按回车再搜（以前被工作台的 Enter 快捷键吃掉）
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt as _Q
    w.activateWindow(); w.focus_search(); app.processEvents()
    w._focus_changed(None, w.q)
    QTest.keyClicks(w.q, "...you are doing..."); QTest.keyClick(w.q, _Q.Key_Return); app.processEvents()
    assert w.hits and all("doing" in h.en for h in w.hits), f"搜索框回车第二次搜没反应：{[h.en for h in w.hits]}"
    # 默认只按匹配度：一列、没有分组头
    assert w.c_rank.isChecked() and not w.group_items, "默认应只按匹配度排、不分组"
    sc = [h.score for h in w.sorted_hits()]
    assert sc == sorted(sc, reverse=True), "没按匹配度排"
    w.c_rank.setChecked(False); app.processEvents()
    assert w.group_items, "关掉只按匹配度后应按集分组"
    w.c_rank.setChecked(True); w.c_ted.setChecked(True); app.processEvents(); w.c_ted.setChecked(False)
    w.c_order.setChecked(True)
    w.q.setText("know what you are doing"); w.do_search(); app.processEvents()
    assert any(h.src == "join" for h in w.hits), f"跨两行的句子没搜到：{[(h.en, h.src) for h in w.hits]}"
    w.c_order.setChecked(False)
    # 工作台
    h = w.hits[0]
    w.open_workspace(h, autoplay=False); app.processEvents()
    assert w.stack.currentIndex() == P_WORK == P_SEARCH and w.script.count() == 3, f"台词本 {w.script.count()} 句"
    # 1.3.0：搜索和工作台同一页；左栏两个标签
    assert w.left_stack.currentIndex() == 0 and w.results.isVisible(), "合并页左栏应停在搜索结果"
    w.left_scr.click(); app.processEvents()
    assert w.left_stack.currentIndex() == 1 and w.script.isVisible(), "本集台词标签切不过去"
    w.left_res.click(); app.processEvents()
    for _ in range(100):
        app.processEvents()
        if w.wave.samples is not None:
            break
        time.sleep(0.05)
    assert w.wave.samples is not None and len(w.wave.samples) > 1000, "波形采样没加载"
    s0, e0 = w.cur_span
    w.wave.snap = {"silence", "zero"}
    t = w.wave._snap(s0 + 0.01, False)
    assert abs(t - s0) < 0.2, f"吸附跑太远：{s0} -> {t}"
    w.wave.zoom(0.5); w.wave.fit_selection()
    w.player.setPosition(int((s0 + 0.2) * 1000)); app.processEvents()
    w._cut_changed(s0 + 0.2, e0)
    assert abs(w.cur_span[0] - (s0 + 0.2)) < 1e-6, "改切点失败"
    w.save_cut()
    row = w._cue_row(w.cur)
    assert w.lib.final_span(row, w.cur.media_key)[2] == "manual", "微调没保存"
    w.reset_cut()
    # 1.3.0：选区改动自动保存；收藏 / 导出按调好的范围
    w._cut_changed(s0 + 0.1, e0 - 0.1); w._flush_cut()
    sp = w.lib.final_span(w._cue_row(w.cur), w.cur.media_key)
    assert sp[2] == "manual" and abs(sp[0] - (s0 + 0.1)) < 1e-6, f"选区没自动保存：{sp}"
    w.collect(w.cur); app.processEvents()
    assert w.toast.isVisible() and "收藏" in w.toast.text(), "收藏没有提示"
    w.reset_cut()
    # 1.3.1：采集包重命名 / 删除（删除只动包，不动微调和文件）
    from PySide6.QtWidgets import QInputDialog as _QID
    _orig = _QID.getText
    pid0 = w.pack_cb.currentData(); n_items = w.pack_list.count()
    _QID.getText = staticmethod(lambda *a, **k: ("改名测试包", True))
    try:
        w.rename_pack()
    finally:
        _QID.getText = _orig
    assert w.pack_cb.currentText() == "改名测试包" and w.pack_cb.currentData() == pid0 \
        and w.pack_list.count() == n_items, "采集包重命名失败"
    w.lib.pack_id("另一个包"); w.refresh_packs("改名测试包")
    assert not w.lib.rename_pack(pid0, "另一个包"), "重名没拦住"
    w.lib.save_adjust(w.cur.media_key, w.cur.idx, s0 + 0.05, e0)
    w._test_confirm = False; w.delete_pack()
    assert any(p["id"] == pid0 for p in w.lib.packs()), "点了取消却删了包"
    w._test_confirm = True; w.delete_pack(); w._test_confirm = None
    assert not any(p["id"] == pid0 for p in w.lib.packs()), "采集包没删掉"
    assert not w.lib.db.execute("SELECT 1 FROM pack_items WHERE pack_id=?", (pid0,)).fetchone(), "包条目没清"
    assert w.lib.final_span(w._cue_row(w.cur), w.cur.media_key)[2] == "manual", "删包误删了微调"
    assert w.pack_cb.currentText() == "另一个包", "删包后没切到剩下的包"
    w.reset_cut()
    from PySide6.QtCore import Qt as _Qt
    i0, i1 = w.script.item(0).data(_Qt.UserRole), w.script.item(1).data(_Qt.UserRole)
    w.pick_cue(i0); first = w.cur.idx
    e_first = w.cur_span[1]
    w.pick_cue(i1, extend=True)
    assert w.cur.idx == first == i0 and w.cur_span[1] > e_first + 0.3, "Shift 加选没把选区扩到下一句"
    w.audition_in(); w.audition_out(); w.player.stop()
    # 1.2.0：选到虚线（原字幕时间）
    w.select_sub()
    assert w.cur_span == tuple(w.wave.sub) and w.wave.cut == w.cur_span, "选到虚线没生效"
    w.reset_cut()
    # 1.2.0：导出文件名 = 英文台词，默认原画无损
    from clipper import jobs
    from clipper.export import clip_name
    from clipper.ui.dialogs import ExportDialog
    assert clip_name('Is this your sock?') == "Is this your sock", clip_name('Is this your sock?')
    for _ww in list(w._wave_workers):      # 1.3.1：先等后台波形线程收尾，再建对话框
        _ww.wait(60000)
    app.processEvents()
    dlg = ExportDialog("x", 1, root, w, name=clip_name(w.cur.en))
    assert dlg.presets() == ["横屏原画无损"] and dlg.name() == clip_name(w.cur.en), f"导出框默认不对：{dlg.presets()}"
    dlg.deleteLater()
    xr = w.lib.db.execute("SELECT c.*, m.key media_key, m.show, m.season, m.episode, m.kind, m.rel_video FROM cues c "
                          "JOIN media m ON m.id=c.media_id WHERE c.idx=?", (w.cur.idx,)).fetchone()
    od = __import__("pathlib").Path(root) / "导出测试"
    for _ in range(2):
        r = jobs.export_items(w.lib, [dict(row=xr)], od, ["横屏原画无损"], w.style())
        assert r["ok"], r
    r = jobs.export_items(w.lib, [dict(row=xr), dict(row=xr)], od, ["横屏原画无损"], w.style(), make_compilation=False)
    names = sorted(f.name for f in (od / "横屏原画无损").iterdir())
    want = clip_name(xr["en"])
    assert names == sorted([f"{want}.mp4"] + [f"{want} ({k}).mp4" for k in (2, 3, 4)]), f"导出文件名不对：{names}"
    from clipper.subs import clean_en
    assert clean_en("THIS IS YOUR SECOND CHANCE.") == "This is your second chance." and \
        clean_en("That&#x27;s yours.") == "That's yours.", "全大写 / 转义没处理"
    # 片段链接
    link = w.link_of(w.cur)
    w.q.setText(link); w.do_search(); app.processEvents()
    assert w.stack.currentIndex() == P_WORK and w.cur.idx == first, "片段链接打不开"
    # 无损导出
    out = os.path.join(root, "x.mp4")
    export.export_lossless(vid, 1.0, 2.5, __import__("pathlib").Path(out))
    assert os.path.getsize(out) > 1000, "无损导出失败"
    for i in range(4):
        w.goto(i); app.processEvents()
    mw = w.minimumSizeHint().width()
    assert mw <= 1000, f"主窗口最小宽度 {mw}px 太大"
    w.close(); app.processEvents()
    return (f"菜单 {len(menus)} 个、顶栏 {len(tb.actions())} 项、4 个页面；搜索/回车再搜/按匹配度/跨两行/剪辑页合并/采集包改名删除/快照不自动扫描+导出不进库/"
            f"选区自动保存/收藏提示/选到虚线/链接/无损导出+重名(2)/全大写转换都过；最小宽 {mw}px")


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
        try:
            r = updater.check()
        except Exception as ex:  # noqa  CI 共享出口 IP 常被 GitHub 限流：证书/网络是通的，只记 DEGRADED
            if "rate limit" in str(ex).lower() or "429" in str(ex):
                t = updater.current_target()
                if paths.frozen() and t is None:
                    raise RuntimeError("打包版却找不到要替换的 exe/.app")
                return f"DEGRADED GitHub 限流（{ex}），证书和网络已通；替换目标 {t}"
            raise
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
    def child_ping():
        from clipper import align
        _d, pid = align.transcribe_isolated("--ping")
        if not pid or pid == os.getpid():
            raise RuntimeError(f"子进程 pid 不对：{pid}")
        return f"识别子进程能起（pid {pid}）"
    chk("识别子进程", child_ping)
    def throttle():
        from clipper import align
        import time as _t
        th = align.Throttle(0.5); th.mark = _t.time() - 0.2; t0 = _t.time(); th()
        nap = _t.time() - t0
        if not 0.15 < nap < 0.5:
            raise RuntimeError(f"50% 上限应歇约 0.2s，实际 {nap:.2f}s")
        return f"50% 上限歇 {nap:.2f}s"
    chk("性能上限", throttle)
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
    chk("界面：顶栏/页面/搜索/工作台/链接/无损导出", _ui_test)
    def mp_lock():
        lk = multiprocessing.RLock()   # 会拉起 resource_tracker 子进程；没处理好就会重开整个程序
        with lk:
            pass
        import tqdm
        tqdm.tqdm(total=1, disable=False, file=open(os.devnull, "w")).close()
        return "ok"
    chk("multiprocessing lock", mp_lock)
    def cuda_panel():
        from clipper import gpu
        r = gpu.selftest()
        if gpu.is_windows():
            from PySide6.QtWidgets import QApplication
            QApplication.instance() or QApplication([])
            from clipper.ui.cudapanel import CudaPanel
            CudaPanel().refresh()
        return r
    chk("显卡加速（CUDA 运行库）", cuda_panel)
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
