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
    w._test_newpack = (True, "default")       # 1.3.4：还没有同名包 → 确认新建（自测不弹窗）
    w._cut_changed(s0 + 0.1, e0 - 0.1); w._flush_cut()
    sp = w.lib.final_span(w._cue_row(w.cur), w.cur.media_key)
    assert sp[2] == "manual" and abs(sp[0] - (s0 + 0.1)) < 1e-6, f"选区没自动保存：{sp}"
    w.collect(w.cur); app.processEvents()
    assert w.toast.isVisible() and "收藏" in w.toast.text(), "收藏没有提示"
    w.reset_cut()
    _pack_tests(w, app, root, s0, e0)
    w.reset_cut()
    _h_keep = w.cur
    _result_row_tests(w, app, root)
    w.preview(_h_keep, autoplay=False); app.processEvents()
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
    assert dlg.presets() == ["横屏原画精确"] and dlg.name() == clip_name(w.cur.en), f"导出框默认不对：{dlg.presets()}"
    dlg.deleteLater()
    xr = w.lib.db.execute("SELECT c.*, m.key media_key, m.show, m.season, m.episode, m.kind, m.rel_video FROM cues c "
                          "JOIN media m ON m.id=c.media_id WHERE c.idx=?", (w.cur.idx,)).fetchone()
    od = __import__("pathlib").Path(root) / "导出测试"
    for _ in range(2):
        r = jobs.export_items(w.lib, [dict(row=xr)], od, ["横屏原画精确"], w.style())
        assert r["ok"], r
    r = jobs.export_items(w.lib, [dict(row=xr), dict(row=xr)], od, ["横屏原画精确"], w.style(), make_compilation=False)
    names = sorted(f.name for f in od.iterdir())       # 1.3.4：只出视频，没有预设名那一层、没有说明 / manifest
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
    # 1.3.3：精确剪切，时长和选区一致（无损会退到关键帧）
    outp = __import__("pathlib").Path(root) / "p.mp4"
    export.export_precise(vid, 1.0, 2.5, outp)
    import re as _re
    _d = _re.search(r"Duration: (\d+):(\d+):([\d.]+)", tools.run([tools.ffmpeg(), "-hide_banner", "-i", str(outp)]).stderr)
    _dur = float(_d.group(3)) + int(_d.group(2)) * 60
    assert abs(_dur - 1.5) < 0.12, f"精确剪切时长不对：{_dur}"
    # 1.3.3：收藏在选区那排，前面显示包里条数
    _fp = w.fav_pack_id if w.active_pattern else None
    assert w.pack_n_lb.text() == f"{w.lib.pack_count(_fp) if _fp else 0} 条", w.pack_n_lb.text()
    _wave_tests(w, app)
    # 1.3.3：波形缓存——播放头移动不重建缓存
    w.wave.update(); w.wave.repaint(); _k = w.wave._cache[0] if w.wave._cache else None
    w.wave.set_playhead((w.wave.v0 + w.wave.v1) / 2, True, 1.0); w.wave.repaint()
    assert _k is not None and w.wave._cache[0] == _k, "播放头移动重建了波形缓存"
    w.wave.set_playhead(None)
    for i in range(4):
        w.goto(i); app.processEvents()
    mw = w.minimumSizeHint().width()
    assert mw <= 820, f"主窗口最小宽度 {mw}px 太大：" + str([w.stack.widget(i).minimumSizeHint().width() for i in range(w.stack.count())]) + w.pack_btn_w.text()
    w.close(); app.processEvents()
    return (f"菜单 {len(menus)} 个、顶栏 {len(tb.actions())} 项、4 个页面；搜索/回车再搜/按匹配度/跨两行/剪辑页合并/采集包改名删除/快照不自动扫描+导出不进库/"
            f"选区自动保存/收藏提示/选到虚线/链接/精确剪切+重名(2)/收藏条数/波形缓存/全大写转换；"
            f"1.3.4 收藏=句型同名包（大小写/空白/...≡…、确认新建、取消不收、多同名取最近、只列同名、空搜索禁用）/"
            f"文件夹增删改移+防套娃+旧库迁移/按文件夹导出只出视频/整集音轨/dpr=2 波形都过；"
            f"1.3.5 结果行不重复（滚动后再搜/切标签/收藏/微调）+星标三态/批量查包/只从当前包移除/同步刷新；最小宽 {mw}px")


def _rank_tests():
    """1.3.4 排名：Hi 误伤、Or? 不靠前、空位、排除表、跨句、等级进分数。"""
    from clipper.search import analyse, parse_query, TIER_ORIG, TIER_NEAR, Searcher
    A = lambda q, en: analyse(parse_query(q), en)
    assert A("is this his…", "Hi, guys! This is Josh.") is None, "his 被当成 hi（Hi 误伤）"
    assert A("is this his…", "No. Is this his signature?")["tier"] == TIER_ORIG, "句首语气词 + 句型没认成原句型"
    assert A("… or …", "Or?")["tier"] == TIER_NEAR and A("… or …", "Or?")["unfilled"] == 2, "Or? 不该是原句型"
    assert A("… or …", "Coffee or tea?")["tier"] == TIER_ORIG, "空位填满的没认成原句型"
    for en in ("It took ten minutes or so.", "Whether or not you like it.", "Either you go or I go.", "Is he crazy or what?"):
        assert A("… or …", en)["tier"] == TIER_NEAR, f"排除表没生效：{en}"
    assert A("would you mind …", "Would you mind? Close the door.")["tier"] == TIER_NEAR, "空位跨句了"
    assert A("is this your…", "This is great. Your car?") is not None and \
        A("is this your…", "This is great. Your car?")["tier"] == TIER_NEAR, "句型跨句了"
    class _R(dict):
        def keys(self):
            return super().keys()
    base = dict(speakers=1, a_start=None, a_end=None, a_score=None, zh="", has_video=1, en="")
    def sc(q, en, dur=2.0):
        qu = parse_query(q); d = analyse(qu, en)
        return Searcher._score(_R(base, en=en), d, dur, qu)
    s_or = sc("… or …", "Or?"); s_ok = sc("… or …", "Coffee or tea?"); s_sym = sc("… or …", "I want the red one or that?")
    assert s_ok > s_sym > s_or, f"Or? 应垫底、空位对称加分：{s_ok} {s_sym} {s_or}"
    assert sc("is this his…", "No. Is this his signature?") > sc("is this his…", "So this is his strike two."), "原句型没排在近似前"
    # --- 1.3.5 缩写互通：搜的写法 = 原句型，另一种写法 = 近似变体
    pairs = [("this is", "This is my car.", "This's my car."), ("it is", "It is fine.", "It's fine."),
             ("do not", "Do not touch it.", "Don't touch it."), ("cannot", "I cannot go.", "I can't go."),
             ("it is not", "It is not fair.", "It isn't fair.")]
    for q1, en1, en2 in pairs:
        q2 = parse_query(en2).parts
        q2 = next(" ".join(a.parts) for a in parse_query(q1).alts if all(w in q2 for w in a.parts))
        for q, mine, other in ((q1, en1, en2), (q2, en2, en1)):
            dm, do = A(q, mine), A(q, other)
            assert dm and dm["tier"] == TIER_ORIG, f"{q}：自己的写法没认成原句型 {dm}"
            assert do and do["tier"] == TIER_NEAR and do.get("alt"), f"{q}：另一种写法 {other!r} 应是近似变体 {do}"
            assert sc(q, mine) > sc(q, other), f"{q}：缩写互通排序不对"
    assert sc("this is", "This's my car.") > sc("this is", "Is this my car?"), "缩写互通应排在一般近似前"
    assert A("can't", "I can not go.")["tier"] == TIER_NEAR and A("it's", "It has been fun.")["tier"] == TIER_NEAR
    assert analyse(parse_query("it's"), "It is fine.", exact=True) is None, "精确模式不该缩写互通"
    assert not parse_query("is this your…").alts and not parse_query("… or …").alts, "没有缩写的句型不该有变体"
    # 搜索层：临时库里两种写法都能搜到，等级对
    import tempfile
    from clipper.library import Library
    root = tempfile.mkdtemp(prefix="clipper-rank-"); dd = os.path.join(root, "Show", "S01"); os.makedirs(dd)
    lines = ["This is my car.", "This's my car.", "It is fine.", "It's fine.", "Do not touch it.", "Don't touch it.",
             "I cannot go.", "I can't go."]
    with open(os.path.join(dd, "S01E01.srt"), "w", encoding="utf-8") as fh:
        for i, ln in enumerate(lines):
            fh.write(f"{i + 1}\n00:00:{i * 3:02d},000 --> 00:00:{i * 3 + 2:02d},000\n{ln}\n\n")
    L = Library(root); L.scan(); S = Searcher(L)
    for q, orig, near in (("this is", lines[0], lines[1]), ("this's", lines[1], lines[0]), ("it's", lines[3], lines[2]),
                          ("it is", lines[2], lines[3]), ("do not", lines[4], lines[5]), ("don't", lines[5], lines[4]),
                          ("can't", lines[7], lines[6]), ("cannot", lines[6], lines[7])):
        got = {h.en: h.tier for h in S.search(q)}
        assert got.get(orig) == TIER_ORIG and got.get(near) == TIER_NEAR, f"搜 {q}：{got}"
    L.db.close()
    return "排名规则 + 缩写互通"


def _pack_tests(w, app, root, s0, e0):
    """1.3.4：收藏 = 句型同名包；文件夹；导出结构。"""
    import sqlite3, tempfile, time
    from pathlib import Path
    from clipper.library import Library
    from clipper.export import clip_name
    from clipper.ui.packs import K_PACK, K_FOLDER
    from PySide6.QtCore import Qt
    L = w.lib
    # --- 同名规则
    assert L.norm_name("  Is  THIS his... ") == L.norm_name("is this his…"), "包名匹配规则不对"
    first = L.packs_named("know what you are doing")
    assert len(first) == 1 and L.pack(first[0])["name"] == "know what you are doing" and L.pack_count(first[0]) == 1, \
        f"第一次收藏没新建同名包：{[dict(p) for p in L.packs()]}"
    n_items = lambda: L.db.execute("SELECT COUNT(*) FROM pack_items").fetchone()[0]
    # --- 不存在 → 取消就不收
    w.q.setText("whose ... is it"); w.do_search(); app.processEvents()
    assert w.active_pattern == "whose … is it" and w.hits, f"句型没记下：{w.active_pattern}"
    h = next(x for x in w.hits if "turn" in x.en)
    k0, p0 = n_items(), len(L.packs())
    w._test_newpack = (False, None); w.collect(h); app.processEvents()
    assert n_items() == k0 and len(L.packs()) == p0, "取消新建却收了 / 建了包"
    # --- 确认新建，放进选的文件夹
    fA = L.ensure_folder_path("NCE1/L1-L2")
    w._test_newpack = (True, fA); w.collect(h); app.processEvents()
    ids = L.packs_named("whose … is it")
    assert len(ids) == 1 and L.pack_path(ids[0]) == ["NCE1", "L1-L2", "whose … is it"] and L.pack_count(ids[0]) == 1, \
        f"确认新建不对：{[L.pack_path(i) for i in ids]}"
    pa = ids[0]
    # --- 大小写 / 空白 / ... ≡ …：直接收进已有的包，不弹框、不新建
    w._test_newpack = (False, None)
    w.q.setText("  WHOSE   ...  is IT "); w.do_search(); app.processEvents()
    assert w.fav_pack_id == pa and "NCE1" in w.pack_btn_w.text(), f"同名包没认出来：{w.fav_pack_id} {w.pack_btn_w.text()}"
    p0 = len(L.packs()); w._test_confirm = False; w.collect(h); app.processEvents()   # 1.3.5：已在包里 → 问移除，取消
    assert len(L.packs()) == p0 and L.pack_count(pa) == 1, "同名包又新建了一个"
    # --- 多个同名包：用最近用过的；收藏到弹窗只列同名的
    other = L.create_pack("something else", fA)
    pb = L.create_pack("Whose... is it", None)
    assert pb and L.pack(pb)["name"] == "Whose… is it", "新建包名三点没转成 …"
    L.touch_recent_pack(pb)
    w.do_search(); app.processEvents()
    assert w.fav_pack_id == pb, f"多个同名包没取最近用过的：{w.fav_pack_id} pa={pa} pb={pb} {L.packs_named(w.active_pattern or '')} {L.get_setting('recent_packs')} {w.active_pattern!r} {w.q.text()!r}"
    w._open_picker(); app.processEvents()
    lst = [w.pack_picker.lst.item(i).data(Qt.UserRole) for i in range(w.pack_picker.lst.count())]
    w.pack_picker.hide()
    assert sorted(lst) == sorted([pa, pb]), f"收藏到弹窗应只列同名包：{lst}"
    w._fav_pick(pa); assert w.fav_pack_id == pa, "弹窗切换同名包失败"
    w._fav_pick(other); assert w.fav_pack_id == pa, "竟然能指向名字不同的包"
    w.collect(h)
    assert L.pack_count(pb) == 0 and L.pack_count(other) == 0 and L.pack_count(pa) == 1, "收进了别的包"
    w._test_confirm = None
    # --- 搜索框空 / 改了没搜 / 链接打开：禁用
    w.q.setText(""); app.processEvents()
    assert w.active_pattern is None and not w.b_fav.isEnabled() and not w.pack_btn_w.isEnabled(), "空搜索时收藏没禁用"
    assert "先搜索句型" in w.b_fav.toolTip() and "先搜索句型" in w.pack_btn_w.text(), "没提示先搜索句型"
    k0 = n_items(); w.collect(h); assert n_items() == k0, "空搜索时还能收藏"
    w.q.setText("whose ... is it"); w.do_search(); w.q.setText("whose ... is it now"); app.processEvents()
    assert not w.b_fav.isEnabled(), "搜索框改了没搜，收藏应先禁用"
    w.q.setText(w.link_of(h)); w.do_search(); app.processEvents()
    assert w.active_pattern is None and not w.b_fav.isEnabled(), "链接打开时收藏应禁用"
    w._test_newpack = None
    # --- 采集包页：树、重命名、重名拦截、删除确认
    w._test_warn = []
    w.refresh_packs((K_PACK, pa)); app.processEvents()   # 不切页：切页会换播放器的画面输出
    assert w.pack_tree.current() == ("p", pa) and w.pack_list.count() == 1 and "NCE1" in w.pack_crumb.text(), "树 / 面包屑不对"
    from PySide6.QtWidgets import QInputDialog as _QID
    _orig = _QID.getText
    try:
        _QID.getText = staticmethod(lambda *a, **k: ("改名测试包", True)); w.rename_pack()
        assert L.pack(pa)["name"] == "改名测试包", "采集包重命名失败"
        _QID.getText = staticmethod(lambda *a, **k: ("SOMETHING  else", True)); w.rename_pack()
        assert L.pack(pa)["name"] == "改名测试包" and w._test_warn, "同文件夹重名没拦住"
    finally:
        _QID.getText = _orig
    # 名字改了 → 不再是「whose … is it」的收藏目标
    w.q.setText("whose ... is it"); w.do_search(); app.processEvents()
    assert w.fav_pack_id == pb, "改名后的包还被当成同名包"
    L.save_adjust(h.media_key, h.idx, h.start + 0.05, h.end)
    w.refresh_packs((K_PACK, pa))
    w._test_confirm = False; w.delete_pack()
    assert L.pack(pa), "点了取消却删了包"
    w._test_confirm = True; w.delete_pack()
    assert not L.pack(pa) and not L.db.execute("SELECT 1 FROM pack_items WHERE pack_id=?", (pa,)).fetchone(), "包没删掉"
    assert L.db.execute("SELECT 1 FROM adjust WHERE media_key=? AND idx=?", (h.media_key, h.idx)).fetchone(), "删包误删了微调"
    # --- 文件夹：防套娃、移动、删除非空（确认里说清数量）
    nce = L.find_folder("nce1")
    assert not L.move_folder(nce, fA) and not L.move_folder(nce, nce), "能把文件夹挪进自己 / 子孙"
    sub = L.create_folder("L3-L4", nce)
    assert L.move_pack(pb, sub) and L.pack_path(pb) == ["NCE1", "L3-L4", "Whose… is it"], "挪包失败"
    assert L.move_folder(sub, None) and L.folder_path(sub) == ["L3-L4"], "挪文件夹失败"
    assert L.move_folder(sub, nce, 0) and [f["name"] for f in L.folders() if f["parent_id"] == nce][0] == "L3-L4", "排序没生效"
    st = L.folder_stats(nce)
    assert st["folders"] == 2 and st["packs"] == 2, f"文件夹统计不对：{st}"
    # --- 导出整个文件夹：导出根/文件夹层级/包名/*.mp4，没有说明 / manifest / 预设名那一层
    w.lib.add_to_pack(pb, h.media_key, h.idx, "whose … is it", h.tier)
    L.db.execute("UPDATE packs SET name=? WHERE id=?", ("is: this/his…?", other)); L.db.commit()
    w.lib.add_to_pack(other, h.media_key, h.idx, "x", h.tier)
    od = Path(root) / "我的导出"
    w.refresh_packs((K_FOLDER, nce)); w._test_export = str(od / "NCE1")
    w.export_pack(); w._test_export = None
    for ew in list(w.export_workers):
        ew.wait(120000)
    app.processEvents()
    files = sorted(p.relative_to(od).as_posix() for p in od.rglob("*") if p.is_file())
    want = clip_name(h.en) + ".mp4"
    assert files == sorted([f"NCE1/L3-L4/Whose… is it/{want}", f"NCE1/L1-L2/is this his…/{want}"]), f"导出目录结构不对：{files}"
    assert not list(od.rglob("说明")) and not list(od.rglob("manifest.json")) and not list(od.rglob("横屏原画*")), "还有说明 / manifest"
    assert any(x == (od / "NCE1").resolve() for x in L.export_dirs()), "导出文件夹没记下（会被扫进媒体库）"
    w._test_confirm = None
    # --- 删除非空文件夹：一起删
    w._test_confirm = True; w.refresh_packs((K_FOLDER, nce)); w.delete_pack(); w._test_confirm = None
    assert not L.folder(nce) and not L.pack(pb) and not L.pack(other) and not L.folder(sub), "非空文件夹没删干净"
    # --- 旧库迁移：包名 UNIQUE 的老表 → 全部放最外层
    d = tempfile.mkdtemp(prefix="clipper-mig-"); os.makedirs(os.path.join(d, ".clipper"))
    db = sqlite3.connect(os.path.join(d, ".clipper", "library.db"))
    db.executescript("CREATE TABLE packs(id INTEGER PRIMARY KEY, name TEXT UNIQUE, created REAL);"
                     "INSERT INTO packs VALUES(1,'is this your…',1),(2,'is this his…',2);")
    db.commit(); db.close()
    M = Library(d)
    ps = M.packs()
    assert [(p["name"], p["folder_id"]) for p in ps] == [("is this your…", None), ("is this his…", None)], "旧库迁移不对"
    f1 = M.create_folder("NCE1")
    assert M.create_pack("is this your…", f1), "迁移后同名包不能放进别的文件夹"
    M.db.close()


def _result_row_tests(w, app, root):
    """1.3.5：①结果列表不重复（以前滚动过再搜，_more 被滚动条信号重入，顶上多出两条旧行）；
    ②星标三态 = 这条在不在采集包里（批量查一次）、点星只加进当前包、已在当前包 → 确认后只从当前包移除、各处变动后同步刷新。"""
    import time
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from clipper.ui.widgets import ResultRow, STAR_IN, STAR_OTHER, STAR_NONE
    from clipper.ui.packs import K_PACK, SEP
    L = w.lib
    # 第二部剧：只有字幕，40 句同一句型
    d = os.path.join(root, "Star Show", "S01"); os.makedirs(d, exist_ok=True)
    animals = ["cat", "dog", "fish", "bird", "horse", "duck", "frog", "goat", "lamb", "mouse"]
    with open(os.path.join(d, "S01E01.srt"), "w", encoding="utf-8") as fh:
        for i in range(40):
            a, b = animals[i % 10], animals[(i * 3 + 1) % 10]
            t = 2 + i * 3
            fh.write(f"{i + 1}\n00:{t // 60:02d}:{t % 60:02d},000 --> 00:{t // 60:02d}:{t % 60:02d},900\n"
                     f"Is she a {a} person or a {b} person number {i}?\n她是{a}派还是{b}派？\n\n")
    w.rescan(); w._scan.wait(60000); app.processEvents()
    w.results.setFixedHeight(260)          # 视口矮一点，40 条一定能滚

    def flush():
        t0 = time.time()
        while time.time() - t0 < 0.05:          # 让延迟布局（0ms 定时器）跑完
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            time.sleep(0.005)
        app.processEvents()

    def check(tag):
        flush()
        vp = w.results.viewport()
        items = [w.results.item(i) for i in range(w.results.count())]
        widgets = [w.results.itemWidget(it) for it in items]
        rows = [x for x in widgets if isinstance(x, ResultRow)]
        ids = [r.hit.cue_id for r in rows]
        assert len(ids) == len(set(ids)), f"{tag}：列表里有重复 cue_id：{len(ids)} 行 {len(set(ids))} 种"
        n_hits = len(w.filtered())
        assert len(rows) == min(n_hits, w.shown) and w.shown <= n_hits, \
            f"{tag}：结果 {n_hits} 条，已加载 {w.shown}，列表里却有 {len(rows)} 行"
        assert {cid: x[1] for cid, x in w.rows.items()} == {r.hit.cue_id: r for r in rows}, f"{tag}：rows 表和列表对不上"
        live = [r for r in w.findChildren(ResultRow)]
        assert all(r.parentWidget() is vp for r in live), f"{tag}：有结果行不在列表视口里"
        vis = [r for r in live if r.isVisible() and r.geometry().intersects(vp.rect())]   # 视口里看得见的
        stray = [r for r in vis if r not in rows]
        assert not stray, f"{tag}：视口里浮着 {len(stray)} 条不属于列表的旧行（{[r.hit.en for r in stray][:2]}）"
        # 整行都在视口里的必须看得见（边上只露出间距的那行各平台字体高度不同，不强求）
        want = [r for r, it in zip(widgets, items) if isinstance(r, ResultRow) and not it.isHidden()
                and vp.rect().contains(w.results.visualItemRect(it))]
        miss = [r for r in want if not r.isVisible()]
        assert (want or not rows) and not miss, f"{tag}：视口里应完整显示 {len(want)} 行，其中 {len(miss)} 行看不见"
        return len(rows)

    sb = w.results.verticalScrollBar()
    w.q.setText("is she a ... or a ..."); w.do_search(); flush()
    assert len(w.hits) >= 30, f"测试剧没搜出来：{len(w.hits)}"
    check("第一次搜索")
    for v in (sb.maximum(), sb.maximum() // 2, 120):
        sb.setValue(v); flush()
        w.do_search(); check(f"滚到 {v} 后再搜")
        sb.setValue(sb.maximum()); flush()
        w.tabs.setCurrentIndex(2); check("切到近似变体"); sb.setValue(sb.maximum()); flush()
        w.tabs.setCurrentIndex(0); check("切回全部")
    # 收藏 / 微调 / 再搜：前两条不能「多出旧的」
    w._test_newpack = (True, None)
    w.preview(w.sorted_hits()[0], autoplay=False); flush()
    s, e = w.cur_span
    w._cut_changed(s + 0.2, e + 0.1); w._flush_cut(); w.collect(w.cur); check("微调+收藏后")
    sb.setValue(sb.maximum()); flush()
    w.do_search(); check("收藏后滚动再搜")
    top = [w.results.itemWidget(w.results.item(i)) for i in range(2)]
    assert top[0].hit.cue_id != top[1].hit.cue_id and top[0].star_state == STAR_IN and \
        abs(L.final_span(w._cue_row(top[0].hit), top[0].hit.media_key)[0] - (s + 0.2)) < 1e-6, "第一条状态 / 时间不对"
    # 滚到底分页加载也不重复
    while w.shown < len(w.items):
        sb.setValue(sb.maximum()); flush()
        if w.shown >= len(w.items):
            break
    check("滚到底")

    # ---------- 星标三态
    hs = w.sorted_hits()
    target = w.fav_pack_id
    assert target is not None and L.pack(target)["name"] == w.active_pattern, "没建同名目标包"
    fo = L.ensure_folder_path("星标测试/别的")
    other = L.create_pack("另一个包", fo)
    A, B, C = hs[0], hs[1], hs[2]                     # A 在目标包（上面收藏的）；B 只在别的包；C 哪都不在
    L.add_to_pack(other, B.media_key, B.idx, "x", B.tier)
    calls = []
    real = L.pack_membership
    L.pack_membership = lambda keys=None: (calls.append(1), real(keys))[1]
    try:
        w.render_results()
    finally:
        L.pack_membership = real
    assert len(calls) == 1, f"建结果行时应只批量查一次采集包，实际 {len(calls)} 次"
    rw = lambda h: w.rows[h.cue_id][1]
    assert (rw(A).star_state, rw(B).star_state, rw(C).star_state) == (STAR_IN, STAR_OTHER, STAR_NONE), \
        f"星标三态不对：{rw(A).star_state} {rw(B).star_state} {rw(C).star_state}"
    p_other = SEP.join(L.pack_path(other)); p_tgt = SEP.join(L.pack_path(target))
    assert p_other in rw(B).star.toolTip() and p_tgt in rw(A).star.toolTip(), "悬停提示没列出完整路径"
    keep = {h.cue_id: rw(h) for h in (A, B, C)}
    # 点 B 的星：只加进当前包，别的包不动
    n_other = L.pack_count(other)
    w.collect(B); flush()
    assert rw(B).star_state == STAR_IN and L.pack_count(other) == n_other, "点星没加进当前包或动了别的包"
    assert p_other in rw(B).star.toolTip(), "也在别的包时提示里没列出来"
    # 顶部「收藏」按钮跟当前片段
    w.preview(C, autoplay=False); flush()
    assert w.b_fav.property("star") == STAR_NONE and w.b_fav.text() == "收藏", "收藏按钮没反映灰色状态"
    w.preview(A, autoplay=False); flush()
    assert w.b_fav.property("star") == STAR_IN and w.b_fav.text() == "已收藏", "收藏按钮没反映实心状态"
    # 已在当前包：取消不删；确认只从当前包删
    w._test_confirm = False; w.collect(A); flush()
    assert rw(A).star_state == STAR_IN and A.idx in [r["idx"] for r in L.pack_items(target)], "取消了却移除了"
    L.add_to_pack(other, A.media_key, A.idx, "x", A.tier)
    w._test_confirm = True; w.collect(A); flush(); w._test_confirm = None
    assert A.idx not in [r["idx"] for r in L.pack_items(target)] and A.idx in [r["idx"] for r in L.pack_items(other)], \
        "移除应只动当前包"
    assert rw(A).star_state == STAR_OTHER and w.b_fav.property("star") == STAR_OTHER, "移除后星标没刷新"
    # 采集包页里移除条目 → 结果行星标同步
    w.refresh_packs((K_PACK, target)); flush()
    for i in range(w.pack_list.count()):
        if tuple(w.pack_list.item(i).data(Qt.UserRole + 1)) == (B.media_key, B.idx):
            w.pack_list.setCurrentRow(i)
    w._pack_remove(); flush()
    assert rw(B).star_state == STAR_OTHER, "采集包页移除后星标没同步"
    # 删除包 → 同步
    w.refresh_packs((K_PACK, other)); w._test_confirm = True; w.delete_pack(); w._test_confirm = None; flush()
    assert rw(A).star_state == STAR_NONE and rw(B).star_state == STAR_NONE, "删包后星标没同步"
    # 切换「收藏到」目标 → 同步
    t2 = L.create_pack(w.active_pattern, fo)
    L.add_to_pack(t2, C.media_key, C.idx, w.active_pattern, C.tier)
    w.refresh_packs(); flush()
    assert rw(C).star_state == STAR_OTHER, "只在另一个同名包里应是金色空心"
    w._fav_pick(t2); flush()
    assert rw(C).star_state == STAR_IN and w.fav_pack_id == t2, "切换目标后星标没同步"
    w._fav_pick(target); flush()
    assert rw(C).star_state == STAR_OTHER, "切回目标后星标没同步"
    assert all(rw(w.hits[[h.cue_id for h in w.hits].index(cid)]) is x for cid, x in keep.items()), "刷新星标却重建了列表"
    check("星标操作后")
    w.results.setMinimumHeight(0); w.results.setMaximumHeight(16777215)
    w._test_newpack = None


def _wave_tests(w, app):
    """1.3.4：整集缩略音轨读出并渲染；dpr=2 时灰 / 青两层波形都只在波形格内。"""
    import numpy as np, time
    from PySide6.QtGui import QColor
    from clipper.ui.widgets import WaveformView
    for _ in range(200):
        if w.dmap.env is not None:
            break
        app.processEvents(); time.sleep(0.05)
    for ow in list(w._ov_workers):
        ow.wait(60000)
    app.processEvents()
    assert w.dmap.env is not None and len(w.dmap.env) > 500, "整集包络没读出来"
    cache = list((w.lib.data / "wave").glob("*.ov200.*.u8"))
    assert cache, "整集包络没缓存"
    w.dmap.set_view(1.0, 2.0); w.dmap.set_playhead(1.5)
    img = w.dmap.grab().toImage()
    assert img.width() > 100 and w.dmap.height() >= 56, "缩略音轨没画出来"
    v = WaveformView(); v.resize(600, 200)
    t = np.arange(8000 * 4) / 8000.0
    v.set_data(0.0, 4.0, [dict(w="hello", start=1.0, end=1.5)], [(0.5, 3.0, "Hello there", 1, "sub")], (0.5, 3.0), (1.0, 3.0))
    v.set_samples((np.sin(t * 2 * np.pi * 200) * 0.9).astype(np.float32), 0.0)
    v._set_view(0.0, 4.0, emit=False)
    top, bottom = v._geom()
    for dpr in (1.0, 2.0):
        base, teal = v._build_cache(dpr)
        assert abs(base.devicePixelRatio() - dpr) < 1e-6 and abs(teal.devicePixelRatio() - dpr) < 1e-6, "位图 dpr 不一致"
        bi, ti = base.toImage(), teal.toImage()
        gray = QColor("#475569").rgb() & 0xFFFFFF
        rows_in, rows_out = 0, 0
        for y in range(bi.height()):
            yl = y / dpr
            cnt = sum(1 for x in range(0, bi.width(), 7) if (bi.pixel(x, y) & 0xFFFFFF) == gray)
            if top - 1 <= yl <= bottom + 1:
                rows_in += cnt > 0
            else:
                rows_out += cnt
        assert rows_in > 10 and rows_out == 0, f"dpr={dpr} 灰色波形溢出到波形格外：{rows_out}"
        out_t = sum(1 for y in range(ti.height()) if not (top - 1 <= y / dpr <= bottom + 1)
                    for x in range(0, ti.width(), 7) if (ti.pixel(x, y) >> 24) > 0)
        assert out_t == 0, f"dpr={dpr} 青色波形溢出：{out_t}"
    v.deleteLater()


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
    chk("搜索排名（1.3.5 含缩写互通）", _rank_tests)
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
